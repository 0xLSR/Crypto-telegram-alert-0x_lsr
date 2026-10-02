"""Continuous Telegram crypto alert worker backed by Telegram and DexScreener."""
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
DEX_BASE = "https://api.dexscreener.com"
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "data/state.json"))
TELEGRAM_POLL_TIMEOUT_SECONDS = 1
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 15
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


class DexScreenerError(BotError):
    pass


def service_for_url(url: str) -> tuple[str, type[BotError]]:
    host = urllib.parse.urlsplit(url).hostname
    if host == "api.telegram.org":
        return "Telegram", TelegramError
    if host == "api.dexscreener.com":
        return "DexScreener", DexScreenerError
    return "API externa", DexScreenerError


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_valid_solana_address(value: str) -> bool:
    """Validate a Solana public key as a whitespace-free 32-byte Base58 value."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    if any(char not in BASE58_ALPHABET for char in value):
        return False
    number = 0
    for char in value:
        number = number * 58 + BASE58_ALPHABET.index(char)
    decoded_length = (number.bit_length() + 7) // 8
    decoded_length += len(value) - len(value.lstrip("1"))
    return decoded_length == 32


COMMAND_ALIASES = {"/preço": "/price", "/add": "/watch", "/lista": "/list", "/remove": "/unwatch"}


def parse_message(text: str) -> tuple[str | None, str]:
    """Return canonical command and one argument, including bare Solana addresses."""
    if not isinstance(text, str):
        return None, ""
    stripped = text.strip()
    if is_valid_solana_address(stripped):
        return "/price", stripped
    if not stripped.startswith("/"):
        return None, ""
    parts = stripped.split(maxsplit=1)
    raw_command = parts[0].split("@", 1)[0].casefold()
    return COMMAND_ALIASES.get(raw_command, raw_command), parts[1].strip() if len(parts) > 1 else ""


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
        try:
            offset = max(0, int(value.get("offset", 0)))
        except (TypeError, ValueError, OverflowError):
            offset = 0
        watches: dict[str, Any] = {}
        for entry in value.get("watches", {}).values():
            if len(watches) >= MAX_WATCHES:
                break
            if not isinstance(entry, dict):
                continue
            address = entry.get("address")
            if not is_valid_solana_address(address):
                continue
            subscribers = entry.get("subscribers", [])
            if not isinstance(subscribers, list):
                subscribers = []
            normalized = dict(entry)
            normalized.update(address=address, subscribers=list(dict.fromkeys(str(v) for v in subscribers)))
            # Base58 addresses are case-sensitive; never lowercase their identity.
            watches[address] = normalized
        return {"offset": offset, "watches": watches}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        LOG.error("Estado salvo inválido ou inacessível; iniciando sem offset avançado e sem watches: %s", exc)
        return {"offset": 0, "watches": {}}


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


def pair_liquidity(pair: dict[str, Any]) -> float:
    try:
        return float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def get_pair(query: str) -> dict[str, Any] | None:
    """Resolve a Solana token by validated address or DexScreener symbol search."""
    if is_valid_solana_address(query):
        url = f"{DEX_BASE}/token-pairs/v1/solana/{urllib.parse.quote(query, safe='')}"
        result = http_json(url, timeout=API_REQUEST_TIMEOUT_SECONDS)
        if not isinstance(result, list):
            raise DexScreenerError("DexScreener retornou uma resposta em formato inesperado.")
        pairs = [pair for pair in result if isinstance(pair, dict) and pair.get("chainId") == "solana"]
        exact_base = [pair for pair in pairs if str((pair.get("baseToken") or {}).get("address", "")).lower() == query.lower()]
        return max(exact_base, key=pair_liquidity, default=None)

    params = urllib.parse.urlencode({"q": query})
    result = http_json(f"{DEX_BASE}/latest/dex/search?{params}", timeout=API_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, dict):
        raise DexScreenerError("DexScreener retornou uma resposta em formato inesperado.")
    pairs = [pair for pair in (result.get("pairs") or []) if isinstance(pair, dict) and pair.get("chainId") == "solana"]
    matches = [pair for pair in pairs if query.casefold() in {
        str((pair.get("baseToken") or {}).get("symbol", "")).casefold(),
        str((pair.get("baseToken") or {}).get("name", "")).casefold(),
    }]
    return max(matches or pairs, key=pair_liquidity, default=None)


def pair_summary(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    change = (pair.get("priceChange") or {}).get("h24")
    change_text = f"{float(change):+.2f}%" if change is not None else "indisponível"
    cap = pair.get("marketCap") or pair.get("fdv")
    return (f"{base.get('symbol') or base.get('name') or 'Token'} (Solana)\n"
        f"Preço: {money(pair.get('priceUsd'))}\nVariação 24h: {change_text}\n"
        f"Liquidez: {compact((pair.get('liquidity') or {}).get('usd'))} · Volume 24h: {compact((pair.get('volume') or {}).get('h24'))}\n"
        f"Market cap/FDV: {compact(cap)}\nEndereço: {base.get('address', 'indisponível')}\nDexScreener: {pair.get('url') or 'indisponível'}")


def help_text() -> str:
    return ("Comandos disponíveis:\n/price <TOKEN ou endereço> — consultar preço (ou envie só o endereço)\n"
        "/watch <endereço Solana> (/add) — monitorar token\n/unwatch <endereço> (/remove) — parar monitoramento\n"
        "/list (/lista) — listar tokens deste chat\n/help — mostrar ajuda\n\n"
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
    command, arg = parse_message(message["text"])
    if command is None:
        return
    LOG.info("Comando recebido: chat_id=%s user_id=%s comando=%s", chat_id, user_id or "indisponível", command)
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
                elif arg.startswith("/") or (" " in arg and is_valid_solana_address(arg)):
                    send_message(token, chat_id, "Informe apenas um símbolo ou endereço Solana válido.")
                elif len(arg.split()) > 1:
                    send_message(token, chat_id, "Informe apenas um símbolo ou endereço Solana válido.")
                elif any(char.isspace() for char in arg):
                    send_message(token, chat_id, "Informe apenas um símbolo ou endereço Solana válido.")
                else:
                    if 32 <= len(arg) <= 50 and not is_valid_solana_address(arg):
                        send_message(token, chat_id, "Endereço Solana inválido. Confira o Base58 e tente novamente.")
                        return
                    pair = get_pair(arg)
                    send_message(token, chat_id, pair_summary(pair) if pair else "Token Solana não encontrado no DexScreener.")
            elif command == "/watch":
                if not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /watch <endereço>")
                    return
                pair = get_pair(arg)
                if not pair:
                    send_message(token, chat_id, "Não encontrei um par Solana para esse endereço no DexScreener.")
                    return
                address = arg
                key = address
                if key not in watches and len(watches) >= MAX_WATCHES:
                    send_message(token, chat_id, f"Limite de {MAX_WATCHES} tokens monitorados atingido.")
                    return
                entry = watches.setdefault(key, {"address": address, "subscribers": [], "anchor_price": float(pair.get("priceUsd") or 0), "last_alert": 0})
                if str(chat_id) not in entry["subscribers"]:
                    entry["subscribers"].append(str(chat_id))
                base = pair.get("baseToken") or {}
                entry["symbol"] = base.get("symbol") or base.get("name") or "Token"
                entry["last_price"] = pair.get("priceUsd")
                LOG.info("Token adicionado: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", address)
                send_message(token, chat_id, f"Monitoramento ativado (limiar {threshold:g}%).\n" + pair_summary(pair))
            elif command == "/unwatch":
                if not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /unwatch <endereço>")
                    return
                entry = watches.get(arg) or next((e for e in watches.values() if e["address"] == arg), None)
                if not entry or str(chat_id) not in entry["subscribers"]:
                    send_message(token, chat_id, "Esse chat não monitora esse endereço.")
                else:
                    entry["subscribers"].remove(str(chat_id))
                    if not entry["subscribers"]:
                        watches.pop(entry["address"], None)
                    LOG.info("Token removido: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", arg)
                    send_message(token, chat_id, "Monitoramento removido.")
            elif command == "/list":
                entries = [e for e in watches.values() if str(chat_id) in e["subscribers"]]
                rows = []
                for entry in entries:
                    price = money(entry.get("last_price"))
                    rows.append(f"• {entry.get('symbol') or 'Token'} — {entry['address']}\n  Preço: {price} · Alerta: {threshold:g}%")
                send_message(token, chat_id, "Seus tokens monitorados:\n" + ("\n".join(rows) if rows else "nenhum"))
            else:
                send_message(token, chat_id, help_text())
        except TelegramError:
            raise
        except DexScreenerError as exc:
            LOG.warning("Consulta DexScreener falhou em %s: %s", command, exc)
            send_message(token, chat_id, "DexScreener está temporariamente indisponível. Tente novamente em instantes.")
        except Exception as exc:
            LOG.warning("Falha ao processar %s: %s", command, exc)
            send_message(token, chat_id, "Não consegui concluir o comando agora. Tente novamente mais tarde.")


def check_prices(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    with STATE_LOCK:
        addresses = [(key, entry["address"]) for key, entry in state["watches"].items()]
    for key, address in addresses:
        if STOP.is_set():
            return
        try:
            pair = get_pair(address)
            if not pair or not pair.get("priceUsd"):
                continue
            now = time.time()
            price = float(pair["priceUsd"])
            base = pair.get("baseToken") or {}
            with STATE_LOCK:
                entry = state["watches"].get(key)
                if entry is None:
                    continue
                entry["last_price"] = price
                entry["symbol"] = base.get("symbol") or base.get("name") or entry.get("symbol") or "Token"
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
        except DexScreenerError as exc:
            LOG.warning("DexScreener falhou ao consultar token monitorado %s: %s", address, exc)


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
    monitor = threading.Thread(target=market_monitor, args=(state, token, threshold, cooldown), name="dexscreener-monitor", daemon=True)
    monitor.start()
    failures = 0
    first_poll = True
    deadline = time.monotonic() + run_seconds if run_seconds else None
    while not STOP.is_set() and (deadline is None or time.monotonic() < deadline):
        try:
            # Always start at offset 0 so a stale/corrupt cached offset cannot skip pending updates.
            # Successful updates are acknowledged by incrementing offset below.
            poll_offset = 0 if first_poll else int(state["offset"])
            updates = telegram(token, "getUpdates", {"offset": poll_offset, "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}) or []
            if updates:
                first_poll = False
            failures = 0
            for update in updates:
                try:
                    handle_update(update, state, token, threshold, allowed_ids)
                except TelegramError as exc:
                    LOG.warning("Falha Telegram ao responder atualização %s; ela será repetida: %s", update.get("update_id", "?"), exc)
                    first_poll = True
                    break
                except Exception:
                    LOG.exception("Erro isolado ao processar atualização %s; polling continuará.", update.get("update_id", "?"))
                    first_poll = True
                    break
                with STATE_LOCK:
                    state["offset"] = update["update_id"] + 1
                    save_state(state)
        except TelegramError as exc:
            failures += 1
            delay = min(60, 2 ** min(failures - 1, 6))
            LOG.warning("Falha temporária no polling Telegram; nova tentativa em %ss: %s", delay, exc)
            STOP.wait(delay)
        except Exception:
            failures += 1
            first_poll = True
            delay = min(60, 2 ** min(failures - 1, 6))
            LOG.exception("Erro inesperado no ciclo do polling; nova tentativa em %ss.", delay)
            STOP.wait(delay)
    with STATE_LOCK:
        save_state(state)
    LOG.info("Janela de execução encerrada; estado salvo. Aguardando o monitor concluir.")
    monitor.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
