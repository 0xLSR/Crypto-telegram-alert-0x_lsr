"""Continuous Telegram crypto alert worker backed by GeckoTerminal and DexScreener."""
from __future__ import annotations

import json
import logging
import math
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


class TelegramUnauthorized(TelegramError):
    """Authentication rejected by the Telegram Bot API."""


class GeckoTerminalError(BotError):
    pass


class DexScreenerError(BotError):
    pass


class MarketDataError(BotError):
    pass


def service_for_url(url: str) -> tuple[str, type[BotError]]:
    host = urllib.parse.urlsplit(url).hostname
    if host == "api.telegram.org":
        return "Telegram", TelegramError
    if host == "api.geckoterminal.com":
        return "GeckoTerminal", GeckoTerminalError
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
        message = f"{service} respondeu HTTP {exc.code}: {detail[:1000]}"
        if service == "Telegram" and exc.code == 401:
            raise TelegramUnauthorized(message) from exc
        raise error_type(message) from exc
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
        error_type = TelegramUnauthorized if str(code) == "401" else TelegramError
        raise error_type(f"Telegram API {code}: {description}")
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
    except (TypeError, ValueError, OverflowError):
        return "indisponível"
    if not math.isfinite(number):
        return "indisponível"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.10f}".rstrip("0").rstrip(".")


def compact(value: Any) -> str:
    try:
        number = float(value)
        return f"${number:,.0f}" if math.isfinite(number) else "indisponível"
    except (TypeError, ValueError, OverflowError):
        return "indisponível"


def percent(value: Any) -> str:
    try:
        number = float(value)
        return f"{number:+.2f}%" if math.isfinite(number) else "indisponível"
    except (TypeError, ValueError, OverflowError):
        return "indisponível"


def pair_liquidity(pair: dict[str, Any]) -> float:
    try:
        return float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def gecko_pools(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in result.get("included", []) if isinstance(item, dict) and item.get("type") == "pool"]


