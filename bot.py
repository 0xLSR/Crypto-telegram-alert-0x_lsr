"""Continuous Telegram crypto alert worker backed by Telegram and GeckoTerminal."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "data/state.json"))
TELEGRAM_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 35
API_REQUEST_TIMEOUT_SECONDS = 15
PRICE_CHECK_SECONDS = max(15, int(os.getenv("PRICE_CHECK_SECONDS", "60")))
MAX_WATCHES = 100
LOG = logging.getLogger("crypto_alert_bot")
STATE_LOCK = threading.RLock()
STOP = threading.Event()


class BotError(Exception):
    """Safe, token-redacted service error."""


class TelegramError(BotError):
    pass


class MarketDataError(BotError):
    pass


def service_for_url(url: str) -> tuple[str, type[BotError]]:
    host = urllib.parse.urlsplit(url).hostname
    if host == "api.telegram.org":
        return "Telegram", TelegramError
    if host == "api.geckoterminal.com":
        return "GeckoTerminal", MarketDataError
    return "API externa", MarketDataError


def redact_token(value: str, url: str) -> str:
    match = re.search(r"/bot([^/]+)/", urllib.parse.urlsplit(url).path)
    if match:
        token = match.group(1)
        for form in (token, urllib.parse.quote(token, safe="")):
            value = value.replace(form, "[REDACTED]")
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise BotError(f"{name} deve ser numérico.") from exc
    if not minimum <= value <= maximum:
        raise BotError(f"{name} deve estar entre {minimum} e {maximum}.")
    return value


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"offset": 0, "watches": {}}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("watches", {}), dict):
            raise ValueError("invalid state")
        value.setdefault("offset", 0)
        value.setdefault("watches", {})
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise BotError("Não foi possível ler o estado salvo em data/state.json.") from exc


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
              timeout: float = API_REQUEST_TIMEOUT_SECONDS) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": "crypto-telegram-alert/1.0", "Content-Type": "application/json"}
    if urllib.parse.urlsplit(url).hostname == "api.geckoterminal.com":
        headers["Accept"] = "application/json;version=20230203"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    service, error_type = service_for_url(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_token(exc.read().decode("utf-8", errors="replace"), url)
        LOG.error("%s respondeu HTTP %s; corpo da resposta: %s", service, exc.code, detail[:4000])
        raise error_type(f"{service} respondeu HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        detail = redact_token(f"{type(exc.reason).__name__}: {exc.reason}", url)
        LOG.error("Erro de rede ao acessar %s: %s", service, detail)
        raise error_type(f"Erro de rede no {service}: {detail}") from exc
    except (TimeoutError, OSError) as exc:
        detail = redact_token(f"{type(exc).__name__}: {exc}", url)
        LOG.error("Erro de rede ao acessar %s: %s", service, detail)
        raise error_type(f"Erro de rede no {service}: {detail}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOG.error("Resposta JSON inválida recebida de %s: %s", service, exc)
        raise error_type(f"Resposta JSON inválida recebida de {service}.") from exc


def telegram(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    url = API_BASE.format(token=token, method=method)
    result = http_json(url, method="POST", payload=payload or {}, timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, dict):
        raise TelegramError(f"Telegram retornou resposta inválida para {method}.")
    if not result.get("ok"):
        code = result.get("error_code", "desconhecido")
        description = redact_token(str(result.get("description") or "sem descrição"), url)
        raise TelegramError(f"Telegram API {code}: {description}")
    return result.get("result")


def validate_telegram(token: str) -> None:
    me = telegram(token, "getMe")
    if not isinstance(me, dict) or not me.get("id"):
        raise BotError("Telegram getMe não retornou a identidade do bot.")
    if telegram(token, "deleteWebhook", {"drop_pending_updates": False}) is not True:
        raise BotError("Telegram não confirmou a remoção do webhook.")
    LOG.info("Telegram autenticado como @%s; webhook removido preservando atualizações pendentes.", me.get("username", "indisponível"))


def send_message(token: str, chat_id: int | str, text: str) -> None:
    telegram(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "indisponível"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.10f}".rstrip("0").rstrip(".")


def compact(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "indisponível"


def normalize_token(token: dict[str, Any], pools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Adapt GeckoTerminal token/pool resources to the bot's display format."""
    attributes = token.get("attributes") or {}
    if not attributes.get("address"):
        return None
    pool_by_id = {pool.get("id"): pool for pool in pools if pool.get("id")}
    top_pool_ids = (((token.get("relationships") or {}).get("top_pools") or {}).get("data") or [])
    pool = next((pool_by_id.get(item.get("id")) for item in top_pool_ids if pool_by_id.get(item.get("id"))), None)
    if pool is None and pools:
        pool = max(pools, key=lambda p: float((p.get("attributes") or {}).get("reserve_in_usd") or 0))
    pool_attributes = (pool or {}).get("attributes") or {}
    token_price = attributes.get("price_usd")
    if token_price is None:
        token_price = pool_attributes.get("token_price_usd") or pool_attributes.get("base_token_price_usd")
    volume = attributes.get("volume_usd") or {}
    volume_24h = volume.get("h24") if isinstance(volume, dict) else None
    volume_24h = volume_24h or (pool_attributes.get("volume_usd") or {}).get("h24")
    price_change = pool_attributes.get("price_change_percentage") or {}
    pool_address = pool_attributes.get("address")
    token_address = attributes["address"]
    return {
        "chainId": "solana",
        "baseToken": {"address": token_address, "symbol": attributes.get("symbol"), "name": attributes.get("name")},
        "priceUsd": token_price,
        "priceChange": {"h24": price_change.get("h24")},
        "liquidity": {"usd": attributes.get("total_reserve_in_usd") or pool_attributes.get("reserve_in_usd")},
        "volume": {"h24": volume_24h},
        "marketCap": attributes.get("market_cap_usd") or pool_attributes.get("market_cap_usd"),
        "fdv": attributes.get("fdv_usd") or pool_attributes.get("fdv_usd"),
        "url": f"https://www.geckoterminal.com/solana/pools/{pool_address}" if pool_address else f"https://www.geckoterminal.com/solana/tokens/{token_address}",
    }


def gecko_pools(result: dict[str, Any]) -> list[dict[str, Any]]:
    included = result.get("included") or []
    return [resource for resource in included if resource.get("type") == "pool"]


def get_pair(query: str) -> dict[str, Any] | None:
    """Resolve a Solana token by address or symbol with GeckoTerminal public API."""
    is_address = bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,50}", query))
    if is_address:
        encoded = urllib.parse.quote(query, safe="")
        result = http_json(f"{GECKO_BASE}/networks/solana/tokens/{encoded}?include=top_pools", timeout=API_REQUEST_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            raise MarketDataError("GeckoTerminal retornou uma resposta em formato inesperado.")
        token = result.get("data") or {}
        return normalize_token(token, gecko_pools(result))

    params = urllib.parse.urlencode({"query": query, "network": "solana", "include": "base_token,quote_token"})
    result = http_json(f"{GECKO_BASE}/search/pools?{params}", timeout=API_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, dict):
        raise MarketDataError("GeckoTerminal retornou uma resposta em formato inesperado.")
    included = result.get("included") or []
    token_by_id = {resource.get("id"): resource for resource in included if resource.get("type") == "token"}
    pools = [resource for resource in (result.get("data") or []) if resource.get("type") == "pool"]
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for pool in pools:
        relationships = pool.get("relationships") or {}
        base_ref = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
        quote_ref = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
        base = token_by_id.get(base_ref)
        quote = token_by_id.get(quote_ref)
        matching = [token for token in (base, quote) if token and query.casefold() in {
            str((token.get("attributes") or {}).get("symbol", "")).casefold(),
            str((token.get("attributes") or {}).get("name", "")).casefold(),
        }]
        for token in matching or ([base] if base else []):
            candidates.append((token, [pool]))
    pairs = [pair for token, token_pools in candidates if (pair := normalize_token(token, token_pools))]
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), default=None)