def normalize_gecko_token(token: dict[str, Any], pools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Convert the documented GeckoTerminal token_detail response to our common pair format."""
    attributes = token.get("attributes") or {}
    address = attributes.get("address")
    if not address:
        return None
    pools_by_id = {pool.get("id"): pool for pool in pools}
    top_ids = (((token.get("relationships") or {}).get("top_pools") or {}).get("data") or [])
    top_pools = [pools_by_id[item.get("id")] for item in top_ids if item.get("id") in pools_by_id]
    def reserve(pool_item: dict[str, Any]) -> float:
        try:
            return float((pool_item.get("attributes") or {}).get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            return 0.0
    pool = max(top_pools or pools, key=reserve, default={})
    pool_attributes = pool.get("attributes") or {}
    volume = attributes.get("volume_usd") or {}
    if not isinstance(volume, dict):
        volume = {}
    pool_volume = pool_attributes.get("volume_usd") or {}
    if not isinstance(pool_volume, dict):
        pool_volume = {}
    change = pool_attributes.get("price_change_percentage") or {}
    address_pool = pool_attributes.get("address")
    return {
        "chainId": "solana",
        "baseToken": {"address": address, "name": attributes.get("name"), "symbol": attributes.get("symbol")},
        "priceUsd": attributes.get("price_usd") or pool_attributes.get("token_price_usd") or pool_attributes.get("base_token_price_usd"),
        "priceChange": {"h24": change.get("h24")},
        "liquidity": {"usd": attributes.get("total_reserve_in_usd") or pool_attributes.get("reserve_in_usd")},
        "volume": {"h24": volume.get("h24") or pool_volume.get("h24")},
        "marketCap": attributes.get("market_cap_usd") or pool_attributes.get("market_cap_usd"),
        "fdv": attributes.get("fdv_usd") or pool_attributes.get("fdv_usd"),
        "url": f"https://www.geckoterminal.com/solana/pools/{address_pool}" if address_pool else f"https://www.geckoterminal.com/solana/tokens/{address}",
        "dataSource": "GeckoTerminal",
    }


def get_gecko_pair(query: str) -> dict[str, Any] | None:
    if is_valid_solana_address(query):
        encoded = urllib.parse.quote(query, safe="")
        result = http_json(f"{GECKO_BASE}/networks/solana/tokens/{encoded}?include=top_pools", timeout=API_REQUEST_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            raise GeckoTerminalError("GeckoTerminal retornou resposta em formato inesperado.")
        token = result.get("data")
        pair = normalize_gecko_token(token, gecko_pools(result)) if isinstance(token, dict) else None
        return pair if pair and pair.get("priceUsd") else None

    params = urllib.parse.urlencode({"query": query, "network": "solana", "include": "base_token,quote_token"})
    result = http_json(f"{GECKO_BASE}/search/pools?{params}", timeout=API_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, dict):
        raise GeckoTerminalError("GeckoTerminal retornou resposta em formato inesperado.")
    token_by_id = {item.get("id"): item for item in result.get("included", []) if isinstance(item, dict) and item.get("type") == "token"}
    candidates = []
    for pool in result.get("data", []):
        if not isinstance(pool, dict) or pool.get("type") != "pool":
            continue
        relationships = pool.get("relationships") or {}
        refs = [((relationships.get(side) or {}).get("data") or {}).get("id") for side in ("base_token", "quote_token")]
        for ref in refs:
            token = token_by_id.get(ref)
            attrs = (token or {}).get("attributes") or {}
            if token and query.casefold() in {str(attrs.get("symbol", "")).casefold(), str(attrs.get("name", "")).casefold()}:
                pair = normalize_gecko_token(token, [pool])
                if pair and pair.get("priceUsd"):
                    candidates.append(pair)
    return max(candidates, key=pair_liquidity, default=None)


def get_gecko_pairs(addresses: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch up to 30 Solana token records in one documented GeckoTerminal request."""
    if not addresses:
        return {}
    joined = ",".join(urllib.parse.quote(address, safe="") for address in addresses)
    result = http_json(f"{GECKO_BASE}/networks/solana/tokens/multi/{joined}?include=top_pools", timeout=API_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise GeckoTerminalError("GeckoTerminal retornou resposta em formato inesperado.")
    pools = gecko_pools(result)
    pairs: dict[str, dict[str, Any]] = {}
    requested = set(addresses)
    for token in result["data"]:
        if not isinstance(token, dict):
            continue
        pair = normalize_gecko_token(token, pools)
        if pair and pair.get("priceUsd") and pair["baseToken"]["address"] in requested:
            pairs[pair["baseToken"]["address"]] = pair
    return pairs


def get_dexscreener_pair(query: str) -> dict[str, Any] | None:
    if is_valid_solana_address(query):
        url = f"{DEX_BASE}/token-pairs/v1/solana/{urllib.parse.quote(query, safe='')}"
        result = http_json(url, timeout=API_REQUEST_TIMEOUT_SECONDS)
        if not isinstance(result, list):
            raise DexScreenerError("DexScreener retornou resposta em formato inesperado.")
        pairs = [pair for pair in result if isinstance(pair, dict) and pair.get("chainId") == "solana"]
        pairs = [pair for pair in pairs if str((pair.get("baseToken") or {}).get("address", "")) == query]
    else:
        params = urllib.parse.urlencode({"q": query})
        result = http_json(f"{DEX_BASE}/latest/dex/search?{params}", timeout=API_REQUEST_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            raise DexScreenerError("DexScreener retornou resposta em formato inesperado.")
        pairs = [pair for pair in (result.get("pairs") or []) if isinstance(pair, dict) and pair.get("chainId") == "solana"]
        matches = [pair for pair in pairs if query.casefold() in {
            str((pair.get("baseToken") or {}).get("symbol", "")).casefold(),
            str((pair.get("baseToken") or {}).get("name", "")).casefold(),
        }]
        pairs = matches or pairs
    pair = max(pairs, key=pair_liquidity, default=None)
    if pair:
        pair = dict(pair)
        pair["dataSource"] = "DexScreener"
    return pair


def get_market_data(query: str) -> dict[str, Any] | None:
    """Query GeckoTerminal first and fail over to DexScreener without affecting Telegram."""
    failures: list[str] = []
    try:
        result = get_gecko_pair(query)
        if result:
            return result
    except Exception as exc:
        failures.append(str(exc))
        LOG.warning("Consulta GeckoTerminal falhou; tentando DexScreener: %s", exc)
    try:
        result = get_dexscreener_pair(query)
        if result:
            return result
    except Exception as exc:
        failures.append(str(exc))
        LOG.warning("Consulta DexScreener de fallback falhou: %s", exc)
    if failures:
        raise MarketDataError("As fontes de mercado estão temporariamente indisponíveis.")
    return None


def pair_summary(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    change = (pair.get("priceChange") or {}).get("h24")
    change_text = percent(change)
    cap = pair.get("marketCap") or pair.get("fdv")
    return (f"{base.get('name') or base.get('symbol') or 'Token'} ({base.get('symbol') or 'Solana'})\n"
        f"Preço: {money(pair.get('priceUsd'))}\nVariação 24h: {change_text}\n"
        f"Liquidez: {compact((pair.get('liquidity') or {}).get('usd'))} · Volume 24h: {compact((pair.get('volume') or {}).get('h24'))}\n"
        f"Market cap/FDV: {compact(cap)}\nEndereço: {base.get('address', 'indisponível')}\nFonte: {pair.get('dataSource', 'DexScreener')} — {pair.get('url') or 'indisponível'}")


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
                    pair = get_market_data(arg)
                    send_message(token, chat_id, pair_summary(pair) if pair else "Token Solana não encontrado nas fontes de mercado.")
            elif command == "/watch":
                if not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /watch <endereço>")
                    return
                pair = get_market_data(arg)
                if not pair:
                    send_message(token, chat_id, "Não encontrei esse token nas fontes de mercado.")
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
                entry["name"] = base.get("name") or entry["symbol"]
                entry["last_price"] = pair.get("priceUsd")
                entry["last_change_24h"] = (pair.get("priceChange") or {}).get("h24")
                save_state(state)
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
                    save_state(state)
                    LOG.info("Token removido: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", arg)
                    send_message(token, chat_id, "Monitoramento removido.")
            elif command == "/list":
                entries = [e for e in watches.values() if str(chat_id) in e["subscribers"]]
                rows = []
                for entry in entries:
                    price = money(entry.get("last_price"))
                    change = entry.get("last_change_24h")
                    change_text = percent(change)
                    label = entry.get("name") or entry.get("symbol") or "Token"
                    symbol = entry.get("symbol")
                    if symbol and symbol != label:
                        label = f"{label} ({symbol})"
                    rows.append(f"• {label}\nPreço: {price} · 24h: {change_text}\nEndereço: {entry['address']} · Alerta: {threshold:g}%")
                if not rows:
                    send_message(token, chat_id, "📊 Seus tokens monitorados:\nVocê ainda não está monitorando nenhum token.")
                else:
                    heading = "📊 Seus tokens monitorados:\n"
                    chunks: list[str] = []
                    current = heading
                    for row in rows:
                        candidate = current + ("\n\n" if current != heading else "") + row
                        if len(candidate) > 3500 and current != heading:
                            chunks.append(current)
                            current = heading + row
                        else:
                            current = candidate
                    if current:
                        chunks.append(current)
                    for chunk in chunks:
                        send_message(token, chat_id, chunk)
            else:
                send_message(token, chat_id, help_text())
        except TelegramError:
            raise
        except MarketDataError as exc:
            LOG.warning("Consulta a fontes de mercado falhou em %s: %s", command, exc)
            send_message(token, chat_id, "Não consegui consultar o preço agora. As fontes de mercado estão temporariamente indisponíveis. Tente novamente em alguns segundos.")
        except Exception as exc:
            LOG.warning("Falha ao processar %s: %s", command, exc)
            send_message(token, chat_id, "Não consegui concluir o comando agora. Tente novamente mais tarde.")


def check_prices(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    with STATE_LOCK:
        addresses = [(key, entry["address"]) for key, entry in state["watches"].items()]
    for start in range(0, len(addresses), 30):
        if STOP.is_set():
            return
        batch = addresses[start:start + 30]
        batch_addresses = [address for _, address in batch]
        try:
            pairs = get_gecko_pairs(batch_addresses)
        except Exception as exc:
            LOG.warning("Consulta em lote ao GeckoTerminal falhou; tentando DexScreener para este lote: %s", exc)
            pairs = {}
        for key, address in batch:
            if STOP.is_set():
                return
            pair = pairs.get(address)
            if pair is None:
                try:
                    pair = get_dexscreener_pair(address)
                except Exception as exc:
                    LOG.warning("Fallback DexScreener falhou para token monitorado %s: %s", address, exc)
                    continue
            if not pair or not pair.get("priceUsd"):
                continue
            try:
                now = time.time()
                price = float(pair["priceUsd"])
                if not math.isfinite(price) or price <= 0:
                    continue
                base = pair.get("baseToken") or {}
                with STATE_LOCK:
                    entry = state["watches"].get(key)
                    if entry is None:
                        continue
                    entry["last_price"] = price
                    entry["last_change_24h"] = (pair.get("priceChange") or {}).get("h24")
                    entry["symbol"] = base.get("symbol") or base.get("name") or entry.get("symbol") or "Token"
                    entry["name"] = base.get("name") or entry.get("name") or entry["symbol"]
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
            except (TypeError, ValueError, OverflowError, KeyError) as exc:
                LOG.warning("Dados de preço inválidos para token monitorado %s; ignorando neste ciclo: %s", address, exc)


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
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token is None:
        LOG.error("Secret TELEGRAM_BOT_TOKEN ausente no ambiente do processo.")
        return 2
    if token == "":
        LOG.error("Secret TELEGRAM_BOT_TOKEN presente, mas vazio.")
        return 2
    if any(char.isspace() for char in token):
        LOG.error("TELEGRAM_BOT_TOKEN contém espaços ou quebras de linha; atualize o Secret sem espaços extras.")
        return 2
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        LOG.error("TELEGRAM_BOT_TOKEN malformado. Confira o Secret no GitHub sem expor seu valor.")
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

    # Validate credentials and remove webhook at boot; never retry rejected credentials forever.
    attempt = 0
    while not STOP.is_set():
        try:
            validate_telegram(token)
            break
        except TelegramUnauthorized as exc:
            LOG.error("TELEGRAM_BOT_TOKEN inválido ou rejeitado pelo Telegram. Confira/atualize o Secret TELEGRAM_BOT_TOKEN no GitHub. Detalhe seguro: %s", exc)
            return 2
        except TelegramError as exc:
            attempt += 1
            delay = min(300, 5 * (2 ** min(attempt - 1, 6)))
            LOG.error("Telegram indisponível na inicialização (tentativa %d); nova tentativa em %ss: %s", attempt, delay, exc)
            STOP.wait(delay)
    if STOP.is_set():
        return 0

    LOG.info("Worker contínuo iniciado; tokens monitorados: %d", len(state["watches"]))
    monitor = threading.Thread(target=market_monitor, args=(state, token, threshold, cooldown), name="market-monitor", daemon=True)
    monitor.start()
    failures = 0
    auth_rejected = False
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
                except TelegramUnauthorized as exc:
                    LOG.error("TELEGRAM_BOT_TOKEN inválido ou rejeitado pelo Telegram. Confira/atualize o Secret TELEGRAM_BOT_TOKEN no GitHub. Detalhe seguro: %s", exc)
                    auth_rejected = True
                    STOP.set()
                    break
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
        except TelegramUnauthorized as exc:
            LOG.error("TELEGRAM_BOT_TOKEN inválido ou rejeitado pelo Telegram. Confira/atualize o Secret TELEGRAM_BOT_TOKEN no GitHub. Detalhe seguro: %s", exc)
            auth_rejected = True
            STOP.set()
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
    return 2 if auth_rejected else 0


if __name__ == "__main__":
    sys.exit(main())