def pair_summary(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    change = (pair.get("priceChange") or {}).get("h24")
    change_text = f"{float(change):+.2f}%" if change is not None else "indisponível"
    cap = pair.get("marketCap") or pair.get("fdv")
    return (f"{base.get('symbol') or base.get('name') or 'Token'} (Solana)\n"
        f"Preço: {money(pair.get('priceUsd'))}\nVariação 24h: {change_text}\n"
        f"Liquidez: {compact((pair.get('liquidity') or {}).get('usd'))} · Volume 24h: {compact((pair.get('volume') or {}).get('h24'))}\n"
        f"Market cap/FDV: {compact(cap)}\nEndereço: {base.get('address', 'indisponível')}\nFonte: GeckoTerminal — {pair.get('url', '')}")


def help_text() -> str:
    return ("Comandos disponíveis:\n/price <TOKEN ou endereço> — consultar preço\n"
        "/watch <endereço Solana> — monitorar token\n/unwatch <endereço> — parar monitoramento\n"
        "/list — listar tokens monitorados\n/help — mostrar ajuda\n\n"
        "Alertas são enviados quando o preço se move pelo percentual configurado.")


def handle_update(update: dict[str, Any], state: dict[str, Any], token: str, threshold: float,
                  allowed_ids: set[str]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("text") or not message.get("chat"):
        return
    chat_id = message["chat"]["id"]
    user_id = str((message.get("from") or {}).get("id", ""))
    if allowed_ids and user_id not in allowed_ids:
        send_message(token, chat_id, "Acesso não autorizado.")
        return
    parts = message["text"].strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    with STATE_LOCK:
        watches = state["watches"]
        try:
            if command == "/start":
                send_message(token, chat_id, "Bot de alertas Solana ativo.\n\n" + help_text())
            elif command == "/help":
                send_message(token, chat_id, help_text())
            elif command == "/price":
                if not arg:
                    send_message(token, chat_id, "Uso: /price <TOKEN ou endereço>")
                else:
                    pair = get_pair(arg)
                    send_message(token, chat_id, pair_summary(pair) if pair else "Token Solana não encontrado no GeckoTerminal.")
            elif command == "/watch":
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,50}", arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /watch <endereço>")
                    return
                pair = get_pair(arg)
                if not pair:
                    send_message(token, chat_id, "Não encontrei um par Solana para esse endereço no GeckoTerminal.")
                    return
                address = pair["baseToken"]["address"]
                key = address.lower()
                if key not in watches and len(watches) >= MAX_WATCHES:
                    send_message(token, chat_id, f"Limite de {MAX_WATCHES} tokens monitorados atingido.")
                    return
                entry = watches.setdefault(key, {"address": address, "subscribers": [], "anchor_price": float(pair.get("priceUsd") or 0), "last_alert": 0})
                if str(chat_id) not in entry["subscribers"]:
                    entry["subscribers"].append(str(chat_id))
                send_message(token, chat_id, f"Monitoramento ativado (limiar {threshold:g}%).\n" + pair_summary(pair))
            elif command == "/unwatch":
                entry = watches.get(arg.lower()) or next((e for e in watches.values() if e["address"].lower() == arg.lower()), None)
                if not entry or str(chat_id) not in entry["subscribers"]:
                    send_message(token, chat_id, "Esse chat não monitora esse endereço.")
                else:
                    entry["subscribers"].remove(str(chat_id))
                    if not entry["subscribers"]:
                        watches.pop(entry["address"].lower(), None)
                    send_message(token, chat_id, "Monitoramento removido.")
            elif command == "/list":
                entries = [e for e in watches.values() if str(chat_id) in e["subscribers"]]
                send_message(token, chat_id, "Seus tokens monitorados:\n" + ("\n".join(f"• {e['address']}" for e in entries) if entries else "nenhum"))
        except TelegramError:
            raise
        except MarketDataError as exc:
            LOG.warning("GeckoTerminal indisponível ao processar %s: %s", command, exc)
            send_message(token, chat_id, "GeckoTerminal está temporariamente indisponível. Tente novamente em instantes.")
        except (BotError, KeyError, TypeError, ValueError) as exc:
            LOG.warning("Falha ao processar %s: %s", command, exc)
            send_message(token, chat_id, "Não consegui concluir o comando agora. Tente novamente mais tarde.")


def check_prices(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    with STATE_LOCK:
        addresses = [(key, entry["address"]) for key, entry in state["watches"].items()]
    for start in range(0, len(addresses), 30):
        if STOP.is_set():
            return
        batch = addresses[start:start + 30]
        try:
            joined = ",".join(address for _, address in batch)
            endpoint = f"{GECKO_BASE}/networks/solana/tokens/multi/{urllib.parse.quote(joined, safe=',')}?include=top_pools"
            result = http_json(endpoint, timeout=API_REQUEST_TIMEOUT_SECONDS)
            if not isinstance(result, dict):
                raise MarketDataError("GeckoTerminal retornou uma resposta em formato inesperado.")
            pools = gecko_pools(result)
            by_address: dict[str, dict[str, Any]] = {}
            for token_resource in result.get("data") or []:
                pair = normalize_token(token_resource, pools)
                if pair:
                    by_address[pair["baseToken"]["address"].lower()] = pair
            for key, _ in batch:
                pair = by_address.get(key)
                if not pair or not pair.get("priceUsd"):
                    continue
                now = time.time()
                price = float(pair["priceUsd"])
                with STATE_LOCK:
                    entry = state["watches"].get(key)
                    if entry is None:
                        continue
                    anchor = float(entry.get("anchor_price") or price)
                    if anchor <= 0:
                        entry["anchor_price"] = price
                        continue
                    change = (price / anchor - 1) * 100
                    if abs(change) < threshold or now - float(entry.get("last_alert", 0)) < cooldown:
                        continue
                    subscribers = list(entry.get("subscribers", []))
                text = f"🚨 Alerta de preço: {change:+.2f}% desde o último alerta\n" + pair_summary(pair)
                delivered = False
                for chat_id in subscribers:
                    try:
                        send_message(token, chat_id, text)
                        delivered = True
                    except TelegramError as exc:
                        LOG.warning("Telegram não entregou alerta; será tentado novamente no próximo ciclo: %s", exc)
                if delivered:
                    with STATE_LOCK:
                        entry = state["watches"].get(key)
                        if entry is not None:
                            entry["anchor_price"] = price
                            entry["last_alert"] = now
        except MarketDataError as exc:
            LOG.warning("GeckoTerminal falhou no monitoramento; o bot continua ativo: %s", exc)


def market_monitor(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    failures = 0
    while not STOP.is_set():
        with STATE_LOCK:
            has_watches = bool(state["watches"])
        if not has_watches:
            failures = 0
            STOP.wait(PRICE_CHECK_SECONDS)
            continue
        try:
            check_prices(state, token, threshold, cooldown)
            failures = 0
            with STATE_LOCK:
                save_state(state)
            STOP.wait(PRICE_CHECK_SECONDS)
        except Exception as exc:
            failures += 1
            delay = min(300, 10 * (2 ** min(failures - 1, 5)))
            LOG.exception("Erro no ciclo de monitoramento de preços; nova tentativa em %ss: %s", delay, exc)
            STOP.wait(delay)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOG.error("Configure TELEGRAM_BOT_TOKEN no ambiente.")
        return 2
    try:
        threshold = env_float("ALERT_THRESHOLD_PERCENT", 10, 0.1, 1000)
        cooldown = env_float("ALERT_COOLDOWN_MINUTES", 30, 1, 10080) * 60
        run_seconds = env_float("RUN_FOR_SECONDS", 0, 0, 86400)
    except BotError as exc:
        LOG.error("Configuração inválida: %s", exc)
        return 2
    state = load_state()
    allowed_ids = {v.strip() for v in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if v.strip()}
    STOP.clear()
    def request_stop(_signum: int, _frame: Any) -> None:
        STOP.set()
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    # Validate credentials and remove webhook at boot; retry safely during Telegram outages.
    attempt = 0
    while not STOP.is_set():
        try:
            validate_telegram(token)
            break
        except TelegramError as exc:
            attempt += 1
            delay = min(300, 5 * (2 ** min(attempt - 1, 6)))
            LOG.error("Telegram indisponível na inicialização (tentativa %d); nova tentativa em %ss: %s", attempt, delay, exc)
            STOP.wait(delay)
    if STOP.is_set():
        return 0

    LOG.info("Worker contínuo iniciado; tokens monitorados: %d", len(state["watches"]))
    monitor = threading.Thread(target=market_monitor, args=(state, token, threshold, cooldown), name="geckoterminal-monitor", daemon=True)
    monitor.start()
    failures = 0
    deadline = time.monotonic() + run_seconds if run_seconds else None
    while not STOP.is_set() and (deadline is None or time.monotonic() < deadline):
        try:
            updates = telegram(token, "getUpdates", {"offset": int(state["offset"]), "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}) or []
            failures = 0
            for update in updates:
                try:
                    handle_update(update, state, token, threshold, allowed_ids)
                except TelegramError as exc:
                    LOG.warning("Falha Telegram ao responder atualização %s; ela será repetida: %s", update.get("update_id", "?"), exc)
                    break
                with STATE_LOCK:
                    state["offset"] = update["update_id"] + 1
                    save_state(state)
        except TelegramError as exc:
            failures += 1
            delay = min(60, 2 ** min(failures - 1, 6))
            LOG.warning("Falha temporária no polling Telegram; nova tentativa em %ss: %s", delay, exc)
            STOP.wait(delay)
    with STATE_LOCK:
        save_state(state)
    LOG.info("Janela de execução encerrada; estado salvo. Aguardando o monitor concluir.")
    monitor.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
