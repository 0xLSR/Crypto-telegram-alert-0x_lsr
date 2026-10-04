"""Continuous Telegram crypto alert worker backed by GeckoTerminal and DexScreener."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import signal
import sys
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import wallet as solana_wallet
import nansen_client
import market_intelligence as intelligence
import market_flow as flow_analysis

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "data/state.json"))
TELEGRAM_POLL_TIMEOUT_SECONDS = 1
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 15
API_REQUEST_TIMEOUT_SECONDS = 15
PRICE_CHECK_SECONDS = max(15, int(os.getenv("PRICE_CHECK_SECONDS", "60")))
try:
    WALLET_CHECK_INTERVAL_SECONDS = max(15, int(os.getenv("WALLET_CHECK_INTERVAL_SECONDS", "60")))
except ValueError:
    WALLET_CHECK_INTERVAL_SECONDS = 60
MAX_WATCHES = 100
INTELLIGENCE_ENABLED = os.getenv("INTELLIGENCE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
SMART_ALERTS_ENABLED = os.getenv("SMART_ALERTS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
try:
    INTELLIGENCE_INTERVAL_SECONDS = max(15, min(3600, int(os.getenv("INTELLIGENCE_INTERVAL_SECONDS", "60"))))
except ValueError:
    INTELLIGENCE_INTERVAL_SECONDS = 60
try:
    HISTORY_RETENTION_HOURS = max(1, min(720, int(os.getenv("HISTORY_RETENTION_HOURS", "24"))))
except ValueError:
    HISTORY_RETENTION_HOURS = 24
try:
    MIN_INTELLIGENCE_SCORE = max(0, min(100, int(os.getenv("MIN_INTELLIGENCE_SCORE", "75"))))
except ValueError:
    MIN_INTELLIGENCE_SCORE = 75
PRICE_LOOKUPS: dict[str, str] = {}
INTELLIGENCE_WALLET_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
INTELLIGENCE_MARKET_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
LOG = logging.getLogger("crypto_alert_bot")
STATE_LOCK = threading.RLock()
STOP = threading.Event()
FLOW_CURSOR = 0
GECKO_RATE_LOCK = threading.Lock()
GECKO_LAST_REQUEST_AT = 0.0
try:
    FLOW_REQUESTS_PER_CYCLE = max(1, min(3, int(os.getenv("FLOW_REQUESTS_PER_CYCLE", "3"))))
except ValueError:
    FLOW_REQUESTS_PER_CYCLE = 3
try:
    SMART_FLOW_COOLDOWN_SECONDS = max(60, min(86400, int(os.getenv("SMART_FLOW_COOLDOWN_MINUTES", "30")) * 60))
except ValueError:
    SMART_FLOW_COOLDOWN_SECONDS = 1800
try:
    WHALE_FLOW_WEIGHT_PERCENT = max(0.0, min(20.0, float(os.getenv("WHALE_FLOW_WEIGHT_PERCENT", "10"))))
except ValueError:
    WHALE_FLOW_WEIGHT_PERCENT = 10.0


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
COMMAND_ALIASES.update({"/preco": "/price", "/monitorar": "/watch", "/adicionar": "/watch", "/remover": "/unwatch"})
COMMAND_ALIASES.update({"/analisar": "/analyze", "/analise": "/analyze", "/oportunidades": "/opportunities",
                        "/entrada": "/entry", "/saida": "/exit", "/scanner": "/scanner", "/performance": "/performance"})


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
        return {"offset": 0, "watches": {}, "awaiting_add": {}, "pending_add": {}, "wallets": {}, "awaiting_wallet": {},
                "market_history": {}, "signals": []}
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
            normalized.setdefault("symbol", "")
            normalized.setdefault("name", "")
            normalized.setdefault("anchor_price", 0)
            normalized.setdefault("timestamp", normalized.get("added_at", time.time()))
            normalized.setdefault("reference_price", normalized.get("anchor_price", 0))
            if "flow" in normalized:
                normalized["flow"] = flow_analysis.sanitize_flow(normalized.get("flow"))
            # Base58 addresses are case-sensitive; never lowercase their identity.
            watches[address] = normalized
        awaiting_add = value.get("awaiting_add", {})
        pending_add = value.get("pending_add", {})
        if not isinstance(awaiting_add, dict):
            awaiting_add = {}
        if not isinstance(pending_add, dict):
            pending_add = {}
        clean_pending = {}
        for chat_id, pending in pending_add.items():
            if isinstance(pending, dict) and is_valid_solana_address(pending.get("address")) and isinstance(pending.get("pair"), dict):
                clean_pending[str(chat_id)] = pending
        raw_wallets = value.get("wallets", {})
        if not isinstance(raw_wallets, dict):
            raw_wallets = {}
        wallets: dict[str, Any] = {}
        for user_id, entry in raw_wallets.items():
            if not isinstance(entry, dict) or not is_valid_solana_address(entry.get("address")):
                continue
            history = entry.get("history", [])
            clean_history = []
            if isinstance(history, list):
                for item in history[-20:]:
                    if not isinstance(item, dict) or not isinstance(item.get("signature"), str):
                        continue
                    assets = []
                    if isinstance(item.get("assets", []), list):
                        for asset in item.get("assets", [])[:3]:
                            if isinstance(asset, dict) and isinstance(asset.get("mint"), str) and isinstance(asset.get("amount"), str):
                                assets.append({"mint": asset["mint"], "amount": asset["amount"],
                                               "decimals": str(asset.get("decimals", "0"))})
                    clean_history.append({
                        "signature": item["signature"],
                        "classification": str(item.get("classification") or "🔄 Movimentação detectada"),
                        "timestamp": item.get("timestamp"),
                        "assets": assets,
                    })
            if not str(user_id).isdigit():
                continue
            wallets[str(user_id)] = {
                "address": entry["address"],
                "chat_id": str(entry.get("chat_id", "")),
                "monitoring_enabled": bool(entry.get("monitoring_enabled", True)),
                "alerts_enabled": bool(entry.get("alerts_enabled", True)),
                "last_signature": entry.get("last_signature") if isinstance(entry.get("last_signature"), str) else None,
                "last_check": entry.get("last_check") if isinstance(entry.get("last_check"), (int, float)) else None,
                "registered_at": entry.get("registered_at") if isinstance(entry.get("registered_at"), (int, float)) else time.time(),
                "history": clean_history,
            }
        awaiting_wallet = value.get("awaiting_wallet", {})
        if not isinstance(awaiting_wallet, dict):
            awaiting_wallet = {}
        safe_awaiting_wallet = {str(user_id): str(chat_id) for user_id, chat_id in awaiting_wallet.items() if str(user_id).isdigit()}
        raw_history = value.get("market_history", {})
        if not isinstance(raw_history, dict):
            raw_history = {}
        market_history: dict[str, list[dict[str, Any]]] = {}
        cutoff = time.time() - HISTORY_RETENTION_HOURS * 3600
        for address, rows in raw_history.items():
            if not is_valid_solana_address(address) or not isinstance(rows, list):
                continue
            safe_rows = []
            for row in rows[-int(HISTORY_RETENTION_HOURS * 3600 / 15):]:
                if not isinstance(row, dict):
                    continue
                timestamp = intelligence.number(row.get("timestamp"))
                price = intelligence.number(row.get("price"))
                if timestamp is None or price is None or price <= 0 or timestamp < cutoff:
                    continue
                safe_rows.append({key: intelligence.number(row.get(key)) for key in
                                  ("timestamp", "price", "volume_24h", "market_cap", "fdv", "liquidity", "price_change_24h")})
            if safe_rows:
                market_history[address] = safe_rows[-intelligence.MAX_SNAPSHOTS_PER_TOKEN:]
        raw_signals = value.get("signals", [])
        signals = []
        if isinstance(raw_signals, list):
            for signal_item in raw_signals[-500:]:
                if not isinstance(signal_item, dict) or not is_valid_solana_address(signal_item.get("address")):
                    continue
                timestamp = intelligence.number(signal_item.get("timestamp"))
                price = intelligence.number(signal_item.get("price"))
                if timestamp is None or price is None or price <= 0:
                    continue
                signals.append({"address": signal_item["address"], "timestamp": timestamp, "price": price,
                                "score": signal_item.get("score"), "entry_score": signal_item.get("entry_score"),
                                "exit_risk": signal_item.get("exit_risk"), "state": str(signal_item.get("state", ""))[:80],
                                "entry_type": str(signal_item.get("entry_type", ""))[:100],
                                "outcomes": signal_item.get("outcomes", {}) if isinstance(signal_item.get("outcomes"), dict) else {},
                                "max_after": intelligence.number(signal_item.get("max_after")) or price,
                                "min_after": intelligence.number(signal_item.get("min_after")) or price})
        return {"offset": offset, "watches": watches, "awaiting_add": awaiting_add, "pending_add": clean_pending,
                "wallets": wallets, "awaiting_wallet": safe_awaiting_wallet,
                "market_history": market_history, "signals": signals}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        LOG.error("Estado salvo inválido ou inacessível; iniciando sem offset avançado e sem watches: %s", exc)
        return {"offset": 0, "watches": {}, "awaiting_add": {}, "pending_add": {}, "wallets": {}, "awaiting_wallet": {},
                "market_history": {}, "signals": []}


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
    global GECKO_LAST_REQUEST_AT
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": "crypto-telegram-alert/1.0", "Content-Type": "application/json"}
    if urllib.parse.urlsplit(url).hostname == "api.geckoterminal.com":
        headers["Accept"] = "application/json;version=20230203"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    service, error_type = service_for_url(url)
    try:
        def read_response() -> str:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        if urllib.parse.urlsplit(url).hostname == "api.geckoterminal.com":
            # GeckoTerminal's unauthenticated public API allows about 10 req/min.
            # Serialize requests and leave a small safety margin for clock/network jitter.
            with GECKO_RATE_LOCK:
                delay = 6.25 - (time.monotonic() - GECKO_LAST_REQUEST_AT)
                if GECKO_LAST_REQUEST_AT and delay > 0:
                    time.sleep(delay)
                GECKO_LAST_REQUEST_AT = time.monotonic()
                response_body = read_response()
        else:
            response_body = read_response()
        return json.loads(response_body)
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


def configure_bot_commands(token: str) -> None:
    commands = [
        {"command": "start", "description": "Abrir menu"},
        {"command": "price", "description": "Consultar preço"},
        {"command": "list", "description": "Minha lista"},
        {"command": "watch", "description": "Adicionar alerta"},
        {"command": "unwatch", "description": "Remover alerta"},
        {"command": "help", "description": "Ajuda"},
    ]
    telegram(token, "setMyCommands", {"commands": commands})


def send_message(token: str, chat_id: int | str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram(token, "sendMessage", payload)


def edit_message(token: str, chat_id: int | str, message_id: int, text: str,
                 reply_markup: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram(token, "editMessageText", payload)


def answer_callback(token: str, callback_id: str, text: str | None = None) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    telegram(token, "answerCallbackQuery", payload)


def money(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    if not number.is_finite():
        return "indisponível"
    if number == 0:
        return "$0.00"
    exponent = number.copy_abs().adjusted() - 7
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits) + abs(exponent) + 2)
        rounded = number.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_UP)
    rendered = format(rounded, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if abs(number) >= 1 and "." not in rendered:
        return f"${Decimal(rendered):,.0f}"
    if abs(number) >= 1 and len(rendered.split(".")[-1]) < 2:
        rendered += "0" * (2 - len(rendered.split(".")[-1]))
    return f"${rendered}"


def compact(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    if not number.is_finite():
        return "indisponível"
    absolute = abs(number)
    if absolute >= Decimal("1e9"):
        scaled, suffix = number / Decimal("1e9"), "B"
    elif absolute >= Decimal("1e6"):
        scaled, suffix = number / Decimal("1e6"), "M"
    elif absolute >= Decimal("1e3"):
        scaled, suffix = number / Decimal("1e3"), "K"
    else:
        return f"${number.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,.0f}"
    rendered = f"{scaled.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".")
    return f"${rendered}{suffix}"


def percent(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    if not number.is_finite():
        return "indisponível"
    if abs(number) < Decimal("0.005"):
        return "⚪ 0.00%"
    icon = "🟢🤑" if number > 0 else "🔴🫠"
    return f"{icon} {number:+.2f}%"


def movement_icon(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "⚪"
    if not number.is_finite() or number == 0:
        return "⚪"
    return "🟢🤑" if number > 0 else "🔴🫠"


def signed_percent(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    if not number.is_finite():
        return "indisponível"
    return f"{number:+.2f}%"


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
        "geckoUrl": f"https://www.geckoterminal.com/solana/pools/{address_pool}" if address_pool else f"https://www.geckoterminal.com/solana/tokens/{address}",
        "dexUrl": f"https://dexscreener.com/solana/{address_pool}" if address_pool else None,
        "poolAddress": address_pool,
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


def get_gecko_trades(pool_address: str, token_address: str) -> list[dict[str, Any]]:
    """Fetch the public latest-trades endpoint oriented to this Solana mint."""
    pool = urllib.parse.quote(pool_address, safe="")
    mint = urllib.parse.quote(token_address, safe="")
    response = http_json(f"{GECKO_BASE}/networks/solana/pools/{pool}/trades?token={mint}",
                         timeout=API_REQUEST_TIMEOUT_SECONDS)
    try:
        return flow_analysis.parse_trades(response, token_address)
    except ValueError as exc:
        raise GeckoTerminalError(str(exc)) from exc


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
        pair["dexUrl"] = pair.get("url")
        token_address = (pair.get("baseToken") or {}).get("address")
        if token_address:
            pair["geckoUrl"] = f"https://www.geckoterminal.com/solana/tokens/{token_address}"
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


def pair_summary(pair: dict[str, Any], alert_change: Any | None = None) -> str:
    base = pair.get("baseToken") or {}
    change = (pair.get("priceChange") or {}).get("h24")
    change_text = percent(change)
    market_cap = pair.get("marketCap")
    fdv = pair.get("fdv")
    cap_label = "💎 Capitalização" if market_cap is not None else "💎 FDV"
    cap_value = market_cap if market_cap is not None else fdv
    name = base.get("name") or base.get("symbol") or "Token Solana"
    symbol = base.get("symbol")
    icon = movement_icon(alert_change) if alert_change is not None else "💰"
    title = f"{icon} {name}" + (f"\n${symbol}" if symbol and symbol.casefold() != str(name).casefold() else "")
    heading = f"━━━━━━━━━━━━━━━━━━━━\n\n{title}\n\n━━━━━━━━━━━━━━━━━━━━\n\n" if alert_change is not None else f"{title}\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    result = (f"{heading}"
        f"💰 Preço\n{money(pair.get('priceUsd'))}\n\n"
        f"📈 Variação 24h\n{change_text}\n\n"
        f"{cap_label}\n{compact(cap_value)}")
    if market_cap is not None and fdv is not None:
        result += f"\n\n📉 FDV\n{compact(fdv)}"
    result += (f"\n\n💧 Liquidez\n{compact((pair.get('liquidity') or {}).get('usd'))}"
        f"\n\n📊 VOLUME 24H\n{compact((pair.get('volume') or {}).get('h24'))}"
        f"\n\n━━━━━━━━━━━━━━━━━━━━\n⛓️ Solana\n\n📍 Contrato\n{short_address(base.get('address'))}\n\nAtualizado agora.")
    return result


def short_address(value: Any) -> str:
    address = str(value or "indisponível")
    return address if len(address) <= 12 else f"{address[:5]}...{address[-5:]}"


def pair_links_markup(pair: dict[str, Any]) -> dict[str, Any] | None:
    rows = []
    pair_url = pair.get("url")
    token_address = (pair.get("baseToken") or {}).get("address")
    dex_url = pair.get("dexUrl") or (pair_url if isinstance(pair_url, str) and pair_url.startswith("https://dexscreener.com/") else None)
    gecko_url = pair.get("geckoUrl") or (pair_url if isinstance(pair_url, str) and pair_url.startswith("https://www.geckoterminal.com/") else None)
    if not gecko_url and token_address:
        gecko_url = f"https://www.geckoterminal.com/solana/tokens/{urllib.parse.quote(str(token_address), safe='')}"
    if isinstance(dex_url, str) and dex_url.startswith("https://dexscreener.com/"):
        rows.append({"text": "📊 DexScreener", "url": dex_url})
    if isinstance(gecko_url, str) and gecko_url.startswith("https://www.geckoterminal.com/"):
        rows.append({"text": "🔍 GeckoTerminal", "url": gecko_url})
    return {"inline_keyboard": [rows]} if rows else None


def wallet_key(user_id: str | int) -> str:
    return str(user_id)


def current_wallet(state: dict[str, Any], user_id: str | int, chat_id: str | int) -> dict[str, Any] | None:
    with STATE_LOCK:
        wallet = state.setdefault("wallets", {}).get(wallet_key(user_id))
        if wallet and wallet.get("chat_id") == str(chat_id):
            return dict(wallet)
    # Optional public address configured by the repository owner. It is read-only
    # and is not inserted into per-user state or used to send wallet alerts.
    configured = os.getenv("NANSEN_WALLET_ADDRESS", "").strip()
    if is_valid_solana_address(configured):
        return {"address": configured, "chat_id": str(chat_id), "monitoring_enabled": False,
                "configured_default": True}
    return None


def wallet_short_address(address: str) -> str:
    return short_address(address)


def wallet_amount(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    if not number.is_finite():
        return "indisponível"
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")


def wallet_usd(value: Any) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    return f"${amount:,.2f}"


def wallet_percent(value: Any) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "indisponível"
    return format(amount, ".2f")


def wallet_token_position(address: str, mint: str) -> tuple[Decimal, int, dict[str, Any] | None]:
    """Prefer normalized provider holdings; retain the public RPC balance fallback."""
    if os.getenv("NANSEN_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"} and os.getenv("NANSEN_API_KEY", "").strip():
        try:
            info = solana_wallet.get_wallet_info(address, get_market_data, get_gecko_pairs)
            for item in info.get("tokens", []):
                if item.get("mint") == mint:
                    pnl = None
                    if info.get("source") == "nansen":
                        try:
                            pnl = nansen_client.get_token_pnl(address, mint)
                        except nansen_client.NansenError as exc:
                            LOG.info("[WALLET] PnL indisponível para mint %s: %s", wallet_short_address(mint), exc)
                    if pnl:
                        item.update(pnl)
                        if item.get("pnl_percent") is not None:
                            try:
                                item["pnl_percent"] = str(Decimal(str(item["pnl_percent"])) * 100)
                            except (InvalidOperation, TypeError, ValueError):
                                item["pnl_percent"] = None
                    return Decimal(str(item["amount"])), int(item.get("decimals", 0)), item
            return Decimal(0), 0, None
        except (solana_wallet.WalletRpcError, nansen_client.NansenError, InvalidOperation, TypeError, ValueError):
            LOG.info("[WALLET] Posição Nansen indisponível para o mint %s; tentando RPC", wallet_short_address(mint))
    amount, decimals = solana_wallet.get_token_balance(address, mint)
    return amount, decimals, None


def wallet_token_amount(address: str, mint: str) -> tuple[Decimal, int]:
    amount, decimals, _item = wallet_token_position(address, mint)
    return amount, decimals


def wallet_markup(wallet: dict[str, Any] | None, *, detail_page: int | None = None, page_count: int = 0) -> dict[str, Any]:
    if not wallet:
        return {"inline_keyboard": [[button("➕ Cadastrar carteira", "wallet_add")], [button("⬅️ Voltar", "menu")]]}
    if wallet.get("configured_default"):
        return {"inline_keyboard": [[button("📊 Ver carteira", "wallet_view"), button("🔄 Atualizar", "wallet_refresh")],
                                     [button("➕ Usar outra carteira", "wallet_add")], [button("⬅️ Voltar", "menu")]]}
    rows = []
    if detail_page is not None and page_count > 1:
        nav = []
        if detail_page > 0:
            nav.append(button("⬅️ Anterior", f"wallet_tokens:{detail_page - 1}"))
        nav.append({"text": f"{detail_page + 1}/{page_count}", "callback_data": f"wallet_tokens:{detail_page}"})
        if detail_page + 1 < page_count:
            nav.append(button("Próxima ➡️", f"wallet_tokens:{detail_page + 1}"))
        rows.append(nav)
    rows.extend([
        [button("📊 Ver carteira", "wallet_view"), button("🔄 Atualizar", "wallet_refresh")],
        [button("📜 Histórico", "wallet_history"), button("🔔 Alertas da carteira", "wallet_alerts")],
        [button("🗑️ Remover carteira", "wallet_remove")],
        [button("⬅️ Voltar", "menu")],
    ])
    return {"inline_keyboard": rows}


WALLET_DETAIL_PAGE_SIZE = 6


def wallet_info_text(address: str, info: dict[str, Any], wallet: dict[str, Any], *, details: bool = False,
                     detail_page: int = 0) -> str:
    status = ("🔒 Endereço padrão · somente leitura" if wallet.get("configured_default") else
              "🟢 Monitoramento ativo" if wallet.get("monitoring_enabled", True) else "⏸️ Monitoramento pausado")
    updated = time.strftime("%H:%M:%S", time.localtime())
    text = ("💼 MINHA CARTEIRA\n\n"
            f"Status: {status}\n\nCarteira: {wallet_short_address(address)}\n\n"
            f"Fonte: {'Nansen Profiler' if info.get('source') == 'nansen' else 'RPC Solana'}\n")
    if info.get("sol") is not None:
        text += f"💰 Saldo SOL: {wallet_amount(info.get('sol'))} SOL\n"
    sol_price = info.get("sol_price_usd")
    if sol_price is not None:
        text += f"Preço SOL: {wallet_usd(sol_price)}\n"
    estimated = info.get("estimated_usd")
    sol_value = info.get("sol_value_usd")
    if sol_value is not None:
        text += f"Valor SOL: {wallet_usd(sol_value)}\n"
    total = info.get("portfolio_total_usd") if info.get("portfolio_total_usd") is not None else estimated
    text += f"\n💵 PATRIMÔNIO ESTIMADO: {wallet_usd(total) if total is not None else 'indisponível'}\n"
    pnl_summary = info.get("pnl_summary") or {}
    if pnl_summary.get("realized_pnl_usd") is not None:
        text += f"📊 PnL realizado (90 dias): {wallet_usd(pnl_summary['realized_pnl_usd'])}\n"
    text += f"🪙 Tokens: {int(info.get('token_count', 0))} ativos\n"
    unpriced = int(info.get("unpriced_tokens", 0) or 0)
    if unpriced:
        priced_count = max(0, int(info.get("token_count", 0)) - unpriced)
        text += f"\n⚠️ {unpriced} token(s) sem cotação\nValor considera SOL e {priced_count} token(s) com preço disponível.\n"
    elif info.get("is_partial"):
        text += "\n⚠️ Estimativa parcial: preço do SOL indisponível.\n"
    if details:
        tokens = info.get("tokens", [])
        if tokens:
            page_count = max(1, (len(tokens) + WALLET_DETAIL_PAGE_SIZE - 1) // WALLET_DETAIL_PAGE_SIZE)
            detail_page = min(max(0, detail_page), page_count - 1)
            page_tokens = tokens[detail_page * WALLET_DETAIL_PAGE_SIZE:(detail_page + 1) * WALLET_DETAIL_PAGE_SIZE]
            text += "\n🪙 ATIVOS DA CARTEIRA\n"
            for index, item in enumerate(page_tokens, start=detail_page * WALLET_DETAIL_PAGE_SIZE + 1):
                label = item.get("symbol") or item.get("name") or "Token não identificado"
                text += f"\n{index}. {label}\nQuantidade: {wallet_amount(item.get('amount'))}\n"
                if item.get("price_usd") is not None:
                    text += f"Preço: {money(item['price_usd'])}\nValor: {wallet_usd(item.get('value_usd'))}\n"
                else:
                    text += "Preço: indisponível\nValor: indisponível\n"
                if item.get("price_change_24h") is not None:
                    text += f"Variação 24h: {signed_percent(item['price_change_24h'])}\n"
                if item.get("allocation_percent") is not None:
                    text += f"Participação: {wallet_percent(item['allocation_percent'])}%\n"
            if page_count > 1:
                text += f"\nPágina {detail_page + 1} de {page_count}."
        else:
            text += "\nNenhum token SPL ativo encontrado.\n"
        text += "\n📊 PnL\n"
        if pnl_summary.get("realized_pnl_usd") is not None:
            text += f"PnL agregado realizado (90 dias): {wallet_usd(pnl_summary['realized_pnl_usd'])}\n"
        else:
            text += "PnL por posição/custo de aquisição: indisponível\n"
    checked = wallet.get("last_check")
    if isinstance(checked, (int, float)):
        updated = time.strftime("%H:%M:%S", time.localtime(checked))
    return text + f"\nÚltima atualização: {updated}"


def wallet_menu_text(state: dict[str, Any], user_id: str | int, chat_id: str | int) -> tuple[str, dict[str, Any] | None]:
    wallet = current_wallet(state, user_id, chat_id)
    if not wallet:
        return ("💼 MINHA CARTEIRA\n\nNenhuma carteira cadastrada.\n\n"
                "Cadastre o endereço público da sua carteira Solana para começar o monitoramento.", None)
    LOG.info("[WALLET] Checking wallet %s", wallet_short_address(wallet["address"]))
    info = solana_wallet.get_wallet_info(wallet["address"], get_market_data, get_gecko_pairs)
    return wallet_info_text(wallet["address"], info, wallet), wallet


def wallet_history_screen(wallet: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if not wallet:
        return "📜 HISTÓRICO DA CARTEIRA\n\nNenhuma carteira cadastrada.", back_markup("wallet_menu")
    items = list(wallet.get("history", []))[:10]
    if not items:
        return "📜 HISTÓRICO DA CARTEIRA\n\nNenhuma movimentação recente foi encontrada.", back_markup("wallet_menu")
    lines = []
    buttons = []
    for item in items:
        classification = item.get("classification") or "🔄 Movimentação detectada"
        timestamp = item.get("timestamp")
        ago = "horário indisponível"
        if isinstance(timestamp, (int, float)):
            seconds = max(0, int(time.time() - timestamp))
            ago = f"há {seconds // 60} min" if seconds < 3600 else f"há {seconds // 3600}h"
        assets = item.get("assets") or []
        token_label = "Token não identificado"
        if assets:
            token_label = f"Token {wallet_short_address(assets[0].get('mint', ''))} · {wallet_amount(assets[0].get('amount'))}"
        lines.append(f"{classification}\n{token_label}\n💵 Valor: indisponível · {ago}")
        signature = item.get("signature")
        if isinstance(signature, str) and signature:
            buttons.append({"text": "🔎 Ver", "url": f"https://solscan.io/tx/{urllib.parse.quote(signature, safe='')}"})
    markup_rows = [[button] for button in buttons]
    markup_rows.append([{"text": "⬅️ Voltar", "callback_data": "wallet_menu"}])
    return "📜 HISTÓRICO DA CARTEIRA\n\n" + "\n\n".join(lines), {"inline_keyboard": markup_rows}


def token_callback_id(address: str) -> str:
    """Compact, non-secret callback identifier; full addresses stay in state.json."""
    return hashlib.blake2s(address.encode("utf-8"), digest_size=6).hexdigest()


def watch_entries(state: dict[str, Any], chat_id: int | str) -> list[dict[str, Any]]:
    with STATE_LOCK:
        return [dict(entry) for entry in state.get("watches", {}).values() if str(chat_id) in entry.get("subscribers", [])]


def watch_for_callback(state: dict[str, Any], chat_id: int | str, callback_id: str) -> dict[str, Any] | None:
    with STATE_LOCK:
        return next((entry for entry in state.get("watches", {}).values()
                     if str(chat_id) in entry.get("subscribers", [])
                     and token_callback_id(entry.get("address", "")) == callback_id), None)


def button(text: str, callback: str) -> dict[str, str]:
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("callback_data excedeu o limite do Telegram")
    return {"text": text, "callback_data": callback}


def main_menu_markup() -> dict[str, Any]:
    return {"inline_keyboard": [
        [button("💰 Consultar preço", "price_menu"), button("🔔 Meus alertas", "alerts")],
        [button("➕ Adicionar token", "add_begin"), button("📋 Minha lista", "watch_list")],
        [button("🔎 Scanner", "intel_scanner"), button("🧠 Analisar token", "intel_analyze_menu")],
        [button("🔥 Oportunidades", "intel_opportunities"), button("📈 Setup de entrada", "intel_entry_menu")],
        [button("🚨 Setup de saída", "intel_exit_menu"), button("📊 Performance", "intel_performance")],
        [button("💼 Minha carteira", "wallet_menu")],
        [button("ℹ️ Ajuda", "help")],
    ]}


def list_actions_markup() -> dict[str, Any]:
    return {"inline_keyboard": [[button("➕ Adicionar token", "add_begin"), button("❌ Remover token", "remove_menu")],
                                [button("◀️ Voltar", "menu")]]}


def back_markup(callback: str = "menu") -> dict[str, Any]:
    return {"inline_keyboard": [[button("◀️ Voltar", callback)]]}


def token_details_markup(pair: dict[str, Any], address: str, back: str = "price_menu") -> dict[str, Any]:
    lookup_id = token_callback_id(address)
    PRICE_LOOKUPS[lookup_id] = address
    if len(PRICE_LOOKUPS) > 500:
        PRICE_LOOKUPS.pop(next(iter(PRICE_LOOKUPS)))
    origin = {"watch_list": "l", "price_menu": "p", "alerts": "a"}.get(back, "d")
    rows = [[button("🔄 Atualizar", f"u:{lookup_id}:{origin}"),
             button("◀️ Voltar", back)]]
    links = pair_links_markup(pair)
    urls = (links or {}).get("inline_keyboard", [[]])[0]
    if urls:
        urls[0] = {**urls[0], "text": "📊 Gráfico"}
        rows.insert(0, urls)
    return {"inline_keyboard": rows}


def price_menu_markup(state: dict[str, Any], chat_id: int | str, page: int = 0) -> tuple[str, dict[str, Any]]:
    entries = watch_entries(state, chat_id)
    if not entries:
        return ("📊 CONSULTAR PREÇO\n\nSua lista ainda está vazia.",
                {"inline_keyboard": [[button("➕ Adicionar token", "add_begin")], [button("◀️ Voltar", "menu")]]})
    page_size = 10
    page = max(0, min(page, (len(entries) - 1) // page_size))
    start = page * page_size
    rows = []
    for entry in entries[start:start + page_size]:
        symbol = entry.get("symbol") or entry.get("name") or "TOKEN"
        rows.append([button(f"{change_icon(entry.get('last_change_24h'))} {symbol}"[:64], f"p:{token_callback_id(entry['address'])}")])
    rows.append([button("🔄 Atualizar", f"p_refresh:{page}")])
    nav = []
    if page > 0:
        nav.append(button("⬅️", f"p_page:{page - 1}"))
    if start + page_size < len(entries):
        nav.append(button("➡️", f"p_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("◀️ Voltar", "menu")])
    return "📊 CONSULTAR PREÇO\n\nSelecione um token:", {"inline_keyboard": rows}


def remove_menu_markup(state: dict[str, Any], chat_id: int | str, page: int = 0) -> tuple[str, dict[str, Any]]:
    entries = watch_entries(state, chat_id)
    if not entries:
        return "❌ REMOVER TOKEN\n\nSua lista ainda está vazia.", {"inline_keyboard": [[button("◀️ Voltar", "alerts")]]}
    page_size = 10
    page = max(0, min(page, (len(entries) - 1) // page_size))
    start = page * page_size
    rows = []
    for entry in entries[start:start + page_size]:
        symbol = entry.get("symbol") or entry.get("name") or "TOKEN"
        rows.append([button(f"❌ {symbol}"[:64], f"r:{token_callback_id(entry['address'])}")])
    nav = []
    if page > 0:
        nav.append(button("⬅️", f"r_page:{page - 1}"))
    if start + page_size < len(entries):
        nav.append(button("➡️", f"r_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("◀️ Voltar", "alerts")])
    return "❌ REMOVER TOKEN\n\nSelecione o token que deseja remover:", {"inline_keyboard": rows}


def start_text() -> str:
    return ("🚀 0x_LSR CRYPTO ALERTS\n\nSeu painel de tokens Solana\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💰 Consultar preço\n🔔 Meus alertas\n➕ Adicionar token\n📋 Minha lista\n\n"
            "🧠 INTELIGÊNCIA\n🔎 Scanner · 🧠 Analisar token\n🔥 Oportunidades · 📈 Setup de entrada · 🚨 Setup de saída\n\n"
            "💼 Minha carteira · 📊 Performance\n\nℹ️ Ajuda")


def intelligence_token_markup(entries: list[dict[str, Any]], action: str) -> dict[str, Any]:
    rows = []
    for entry in entries[:100]:
        address = entry.get("address")
        if not is_valid_solana_address(address):
            continue
        symbol = entry.get("symbol") or entry.get("name") or "TOKEN"
        rows.append([button(f"{symbol}"[:64], f"{action}:{address}")])
    rows.append([button("◀️ Voltar", "menu")])
    return {"inline_keyboard": rows}


def intelligence_pair(entry: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = snapshots[-1] if snapshots else {}
    price = latest.get("price", entry.get("last_price"))
    if not price:
        return None
    return {"priceUsd": str(price), "baseToken": {"address": entry.get("address"),
            "symbol": entry.get("symbol"), "name": entry.get("name")},
            "priceChange": {"h24": latest.get("price_change_24h", entry.get("last_change_24h"))},
            "volume": {"h24": latest.get("volume_24h")}, "marketCap": latest.get("market_cap", entry.get("market_cap")),
            "fdv": latest.get("fdv", entry.get("fdv")), "liquidity": {"usd": latest.get("liquidity")}}


def get_intelligence_market_data(address: str) -> dict[str, Any] | None:
    cached = INTELLIGENCE_MARKET_CACHE.get(address)
    now = time.monotonic()
    if cached and now - cached[0] < 60:
        return cached[1]
    pair = get_market_data(address)
    INTELLIGENCE_MARKET_CACHE[address] = (now, pair)
    return pair


def intelligence_analysis_text(address: str, entry: dict[str, Any], result: dict[str, Any],
                               pair: dict[str, Any] | None, mode: str = "analysis") -> str:
    base = (pair or {}).get("baseToken") or {}
    label = base.get("symbol") or base.get("name") or entry.get("symbol") or entry.get("name") or "TOKEN"
    metrics = result.get("metrics", {})
    score = result.get("score")
    entry_score = result.get("entry_score")
    exit_risk = result.get("exit_risk")
    confidence = result.get("confidence", 0)
    if mode == "entry":
        title = "📈 SETUP DE ENTRADA"
    elif mode == "exit":
        title = "🚨 RISCO DE SAÍDA"
    else:
        title = "🧠 ANÁLISE"
    lines = [title, "", str(label), f"\n💰 Preço: {money((pair or {}).get('priceUsd') or metrics.get('price'))}"]
    if metrics.get("market_cap") is not None:
        lines.append(f"📊 Market Cap: {compact(metrics['market_cap'])}")
    if metrics.get("liquidity") is not None:
        lines.append(f"💧 Liquidez: {compact(metrics['liquidity'])}")
    if metrics.get("volume_24h") is not None:
        lines.append(f"📊 Volume 24h: {compact(metrics['volume_24h'])}")
    change = (pair or {}).get("priceChange", {}).get("h24")
    if change is not None:
        lines.append(f"📈 24h: {signed_percent(change)}")
    flow_state = entry.get("flow") if isinstance(entry.get("flow"), dict) else {}
    flow_summary = flow_state.get("summary") if isinstance(flow_state.get("summary"), dict) else {}
    if flow_summary.get("available"):
        lines += ["", "🐋 FLUXO DE MERCADO"]
        for period in ("5m", "15m", "30m", "1h", "4h", "24h"):
            window = flow_summary.get("windows", {}).get(period)
            if not isinstance(window, dict):
                continue
            if not window.get("known_trades"):
                lines.append(f"{period}: ⚪ {window.get('unknown_trades', 0)} operações sem BUY/SELL confiável")
                continue
            lines.append(f"{period}: compras {wallet_usd(window['buy_usd'])} · vendas {wallet_usd(window['sell_usd'])} · líquido {wallet_usd(window['net_usd'])}")
        if flow_state.get("possibly_truncated"):
            lines.append("⚠️ Fonte devolveu 300 trades; períodos maiores podem estar incompletos.")
        divergence_names = {"price_up_buying": "🟢 possível confirmação comprador/preço",
                            "price_up_selling": "⚠️ divergência: preço em alta e fluxo vendedor",
                            "price_down_buying": "🟡 possível absorção: preço em queda e fluxo comprador",
                            "price_down_selling": "🔴 pressão vendedora observada"}
        if flow_summary.get("divergence") in divergence_names:
            lines.append(divergence_names[flow_summary["divergence"]])
    elif flow_state:
        lines += ["", "🐋 FLUXO DE MERCADO", "Dados de trades temporariamente indisponíveis; fluxo não pontuado."]
    lines += ["", "━━━━━━━━━━━━━━━━━━━━", "", f"🧠 Estado: {result.get('state', '⚪ SEM DADOS')}",
              f"Score: {score if score is not None else '—'}/100"]
    if score is not None:
        score_label = ("🚨 Excepcional" if score >= 90 else "🔥 Forte" if score >= 80 else
                       "🟢 Interessante" if score >= 65 else "🟡 Neutro" if score >= 50 else
                       "🟠 Baixo" if score >= 30 else "🔴 Fraco")
        lines.append(score_label)
    if entry_score is not None:
        entry_label = ("🚨 Excepcional" if entry_score >= 90 else "🔥 Forte" if entry_score >= 80 else
                       "🟢 Interessante" if entry_score >= 65 else "🟡 Neutro" if entry_score >= 50 else
                       "🟠 Baixo" if entry_score >= 30 else "🔴 Fraco")
        lines.append(f"Entry Score: {entry_score}/100 · {entry_label}")
    if exit_risk is not None:
        lines.append(f"Exit Risk: {exit_risk}/100")
        exit_label = ("🚨 Estrutura deteriorada" if exit_risk >= 85 else "🔴 Risco elevado" if exit_risk >= 70 else
                      "🟠 Risco crescente" if exit_risk >= 50 else "🟡 Atenção" if exit_risk >= 30 else "🟢 Estrutura saudável")
        exit_action = ("🔴 Saída/invalidação a considerar" if exit_risk >= 70 else
                       "🟠 Reduzir risco" if exit_risk >= 50 else "🟡 Atenção" if exit_risk >= 30 else "🟢 Segurar / estrutura saudável")
        lines.extend([exit_label, exit_action])
    lines.append(f"Confiança dos dados: {confidence}% (não representa probabilidade de lucro)")
    component_names = {"momentum": "Momentum", "volume": "Volume", "structure": "Estrutura",
                       "liquidity": "Liquidez", "volatility": "Volatilidade", "volume_acceleration": "Aceleração de volume",
                       "price_acceleration": "Aceleração de preço", "turnover": "Volume / market cap",
                       "market_cap": "Market cap", "fdv_overhang": "Relação FDV / market cap",
                       "consistency": "Consistência do movimento", "whale_flow": "Whale Flow"}
    if result.get("components"):
        lines.append("\nComponentes disponíveis:")
        lines.extend(f"• {component_names.get(key, key)}: {round(value)}/100" for key, value in result["components"].items())
    for label_key, metric_key in (("5m", "5m"), ("15m", "15m"), ("30m", "30m"), ("1h", "1h"), ("4h", "4h"), ("24h", "24h")):
        value = metrics.get("return_" + metric_key)
        if value is not None:
            lines.append(f"{label_key}: {signed_percent(value)}")
    if result.get("insufficient"):
        lines.append("\n⏳ Dados insuficientes para análise completa.")
    entry_setup = result.get("entry", {})
    lines += ["\n━━━━━━━━━━━━━━━━━━━━", "", f"📈 Setup: {entry_setup.get('type', 'Dados insuficientes')}"]
    zone = entry_setup.get("zone")
    if zone:
        lines.append(f"📍 Zona observada: {money(zone[0])} — {money(zone[1])}")
    if entry_setup.get("confirmation") is not None:
        lines.append(f"✅ Confirmação observada: {money(entry_setup['confirmation'])}")
    if entry_setup.get("invalidation") is not None:
        lines.append(f"🛑 Invalidação (mínima recente): {money(entry_setup['invalidation'])}")
    if entry_setup.get("tp1") is not None:
        lines.append(f"🎯 Resistência/TP1 observada: {money(entry_setup['tp1'])}")
        if entry_setup.get("risk_reward") is not None:
            lines.append(f"📐 R/R até TP1: 1:{entry_setup['risk_reward']:.2f}")
    else:
        lines.append("🎯 Alvos: dados insuficientes para níveis observados confiáveis.")
    if entry_setup.get("late"):
        lines += ["\n⚠️ ENTRADA TARDIA", "Movimento estendido; considere aguardar consolidação/pullback."]
    if result.get("exit_signals"):
        lines += ["\n⚠️ Riscos:"] + ["• " + reason for reason in result["exit_signals"]]
    elif metrics.get("liquidity") is not None and metrics["liquidity"] < intelligence.MIN_LIQUIDITY_USD:
        lines.append("\n☠️ ALTO RISCO: liquidez muito baixa.")
    if metrics.get("liquidity") is not None and metrics["liquidity"] >= intelligence.MIN_LIQUIDITY_USD:
        risk_label = "ALTO" if metrics["liquidity"] < 20_000 or (metrics.get("volatility_pct") or 0) > 20 else "MODERADO"
        lines.append(f"⚠️ Risco estimado do ativo: {risk_label} (heurística de liquidez/volatilidade).")
    if result.get("reasons"):
        lines += ["\nContexto:"] + ["• " + reason for reason in result["reasons"][:4]]
    if exit_risk is not None and exit_risk >= 70:
        conclusion = "Estrutura deteriorada; risco de saída elevado, sem instrução automática de venda."
    elif entry_setup.get("late"):
        conclusion = "Movimento estendido; risco de entrada tardia e possibilidade de correção."
    elif result.get("state") == "🔥 BREAKOUT":
        conclusion = "Rompimento com confluência de volume observado; continuidade não é garantida."
    elif result.get("state") == "🟢 MOMENTUM":
        conclusion = "Momentum positivo observado; aguarde confirmação e avalie o risco."
    else:
        conclusion = "Sinais mistos ou incompletos; não há confirmação suficiente para um setup forte."
    lines.append(f"\nConclusão: {conclusion} Score e confiança não são probabilidade de lucro.")
    return "\n".join(lines)[:3900]


def intelligence_scan_text(entries: list[dict[str, Any]], history: dict[str, list[dict[str, Any]]],
                            *, opportunities: bool = False) -> tuple[str, dict[str, Any]]:
    ranked = []
    for entry in entries:
        address = entry.get("address", "")
        snapshots = history.get(address, [])
        pair = entry.get("_market_pair") or intelligence_pair(entry, snapshots)
        if pair is None:
            continue
        latest_timestamp = snapshots[-1].get("timestamp") if snapshots else None
        entry_flow = (entry.get("flow") or {}).get("summary") if isinstance(entry.get("flow"), dict) else None
        result = intelligence.analyze(snapshots, pair, timestamp=latest_timestamp, flow=entry_flow,
                                      whale_flow_weight=WHALE_FLOW_WEIGHT_PERCENT)
        if result.get("score") is None:
            continue
        if opportunities and (result["score"] < MIN_INTELLIGENCE_SCORE or result.get("insufficient")):
            continue
        ranked.append((result["score"], entry, pair, result))
    ranked.sort(key=lambda item: item[0], reverse=True)
    ranked = ranked[:5]
    if not ranked:
        prefix = "🔥 OPORTUNIDADES" if opportunities else "🔎 SCANNER"
        return (f"{prefix}\n\nNenhum token monitorado tem dados suficientes neste momento.\n"
                "Adicione tokens à sua lista; o histórico começa a ser coletado pelo monitor normal."), back_markup()
    heading = "🔥 OPORTUNIDADES" if opportunities else "🔎 SCANNER"
    lines = [heading, "", "Ativos monitorados com maior confluência segundo o modelo. Não são recomendações de investimento.", ""]
    rows = []
    for index, (score, entry, pair, result) in enumerate(ranked, 1):
        base = pair.get("baseToken") or {}
        name = base.get("symbol") or base.get("name") or "TOKEN"
        lines.append(f"{index}. {name} · Score {score}/100 · {result['state']} · confiança {result['confidence']}%")
        if result.get("insufficient"):
            lines.append("⏳ Histórico insuficiente; não entra em Oportunidades.")
        if pair.get("marketCap") is not None:
            lines.append(f"MC {compact(pair['marketCap'])}")
        if (pair.get("priceChange") or {}).get("h24") is not None:
            lines.append(f"24h {signed_percent(pair['priceChange']['h24'])}")
        rows.append([button(f"🧠 {name}"[:64], f"ia:{entry['address']}"),
                     button("📈 Setup", f"ie:{entry['address']}")])
    rows.append([button("◀️ Voltar", "menu")])
    return "\n".join(lines)[:3900], {"inline_keyboard": rows}


def intelligence_entries_for_chat(state: dict[str, Any], chat_id: int | str, user_id: str | None = None) -> list[dict[str, Any]]:
    """Use the existing watchlist and read-only wallet lookup; never crawl external token lists."""
    entries = watch_entries(state, chat_id)
    if not INTELLIGENCE_ENABLED or not user_id or not user_id.isdigit():
        return entries
    tracked_wallet = current_wallet(state, user_id, chat_id)
    if not tracked_wallet:
        return entries
    wallet_address = tracked_wallet["address"]
    now = time.monotonic()
    cached = INTELLIGENCE_WALLET_CACHE.get(wallet_address)
    if cached and now - cached[0] < 120:
        wallet_tokens = cached[1]
    else:
        try:
            info = solana_wallet.get_wallet_info(wallet_address, get_market_data, get_gecko_pairs)
            wallet_tokens = []
            for item in info.get("tokens", []):
                if not item.get("price_usd") or not is_valid_solana_address(item.get("mint")):
                    continue
                wallet_tokens.append({
                    "mint": item["mint"], "name": item.get("name"), "symbol": item.get("symbol"),
                    "amount": item.get("amount"), "_market_pair": {
                        "priceUsd": item["price_usd"],
                        "baseToken": {"address": item["mint"], "name": item.get("name"), "symbol": item.get("symbol")},
                        "marketCap": item.get("market_cap"), "fdv": item.get("fdv"),
                        "liquidity": {"usd": item.get("liquidity_usd")},
                        "volume": {"h24": item.get("volume_24h")},
                        "priceChange": {"h24": item.get("price_change_24h")},
                    },
                })
                INTELLIGENCE_MARKET_CACHE[item["mint"]] = (now, wallet_tokens[-1]["_market_pair"])
            INTELLIGENCE_WALLET_CACHE[wallet_address] = (now, wallet_tokens)
        except solana_wallet.WalletRpcError as exc:
            LOG.info("[WALLET] Scanner sem saldos por falha RPC: %s", exc)
            INTELLIGENCE_WALLET_CACHE[wallet_address] = (now, [])
            wallet_tokens = []
    known = {entry.get("address") for entry in entries}
    entries.extend({"address": item["mint"], "symbol": item.get("symbol"), "name": item.get("name"),
                    "subscribers": [str(chat_id)], "_market_pair": item["_market_pair"]}
                   for item in wallet_tokens if item["mint"] not in known)
    return entries


def intelligence_single_screen(state: dict[str, Any], chat_id: int | str, address: str,
                               mode: str = "analysis", user_id: str | None = None) -> tuple[str, dict[str, Any]]:
    if not INTELLIGENCE_ENABLED:
        return "🧠 Inteligência está desativada nesta execução.", back_markup()
    if not is_valid_solana_address(address):
        return "Informe um endereço Solana válido para análise.", back_markup()
    entry = state.get("watches", {}).get(address)
    if entry and str(chat_id) not in entry.get("subscribers", []):
        entry = None
    entry = dict(entry or {"address": address, "symbol": "", "name": "TOKEN"})
    pair = get_intelligence_market_data(address)
    if not pair:
        return "⚠️ Dados de mercado indisponíveis.", back_markup()
    history = state.setdefault("market_history", {}).get(address, [])
    entry_flow = (entry.get("flow") or {}).get("summary") if isinstance(entry.get("flow"), dict) else None
    result = intelligence.analyze(history, pair, flow=entry_flow,
                                  whale_flow_weight=WHALE_FLOW_WEIGHT_PERCENT)
    text = intelligence_analysis_text(address, entry, result, pair, mode)
    if user_id and user_id.isdigit():
        tracked_wallet = current_wallet(state, user_id, chat_id)
        if tracked_wallet:
            try:
                amount, _decimals, position = wallet_token_position(tracked_wallet["address"], address)
                if amount > 0:
                    price = intelligence.number(pair.get("priceUsd"))
                    value = amount * Decimal(str(price)) if price is not None else None
                    text += (f"\n\n💼 SUA POSIÇÃO\nQuantidade: {wallet_amount(amount)}\n"
                             f"Valor: {wallet_usd(value) if value is not None else 'indisponível'}\n"
                             + (f"Participação na carteira: {wallet_percent(position.get('allocation_percent'))}%\n"
                                if position and position.get("allocation_percent") is not None else "")
                             + (f"Investido (custo base): {wallet_usd(position['cost_basis_usd'])}\n"
                                if position and position.get("cost_basis_usd") is not None else "")
                             + (f"PnL não realizado: {wallet_usd(position['pnl_usd'])}"
                                + (f" ({wallet_percent(position['pnl_percent'])}%)"
                                   if position.get("pnl_percent") is not None else "")
                                if position and position.get("pnl_usd") is not None else
                                "PnL: indisponível por posição (sem custo de aquisição fornecido)."))
            except solana_wallet.WalletRpcError as exc:
                LOG.info("[WALLET] Posição da carteira indisponível para análise: %s", exc)
    rows = [[button("🧠 Analisar", f"ia:{address}"), button("📈 Entrada", f"ie:{address}"),
             button("🚨 Saída", f"ix:{address}")],
            [button("💼 Minha posição", f"ip:{address}")],
            [button("💰 Preço", f"p:{token_callback_id(address)}"), button("◀️ Voltar", "intel_analyze_menu")]]
    PRICE_LOOKUPS[token_callback_id(address)] = address
    return text, {"inline_keyboard": rows}


def intelligence_performance_text(state: dict[str, Any], chat_id: int | str) -> str:
    addresses = {entry.get("address") for entry in watch_entries(state, chat_id)}
    records = [item for item in state.get("signals", []) if item.get("address") in addresses]
    if not records:
        return ("📊 PERFORMANCE DO MODELO\n\nAinda não há sinais históricos para os tokens deste chat.\n"
                "A coleta começa junto com os snapshots do monitor. Não há alegação de desempenho sem amostra.")
    counts: dict[str, int] = {}
    for item in records:
        counts[item.get("state", "⚪ SEM DADOS")] = counts.get(item.get("state", "⚪ SEM DADOS"), 0) + 1
    lines = ["📊 PERFORMANCE DO MODELO", "", f"Sinais registrados: {len(records)}", "Contexto descritivo; não é backtest validado.", ""]
    lines.extend(f"{state_name}: {count}" for state_name, count in sorted(counts.items()))
    for horizon in ("5m", "15m", "1h", "4h"):
        outcomes = [item.get("outcomes", {}).get(horizon) for item in records
                    if isinstance(item.get("outcomes"), dict) and item.get("outcomes", {}).get(horizon) is not None]
        if outcomes:
            lines.append(f"\nApós {horizon}: {len(outcomes)} observações · média {signed_percent(sum(outcomes) / len(outcomes))}")
    watch_names = {entry.get("address"): entry.get("symbol") or entry.get("name") or "TOKEN"
                   for entry in watch_entries(state, chat_id)}
    lines.append("\nSinais recentes:")
    for item in reversed(records[-10:]):
        timestamp = item.get("timestamp")
        date_text = time.strftime("%d/%m %H:%M", time.localtime(timestamp)) if isinstance(timestamp, (int, float)) else "data indisponível"
        signal_score = item.get("score")
        lines.append(f"• {watch_names.get(item.get('address'), 'TOKEN')} · {date_text} · {item.get('state', '⚪ SEM DADOS')} · {signal_score if signal_score is not None else '—'}/100")
    lines.append("\nResultados passados não garantem resultados futuros.")
    return "\n".join(lines)[:3900]


def change_icon(value: Any) -> str:
    return "💰"


def watch_list_text(entries: list[dict[str, Any]], threshold: float, page: int = 0) -> str:
    if not entries:
        return "📋 MINHA LISTA\n\nSua lista ainda está vazia."
    page_size = 10
    page = max(0, min(page, (len(entries) - 1) // page_size))
    rows = []
    for entry in entries[page * page_size:(page + 1) * page_size]:
        name = entry.get("name") or entry.get("symbol") or "TOKEN"
        symbol = entry.get("symbol")
        if symbol and symbol.casefold() != name.casefold():
            name = f"{name} ({symbol})"
        rows.append(f"{change_icon(entry.get('last_change_24h'))} {name}")
    return "📋 MINHA LISTA\n\n" + "\n".join(rows)


def alerts_text(entries: list[dict[str, Any]], threshold: float) -> str:
    if not entries:
        return "🔔 MEUS ALERTAS\n\nAinda não há tokens na sua lista."
    rows = []
    for entry in entries:
        name = entry.get("symbol") or entry.get("name") or "TOKEN"
        rows.append(f"{change_icon(entry.get('last_change_24h'))} {name}\n⚡ Limite: ±{threshold:g}%")
    return "🔔 MEUS ALERTAS\n\nVocê está acompanhando:\n\n" + "\n\n".join(rows)


def token_buttons(entries: list[dict[str, Any]], action: str = "l", page: int = 0,
                  navigation_action: str | None = None) -> dict[str, Any]:
    page_size = 10
    page = max(0, min(page, (len(entries) - 1) // page_size)) if entries else 0
    start = page * page_size
    rows = [[button(f"{change_icon(entry.get('last_change_24h'))} {entry.get('symbol') or entry.get('name') or 'TOKEN'}"[:64],
                    f"{action}:{token_callback_id(entry['address'])}")] for entry in entries[start:start + page_size]]
    navigation = []
    navigation_action = navigation_action or action
    if page:
        navigation.append(button("⬅️ Anterior", f"{navigation_action}_page:{page - 1}"))
    if start + page_size < len(entries):
        navigation.append(button("Próxima ➡️", f"{navigation_action}_page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return {"inline_keyboard": rows}


def alerts_markup(entries: list[dict[str, Any]], page: int = 0) -> dict[str, Any]:
    rows = token_buttons(entries, "l", page, "a")["inline_keyboard"]
    rows.extend([[button("➕ Adicionar", "add_begin"), button("❌ Remover", "remove_menu")],
                 [button("◀️ Voltar", "menu")]])
    return {"inline_keyboard": rows}


def save_pair_to_watch(state: dict[str, Any], address: str, chat_id: int | str,
                       pair: dict[str, Any]) -> dict[str, Any]:
    with STATE_LOCK:
        watches = state.setdefault("watches", {})
        if address not in watches and len(watches) >= MAX_WATCHES:
            raise BotError(f"Limite de {MAX_WATCHES} tokens monitorados atingido.")
        price = pair.get("priceUsd")
        base = pair.get("baseToken") or {}
        entry = watches.setdefault(address, {"address": address, "subscribers": [], "anchor_price": float(price or 0), "last_alert": 0, "timestamp": time.time()})
        if str(chat_id) not in entry.setdefault("subscribers", []):
            entry["subscribers"].append(str(chat_id))
        entry["name"] = base.get("name") or entry.get("name") or base.get("symbol") or "Token"
        entry["symbol"] = base.get("symbol") or entry.get("symbol") or ""
        entry["last_price"] = price
        entry["reference_price"] = entry.get("anchor_price", price)
        entry["market_cap"] = pair.get("marketCap")
        entry["fdv"] = pair.get("fdv")
        entry["last_change_24h"] = (pair.get("priceChange") or {}).get("h24")
        entry["timestamp"] = entry.get("timestamp", time.time())
        return entry


def help_text() -> str:
    return ("ℹ️ AJUDA\n\n🚀 Este bot permite:\n\n"
        "💰 Consultar preços de tokens Solana\n🔔 Criar alertas de variação\n"
        "📋 Gerenciar sua lista de tokens\n📊 Acompanhar dados de mercado\n"
        "💼 Consultar uma carteira Solana pública\n🧠 Análise técnica experimental baseada no histórico coletado pelo bot\n\n"
        "Como usar:\n\n1️⃣ Toque em \"Consultar preço\"\n2️⃣ Escolha um token\n"
        "3️⃣ Para adicionar outro, toque em \"Adicionar token\"\n"
        "4️⃣ Para acompanhar uma carteira pública, toque em \"Minha carteira\"\n\n"
        "Comandos disponíveis:\n/start — abrir o menu\n/price — consultar preço\n"
        "/list — minha lista\n/watch — adicionar alerta\n/unwatch — remover alerta\n/help — ajuda\n\n"
        "/analisar <endereço> — análise de mercado\n\n"
        "Os comandos ficam apenas como referência técnica. A inteligência é somente analítica, não executa operações e não garante resultados.")


MARKET_UNAVAILABLE = "Não consegui consultar o preço agora. As fontes de mercado estão temporariamente indisponíveis. Tente novamente em alguns segundos."


def begin_add_flow(state: dict[str, Any], token: str, chat_id: int | str,
                   message: dict[str, Any] | None = None) -> None:
    chat_key = str(chat_id)
    with STATE_LOCK:
        state.setdefault("awaiting_add", {})[chat_key] = time.time()
        state.setdefault("pending_add", {}).pop(chat_key, None)
        save_state(state)
    text = "➕ ADICIONAR TOKEN\n\nEnvie o endereço do token Solana que deseja adicionar."
    if message is not None:
        show_callback_screen(token, chat_id, message, text, back_markup())
    else:
        send_message(token, chat_id, text, back_markup())


def begin_wallet_add(state: dict[str, Any], token: str, chat_id: int | str, user_id: str,
                     message: dict[str, Any] | None = None) -> None:
    if not user_id.isdigit():
        text = "Não consegui identificar sua conta do Telegram. Abra o bot em uma conversa privada e tente novamente."
        if message is not None:
            show_callback_screen(token, chat_id, message, text, back_markup("wallet_menu"))
        else:
            send_message(token, chat_id, text, back_markup("wallet_menu"))
        return
    chat_key = str(chat_id)
    with STATE_LOCK:
        state.setdefault("awaiting_wallet", {})[user_id] = chat_key
        state.setdefault("awaiting_add", {}).pop(chat_key, None)
        state.setdefault("pending_add", {}).pop(chat_key, None)
        save_state(state)
    text = ("📥 Envie o endereço público da sua carteira Solana.\n\n"
            "⚠️ Envie SOMENTE o endereço público.\nNunca envie seed phrase ou chave privada.")
    markup = back_markup("wallet_menu")
    if message is not None:
        show_callback_screen(token, chat_id, message, text, markup)
    else:
        send_message(token, chat_id, text, markup)


def register_wallet(state: dict[str, Any], user_id: str, chat_id: int | str, address: str,
                    last_signature: str | None = None) -> None:
    # Read-only wallet record: public address and monitoring metadata only; no signing credentials.
    state.setdefault("wallets", {})[user_id] = {
        "address": address,
        "chat_id": str(chat_id),
        "monitoring_enabled": True,
        "alerts_enabled": True,
        "last_signature": last_signature,
        "last_check": None,
        "registered_at": time.time(),
        "history": [],
    }
    state.setdefault("awaiting_wallet", {}).pop(user_id, None)
    save_state(state)


def preview_add_flow(state: dict[str, Any], token: str, chat_id: int | str, address: str) -> None:
    if not is_valid_solana_address(address):
        send_message(token, chat_id, "Endereço Solana inválido. Envie uma chave pública Base58 de 32 bytes.")
        return
    pair = get_market_data(address)
    if not pair:
        send_message(token, chat_id, "Não encontrei esse token nas fontes de mercado.")
        return
    chat_key = str(chat_id)
    with STATE_LOCK:
        state.setdefault("pending_add", {})[chat_key] = {"address": address, "pair": pair, "timestamp": time.time()}
        save_state(state)
    markup = {"inline_keyboard": [[button("✅ Adicionar", f"add_yes:{token_callback_id(address)}"), button("❌ Cancelar", "cancel_add")],
                                   [button("◀️ Voltar", "menu")]]}
    send_message(token, chat_id, "✅ TOKEN ENCONTRADO\n\n" + pair_summary(pair), markup)


def send_watch_list(token: str, chat_id: int | str, state: dict[str, Any], threshold: float) -> None:
    entries = watch_entries(state, chat_id)
    if not entries:
        send_message(token, chat_id, watch_list_text([], threshold), list_actions_markup())
        return
    markup = token_buttons(entries)
    markup["inline_keyboard"].extend(list_actions_markup()["inline_keyboard"])
    send_message(token, chat_id, watch_list_text(entries, threshold), markup)


def send_alerts(token: str, chat_id: int | str, state: dict[str, Any], threshold: float) -> None:
    entries = watch_entries(state, chat_id)
    send_message(token, chat_id, alerts_text(entries, threshold), alerts_markup(entries))


def show_callback_screen(token: str, chat_id: int | str, message: dict[str, Any], text: str,
                         markup: dict[str, Any] | None = None) -> None:
    message_id = message.get("message_id")
    if message_id is None:
        send_message(token, chat_id, text, markup)
        return
    try:
        edit_message(token, chat_id, message_id, text, markup)
    except TelegramError as exc:
        error = str(exc).casefold()
        if "message is not modified" in error:
            return
        if "message to edit not found" in error or "message can't be edited" in error:
            send_message(token, chat_id, text, markup)
            return
        raise


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
    incoming_text = message["text"]
    raw_text = incoming_text.strip()
    chat_key = str(chat_id)
    with STATE_LOCK:
        state.setdefault("awaiting_add", {})
        state.setdefault("pending_add", {})
        state.setdefault("awaiting_wallet", {})
        state.setdefault("wallets", {})
        is_awaiting_address = chat_key in state["awaiting_add"]
        is_awaiting_wallet = bool(user_id and state["awaiting_wallet"].get(user_id) == chat_key)
    if raw_text.casefold() == "/cancel":
        with STATE_LOCK:
            state["awaiting_add"].pop(chat_key, None)
            state["pending_add"].pop(chat_key, None)
            state["awaiting_wallet"].pop(user_id, None)
            save_state(state)
        send_message(token, chat_id, "Operação cancelada.")
        return
    if is_awaiting_wallet and not raw_text.startswith("/"):
        if incoming_text != raw_text or not is_valid_solana_address(incoming_text):
            send_message(token, chat_id, "❌ Endereço Solana inválido.\n\nEnvie apenas o endereço público da carteira.", back_markup("wallet_menu"))
            return
        try:
            with STATE_LOCK:
                register_wallet(state, user_id, chat_id, incoming_text)
        except Exception as exc:
            LOG.exception("[WALLET] Could not persist public wallet for user_id=%s: %s", user_id, exc)
            send_message(token, chat_id, "Não consegui salvar a carteira agora. Tente novamente.", back_markup("wallet_menu"))
            return
        LOG.info("[WALLET] Public wallet registered for user_id=%s", user_id)
        send_message(token, chat_id,
                     "💼 CARTEIRA CADASTRADA\n\nO monitoramento somente leitura foi ativado.\n"
                     f"Carteira: {wallet_short_address(incoming_text)}\n\n"
                     "Não armazenamos chaves privadas nem assinamos transações.",
                     {"inline_keyboard": [[button("💼 Ver carteira", "wallet_menu")], [button("⬅️ Voltar", "menu")]]})
        return
    if is_awaiting_address and not raw_text.startswith("/"):
        try:
            preview_add_flow(state, token, chat_id, raw_text)
        except MarketDataError as exc:
            LOG.warning("Consulta a fontes de mercado falhou no fluxo de adição: %s", exc)
            send_message(token, chat_id, MARKET_UNAVAILABLE)
        return
    command, arg = parse_message(raw_text)
    if command is None:
        return
    LOG.info("Comando recebido: chat_id=%s user_id=%s comando=%s", chat_id, user_id or "indisponível", command)
    with STATE_LOCK:
        try:
            if command == "/start":
                send_message(token, chat_id, start_text(), main_menu_markup())
            elif command == "/help":
                send_message(token, chat_id, help_text(), back_markup())
            elif command == "/price":
                if not arg:
                    text, markup = price_menu_markup(state, chat_id)
                    send_message(token, chat_id, text, markup)
                elif len(arg.split()) > 1 or any(char.isspace() for char in arg):
                    send_message(token, chat_id, "Informe apenas um símbolo ou endereço Solana válido.")
                elif 32 <= len(arg) <= 50 and not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Endereço Solana inválido. Confira o Base58 e tente novamente.")
                else:
                    pair = get_market_data(arg)
                    send_message(token, chat_id, pair_summary(pair) if pair else "Token Solana não encontrado nas fontes de mercado.",
                                 token_details_markup(pair, arg, "menu") if pair else None)
            elif command == "/watch":
                if not arg:
                    begin_add_flow(state, token, chat_id)
                    return
                if not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /watch <endereço>")
                    return
                pair = get_market_data(arg)
                if not pair:
                    send_message(token, chat_id, "Não encontrei esse token nas fontes de mercado.")
                    return
                entry = save_pair_to_watch(state, arg, chat_id, pair)
                save_state(state)
                LOG.info("Token adicionado: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", arg)
                confirmation = ("✅ TOKEN ADICIONADO\n\n" + pair_summary(pair)
                    + f"\n\n🔔 Alertas: ±{threshold:g}%\nMonitoramento ativo.")
                send_message(token, chat_id, confirmation, pair_links_markup(pair))
            elif command == "/unwatch":
                if not arg:
                    text, markup = remove_menu_markup(state, chat_id)
                    send_message(token, chat_id, text, markup)
                    return
                if not is_valid_solana_address(arg):
                    send_message(token, chat_id, "Informe um endereço Solana válido: /unwatch <endereço>")
                    return
                entry = state["watches"].get(arg)
                if not entry or chat_key not in entry.get("subscribers", []):
                    send_message(token, chat_id, "Esse chat não monitora esse endereço.")
                else:
                    entry["subscribers"].remove(chat_key)
                    if not entry["subscribers"]:
                        state["watches"].pop(arg, None)
                    save_state(state)
                    LOG.info("Token removido: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", arg)
                    send_message(token, chat_id, "Monitoramento removido.")
            elif command == "/list":
                send_watch_list(token, chat_id, state, threshold)
            elif command == "/analyze":
                if not INTELLIGENCE_ENABLED:
                    send_message(token, chat_id, "🧠 Inteligência está desativada nesta execução.", back_markup())
                elif not arg:
                    entries = intelligence_entries_for_chat(state, chat_id, user_id)
                    send_message(token, chat_id, "🧠 ANALISAR TOKEN\n\nEscolha um token monitorado:",
                                 intelligence_token_markup(entries, "ia"))
                else:
                    text, markup = intelligence_single_screen(state, chat_id, arg, user_id=user_id)
                    send_message(token, chat_id, text, markup)
            elif command in {"/scanner", "/opportunities"}:
                if not INTELLIGENCE_ENABLED:
                    text, markup = "🧠 Inteligência está desativada nesta execução.", back_markup()
                else:
                    text, markup = intelligence_scan_text(intelligence_entries_for_chat(state, chat_id, user_id), state.setdefault("market_history", {}),
                                                           opportunities=command == "/opportunities")
                send_message(token, chat_id, text, markup)
            elif command in {"/entry", "/exit"}:
                entries = intelligence_entries_for_chat(state, chat_id, user_id)
                title = "📈 SETUP DE ENTRADA" if command == "/entry" else "🚨 SETUP DE SAÍDA"
                action = "ie" if command == "/entry" else "ix"
                text = (f"{title}\n\nEscolha um token monitorado:" if INTELLIGENCE_ENABLED
                        else "🧠 Inteligência está desativada nesta execução.")
                send_message(token, chat_id, text, intelligence_token_markup(entries, action) if INTELLIGENCE_ENABLED else back_markup())
            elif command == "/performance":
                send_message(token, chat_id, intelligence_performance_text(state, chat_id), back_markup())
            else:
                send_message(token, chat_id, help_text(), main_menu_markup())
        except TelegramError:
            raise
        except MarketDataError as exc:
            LOG.warning("Consulta a fontes de mercado falhou em %s: %s", command, exc)
            send_message(token, chat_id, "⚠️ Dados de mercado indisponíveis." if command in {"/analyze", "/scanner", "/opportunities", "/entry", "/exit"} else MARKET_UNAVAILABLE)
        except BotError as exc:
            send_message(token, chat_id, str(exc))
        except Exception as exc:
            LOG.warning("Falha ao processar %s: %s", command, exc)
            send_message(token, chat_id, "Não consegui concluir o comando agora. Tente novamente mais tarde.")


def handle_callback_update(update: dict[str, Any], state: dict[str, Any], token: str, threshold: float,
                           allowed_ids: set[str]) -> None:
    query = update.get("callback_query") or {}
    callback_id = query.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if callback_id is None:
        return
    user_id = str((query.get("from") or {}).get("id", ""))
    unauthorized = bool(allowed_ids and user_id not in allowed_ids)
    try:
        answer_callback(token, str(callback_id), "Acesso não autorizado." if unauthorized else None)
    except TelegramError as exc:
        LOG.warning("Não foi possível confirmar callback do Telegram: %s", exc)
    if unauthorized or chat.get("id") is None:
        return
    chat_id = chat["id"]
    chat_key = str(chat_id)
    data = str(query.get("data") or "")
    callback_message = message
    with STATE_LOCK:
        state.setdefault("awaiting_add", {})
        state.setdefault("pending_add", {})
    LOG.info("Callback recebido: chat_id=%s user_id=%s ação=%s", chat_id, user_id or "indisponível", data.partition(":")[0])
    try:
        if data in {"menu", "back"}:
            with STATE_LOCK:
                state["awaiting_add"].pop(chat_key, None)
                state["pending_add"].pop(chat_key, None)
                if user_id:
                    state.setdefault("awaiting_wallet", {}).pop(user_id, None)
                save_state(state)
            show_callback_screen(token, chat_id, callback_message, start_text(), main_menu_markup())
        elif data in {"intel_scanner", "intel_opportunities"}:
            if not INTELLIGENCE_ENABLED:
                text, markup = "🧠 Inteligência está desativada nesta execução.", back_markup()
            else:
                text, markup = intelligence_scan_text(intelligence_entries_for_chat(state, chat_id, user_id), state.setdefault("market_history", {}),
                                                       opportunities=data == "intel_opportunities")
            show_callback_screen(token, chat_id, callback_message, text, markup)
        elif data in {"intel_analyze_menu", "intel_entry_menu", "intel_exit_menu"}:
            action = {"intel_analyze_menu": "ia", "intel_entry_menu": "ie", "intel_exit_menu": "ix"}[data]
            title = {"ia": "🧠 ANALISAR TOKEN", "ie": "📈 SETUP DE ENTRADA", "ix": "🚨 SETUP DE SAÍDA"}[action]
            show_callback_screen(token, chat_id, callback_message,
                                 f"{title}\n\nEscolha um token monitorado:",
                                 intelligence_token_markup(intelligence_entries_for_chat(state, chat_id, user_id), action))
        elif data == "intel_performance":
            show_callback_screen(token, chat_id, callback_message, intelligence_performance_text(state, chat_id), back_markup())
        elif data.startswith(("ia:", "ie:", "ix:", "ip:")):
            action, _, address = data.partition(":")
            mode = {"ia": "analysis", "ie": "entry", "ix": "exit", "ip": "analysis"}[action]
            text, markup = intelligence_single_screen(state, chat_id, address, mode, user_id)
            show_callback_screen(token, chat_id, callback_message, text, markup)
        elif data == "wallet_menu":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if not wallet:
                text, _ = wallet_menu_text(state, user_id, chat_id) if user_id.isdigit() else (
                    "💼 MINHA CARTEIRA\n\nNenhuma carteira cadastrada.", None)
                show_callback_screen(token, chat_id, callback_message, text, wallet_markup(None))
            else:
                try:
                    LOG.info("[WALLET] Checking wallet %s", wallet_short_address(wallet["address"]))
                    info = solana_wallet.get_wallet_info(wallet["address"], get_market_data, get_gecko_pairs)
                    wallet["last_check"] = time.time()
                    with STATE_LOCK:
                        state["wallets"][user_id]["last_check"] = wallet["last_check"]
                        save_state(state)
                    show_callback_screen(token, chat_id, callback_message,
                                         wallet_info_text(wallet["address"], info, wallet), wallet_markup(wallet))
                except solana_wallet.WalletRpcError as exc:
                    LOG.warning("[WALLET] RPC error for wallet %s: %s", wallet_short_address(wallet["address"]), exc)
                    show_callback_screen(token, chat_id, callback_message,
                                         "⚠️ Não consegui consultar a blockchain agora.\n\nTente novamente em alguns segundos.",
                                         wallet_markup(wallet))
        elif data in {"wallet_view", "wallet_refresh"} or data.startswith("wallet_tokens:"):
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if not wallet:
                show_callback_screen(token, chat_id, callback_message, "Nenhuma carteira cadastrada.", wallet_markup(None))
                return
            try:
                LOG.info("[WALLET] Checking wallet %s", wallet_short_address(wallet["address"]))
                info = solana_wallet.get_wallet_info(wallet["address"], get_market_data, get_gecko_pairs)
                wallet["last_check"] = time.time()
                with STATE_LOCK:
                    state["wallets"][user_id]["last_check"] = wallet["last_check"]
                    save_state(state)
                details = data == "wallet_view" or data.startswith("wallet_tokens:")
                page = 0
                if data.startswith("wallet_tokens:"):
                    try:
                        page = max(0, int(data.partition(":")[2]))
                    except ValueError:
                        page = 0
                page_count = max(1, (len(info.get("tokens", [])) + WALLET_DETAIL_PAGE_SIZE - 1) // WALLET_DETAIL_PAGE_SIZE)
                page = min(page, page_count - 1)
                show_callback_screen(token, chat_id, callback_message,
                                     wallet_info_text(wallet["address"], info, wallet, details=details, detail_page=page),
                                     wallet_markup(wallet, detail_page=page if details else None, page_count=page_count))
            except solana_wallet.WalletRpcError as exc:
                LOG.warning("[WALLET] RPC error for wallet %s: %s", wallet_short_address(wallet["address"]), exc)
                show_callback_screen(token, chat_id, callback_message,
                                     "⚠️ Não consegui consultar a blockchain agora.\n\nTente novamente em alguns segundos.",
                                     wallet_markup(wallet))
        elif data == "wallet_add":
            if not user_id.isdigit():
                show_callback_screen(token, chat_id, callback_message, "Não consegui identificar sua conta do Telegram.", back_markup())
            elif current_wallet(state, user_id, chat_id) and not current_wallet(state, user_id, chat_id).get("configured_default"):
                show_callback_screen(token, chat_id, callback_message, "Já existe uma carteira cadastrada nesta conversa.", wallet_markup(current_wallet(state, user_id, chat_id)))
            else:
                begin_wallet_add(state, token, chat_id, user_id, callback_message)
        elif data == "wallet_history":
            text, markup = wallet_history_screen(current_wallet(state, user_id, chat_id) if user_id.isdigit() else None)
            show_callback_screen(token, chat_id, callback_message, text, markup)
        elif data == "wallet_alerts":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if wallet and wallet.get("configured_default"):
                show_callback_screen(token, chat_id, callback_message, "Alertas de carteira exigem cadastrar o endereço nesta conversa.", wallet_markup(wallet))
                return
            if not wallet:
                show_callback_screen(token, chat_id, callback_message, "Nenhuma carteira cadastrada.", wallet_markup(None))
            else:
                enabled = wallet.get("alerts_enabled", True)
                text = "🔔 ALERTAS DA CARTEIRA\n\nStatus: 🟢 Ativados" if enabled else "🔔 ALERTAS DA CARTEIRA\n\n🔕 Alertas desativados"
                toggle = "🔕 Desativar alertas" if enabled else "🔔 Ativar alertas"
                show_callback_screen(token, chat_id, callback_message, text,
                                     {"inline_keyboard": [[button(toggle, "wallet_alert_toggle")], [button("⬅️ Voltar", "wallet_menu")]]})
        elif data == "wallet_alert_toggle":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if wallet and wallet.get("configured_default"):
                show_callback_screen(token, chat_id, callback_message, "Alertas de carteira exigem cadastrar o endereço nesta conversa.", wallet_markup(wallet))
                return
            if not wallet:
                show_callback_screen(token, chat_id, callback_message, "Nenhuma carteira cadastrada.", wallet_markup(None))
            else:
                with STATE_LOCK:
                    entry = state["wallets"][user_id]
                    entry["alerts_enabled"] = not entry.get("alerts_enabled", True)
                    save_state(state)
                    enabled = entry["alerts_enabled"]
                text = "🔔 ALERTAS DA CARTEIRA\n\nStatus: 🟢 Ativados" if enabled else "🔔 ALERTAS DA CARTEIRA\n\n🔕 Alertas desativados"
                toggle = "🔕 Desativar alertas" if enabled else "🔔 Ativar alertas"
                show_callback_screen(token, chat_id, callback_message, text,
                                     {"inline_keyboard": [[button(toggle, "wallet_alert_toggle")], [button("⬅️ Voltar", "wallet_menu")]]})
        elif data == "wallet_remove":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if wallet and wallet.get("configured_default"):
                show_callback_screen(token, chat_id, callback_message, "Este endereço padrão é configurado pelo proprietário do bot.", wallet_markup(wallet))
                return
            if not wallet:
                show_callback_screen(token, chat_id, callback_message, "Nenhuma carteira cadastrada.", wallet_markup(None))
            else:
                show_callback_screen(token, chat_id, callback_message,
                                     "⚠️ Remover carteira?\n\nIsso interromperá o monitoramento dessa carteira.",
                                     {"inline_keyboard": [[button("✅ Sim, remover", "wallet_remove_yes"), button("❌ Cancelar", "wallet_remove_no")],
                                                           [button("⬅️ Voltar", "wallet_menu")]]})
        elif data == "wallet_remove_yes":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if wallet and wallet.get("configured_default"):
                show_callback_screen(token, chat_id, callback_message, "Este endereço padrão é configurado pelo proprietário do bot.", wallet_markup(wallet))
                return
            if wallet:
                with STATE_LOCK:
                    state["wallets"].pop(user_id, None)
                    save_state(state)
                show_callback_screen(token, chat_id, callback_message, "🗑️ Carteira removida com sucesso.", wallet_markup(None))
            else:
                show_callback_screen(token, chat_id, callback_message, "Nenhuma carteira cadastrada.", wallet_markup(None))
        elif data == "wallet_remove_no":
            wallet = current_wallet(state, user_id, chat_id) if user_id.isdigit() else None
            if wallet:
                try:
                    info = solana_wallet.get_wallet_info(wallet["address"], get_market_data, get_gecko_pairs)
                    show_callback_screen(token, chat_id, callback_message, wallet_info_text(wallet["address"], info, wallet), wallet_markup(wallet))
                except solana_wallet.WalletRpcError:
                    show_callback_screen(token, chat_id, callback_message, "💼 MINHA CARTEIRA\n\nMonitoramento ativo.", wallet_markup(wallet))
            else:
                show_callback_screen(token, chat_id, callback_message, "💼 MINHA CARTEIRA\n\nNenhuma carteira cadastrada.", wallet_markup(None))
        elif data == "price_menu" or data.startswith(("p_page:", "p_refresh:")):
            try:
                page = int(data.split(":", 1)[1]) if ":" in data else 0
            except ValueError:
                page = 0
            text, markup = price_menu_markup(state, chat_id, page)
            show_callback_screen(token, chat_id, callback_message, text, markup)
        elif data == "watch_list":
            entries = watch_entries(state, chat_id)
            markup = token_buttons(entries)
            markup["inline_keyboard"].extend(list_actions_markup()["inline_keyboard"])
            show_callback_screen(token, chat_id, callback_message, watch_list_text(entries, threshold), markup)
        elif data.startswith("l_page:"):
            try:
                page = max(0, int(data.split(":", 1)[1]))
            except ValueError:
                page = 0
            entries = watch_entries(state, chat_id)
            markup = token_buttons(entries, "l", page)
            markup["inline_keyboard"].extend(list_actions_markup()["inline_keyboard"])
            show_callback_screen(token, chat_id, callback_message, watch_list_text(entries, threshold, page), markup)
        elif data == "alerts" or data.startswith("a_page:"):
            try:
                page = max(0, int(data.split(":", 1)[1])) if ":" in data else 0
            except ValueError:
                page = 0
            entries = watch_entries(state, chat_id)
            alert_text = alerts_text(entries, threshold)
            if entries:
                page = max(0, min(page, (len(entries) - 1) // 10))
                names = [f"{change_icon(entry.get('last_change_24h'))} {entry.get('symbol') or entry.get('name') or 'TOKEN'}\n⚡ Limite: ±{threshold:g}%"
                         for entry in entries[page * 10:(page + 1) * 10]]
                alert_text = "🔔 MEUS ALERTAS\n\nVocê está acompanhando:\n\n" + "\n\n".join(names)
            show_callback_screen(token, chat_id, callback_message, alert_text, alerts_markup(entries, page))
        elif data == "help":
            show_callback_screen(token, chat_id, callback_message, help_text(), back_markup())
        elif data == "add_begin":
            begin_add_flow(state, token, chat_id, callback_message)
        elif data == "remove_menu" or data.startswith("r_page:"):
            try:
                page = int(data.split(":", 1)[1]) if ":" in data else 0
            except ValueError:
                page = 0
            text, markup = remove_menu_markup(state, chat_id, page)
            show_callback_screen(token, chat_id, callback_message, text, markup)
        elif data.startswith(("p:", "l:")):
            action = data[0]
            entry = watch_for_callback(state, chat_id, data[2:])
            if not entry:
                show_callback_screen(token, chat_id, callback_message, "Esse token não está mais na sua lista.", back_markup("watch_list" if action == "l" else "price_menu"))
                return
            pair = get_market_data(entry["address"])
            if pair:
                back = "watch_list" if action == "l" else "price_menu"
                show_callback_screen(token, chat_id, callback_message, pair_summary(pair), token_details_markup(pair, entry["address"], back))
            else:
                show_callback_screen(token, chat_id, callback_message, "Token não encontrado nas fontes de mercado.", back_markup("watch_list" if action == "l" else "price_menu"))
        elif data.startswith("u:"):
            _, callback_key, origin = (data.split(":", 2) + ["p"])[:3]
            if origin == "d":
                address = PRICE_LOOKUPS.get(callback_key)
                back = "menu"
            else:
                entry = watch_for_callback(state, chat_id, callback_key)
                address = entry.get("address") if entry else None
                back = {"l": "watch_list", "p": "price_menu", "a": "alerts"}.get(origin, "price_menu")
            if not address:
                show_callback_screen(token, chat_id, callback_message, "Esse token não está mais na sua lista.", back_markup(back))
                return
            pair = get_market_data(address)
            if pair:
                show_callback_screen(token, chat_id, callback_message, pair_summary(pair), token_details_markup(pair, address, back))
            else:
                show_callback_screen(token, chat_id, callback_message, MARKET_UNAVAILABLE, back_markup(back))
        elif data.startswith("r:"):
            entry = watch_for_callback(state, chat_id, data[2:])
            if not entry:
                show_callback_screen(token, chat_id, callback_message, "Esse token não está mais na sua lista.", back_markup("alerts"))
                return
            label = entry.get("name") or entry.get("symbol") or "este token"
            markup = {"inline_keyboard": [[button("✅ Sim, remover", f"ry:{token_callback_id(entry['address'])}"), button("❌ Cancelar", "cancel_remove")]]}
            markup["inline_keyboard"].append([button("◀️ Voltar", "remove_menu")])
            show_callback_screen(token, chat_id, callback_message, f"Deseja remover {label} dos seus alertas?", markup)
        elif data.startswith("ry:"):
            entry = watch_for_callback(state, chat_id, data[3:])
            if not entry:
                show_callback_screen(token, chat_id, callback_message, "Esse token não está mais na sua lista.", back_markup("alerts"))
                return
            address = entry["address"]
            with STATE_LOCK:
                entry["subscribers"].remove(chat_key)
                if not entry["subscribers"]:
                    state["watches"].pop(address, None)
                save_state(state)
            show_callback_screen(token, chat_id, callback_message, "✅ Token removido dos seus alertas.", back_markup("alerts"))
        elif data.startswith("add_yes:"):
            with STATE_LOCK:
                pending = dict(state["pending_add"].get(chat_key) or {})
            if not pending or token_callback_id(pending.get("address", "")) != data.split(":", 1)[1]:
                show_callback_screen(token, chat_id, callback_message, "A confirmação expirou. Inicie novamente em Adicionar token.", back_markup())
                return
            pair = get_market_data(pending["address"])
            if not pair:
                show_callback_screen(token, chat_id, callback_message, "Token não encontrado nas fontes de mercado.", back_markup())
                return
            entry = save_pair_to_watch(state, pending["address"], chat_id, pair)
            with STATE_LOCK:
                state["pending_add"].pop(chat_key, None)
                state["awaiting_add"].pop(chat_key, None)
                save_state(state)
            LOG.info("Token adicionado pelo menu: chat_id=%s user_id=%s address=%s", chat_id, user_id or "indisponível", entry["address"])
            show_callback_screen(token, chat_id, callback_message, "✅ Adicionado aos seus alertas.\n\n" + pair_summary(pair),
                                 token_details_markup(pair, pending["address"], "alerts"))
        elif data in {"cancel_add", "cancel_remove"}:
            if data == "cancel_add":
                with STATE_LOCK:
                    state["pending_add"].pop(chat_key, None)
                    state["awaiting_add"].pop(chat_key, None)
                    save_state(state)
            if data == "cancel_remove":
                entries = watch_entries(state, chat_id)
                show_callback_screen(token, chat_id, callback_message, alerts_text(entries, threshold), alerts_markup(entries))
            else:
                show_callback_screen(token, chat_id, callback_message, start_text(), main_menu_markup())
        else:
            show_callback_screen(token, chat_id, callback_message, "Essa ação não é reconhecida.", back_markup())
    except TelegramError:
        raise
    except MarketDataError as exc:
        LOG.warning("Consulta a fontes de mercado falhou no callback: %s", exc)
        if data.startswith(("ia:", "ie:", "ix:", "ip:")):
            show_callback_screen(token, chat_id, callback_message, "⚠️ Dados de mercado indisponíveis.", back_markup("intel_analyze_menu"))
            return
        if data.startswith("l:") or data.endswith(":l"):
            back = "watch_list"
        elif data.startswith("p:") or data.endswith(":p"):
            back = "price_menu"
        elif data.endswith(":a"):
            back = "alerts"
        else:
            back = "menu"
        show_callback_screen(token, chat_id, callback_message, MARKET_UNAVAILABLE, back_markup(back))
    except BotError as exc:
        show_callback_screen(token, chat_id, callback_message, str(exc), back_markup())
    except Exception:
        LOG.exception("Falha ao processar callback %s no chat %s", data.partition(":")[0], chat_id)
        show_callback_screen(token, chat_id, callback_message, "Não consegui concluir essa ação agora. Tente novamente.", back_markup())


def check_prices(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    global FLOW_CURSOR
    with STATE_LOCK:
        addresses = [(key, entry["address"]) for key, entry in state["watches"].items()]
    flow_targets: set[str] = set()
    if addresses:
        first = FLOW_CURSOR % len(addresses)
        budget = min(FLOW_REQUESTS_PER_CYCLE, len(addresses))
        flow_targets = {addresses[(first + index) % len(addresses)][1] for index in range(budget)}
        FLOW_CURSOR = (first + budget) % len(addresses)
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
                flow_rows = None
                flow_error = None
                if address in flow_targets and pair.get("poolAddress"):
                    try:
                        flow_rows = get_gecko_trades(pair["poolAddress"], address)
                    except Exception as exc:
                        flow_error = str(exc)
                        LOG.warning("Consulta de trades GeckoTerminal falhou para %s: %s", address, flow_error)
                with STATE_LOCK:
                    entry = state["watches"].get(key)
                    if entry is None:
                        continue
                    previous_price = intelligence.number(entry.get("last_price"))
                    flow_event = None
                    flow_summary = None
                    history_rows = state.get("market_history", {}).get(address, [])
                    old_15m = [row for row in history_rows if isinstance(row, dict)
                               and intelligence.number(row.get("timestamp")) is not None
                               and now - 1800 <= float(row["timestamp"]) <= now - 900]
                    price_15m = float(old_15m[-1]["price"]) if old_15m and intelligence.number(old_15m[-1].get("price")) else None
                    existing_flow = flow_analysis.sanitize_flow(entry.get("flow"))
                    if address in flow_targets and flow_error:
                        existing_flow.update(available=False, last_check=now, last_fetch_at=now,
                                             last_error=flow_error[:300])
                        if isinstance(existing_flow.get("summary"), dict):
                            existing_flow["summary"]["available"] = False
                        entry["flow"] = existing_flow
                    elif address in flow_targets and not pair.get("poolAddress"):
                        existing_flow.update(available=False, last_check=now, last_fetch_at=now,
                                             last_error="Esta fonte não identificou um pool GeckoTerminal.")
                        if isinstance(existing_flow.get("summary"), dict):
                            existing_flow["summary"]["available"] = False
                        entry["flow"] = existing_flow
                    elif existing_flow.get("initialized") and not existing_flow.get("last_error"):
                        flow_state, flow_event = flow_analysis.update_flow(
                            existing_flow, flow_rows or [], now=now,
                            liquidity_usd=intelligence.number((pair.get("liquidity") or {}).get("usd")),
                            previous_price=price_15m, current_price=price,
                            cooldown_seconds=SMART_FLOW_COOLDOWN_SECONDS)
                        if address in flow_targets:
                            flow_state["last_fetch_at"] = now
                            flow_state["possibly_truncated"] = len(flow_rows or []) >= 300
                        entry["flow"] = flow_state
                        flow_summary = flow_state.get("summary") if flow_state.get("available") else None
                    elif address in flow_targets and pair.get("poolAddress") and flow_rows is not None:
                        flow_state, flow_event = flow_analysis.update_flow(
                            existing_flow, flow_rows, now=now,
                            liquidity_usd=intelligence.number((pair.get("liquidity") or {}).get("usd")),
                            previous_price=price_15m, current_price=price,
                            cooldown_seconds=SMART_FLOW_COOLDOWN_SECONDS)
                        flow_state["last_fetch_at"] = now
                        flow_state["possibly_truncated"] = len(flow_rows or []) >= 300
                        entry["flow"] = flow_state
                        flow_summary = flow_state.get("summary") if flow_state.get("available") else None
                    entry["last_price"] = price
                    entry["last_change_24h"] = (pair.get("priceChange") or {}).get("h24")
                    entry["market_cap"] = pair.get("marketCap")
                    entry["fdv"] = pair.get("fdv")
                    entry["updated_at"] = now
                    entry["symbol"] = base.get("symbol") or base.get("name") or entry.get("symbol") or "Token"
                    entry["name"] = base.get("name") or entry.get("name") or entry["symbol"]
                    anchor = float(entry.get("anchor_price") or price)
                    if anchor <= 0:
                        entry["anchor_price"] = price
                        continue
                    change = (price / anchor - 1) * 100
                    smart_notice = None
                    if INTELLIGENCE_ENABLED:
                        market_history = state.setdefault("market_history", {})
                        snapshot = intelligence.append_snapshot(market_history, address, pair, now,
                                                                HISTORY_RETENTION_HOURS, INTELLIGENCE_INTERVAL_SECONDS)
                        result = intelligence.analyze(market_history.get(address, []), pair, timestamp=now,
                                                      flow=flow_summary, whale_flow_weight=WHALE_FLOW_WEIGHT_PERCENT)
                        if snapshot:
                            signals = state.setdefault("signals", [])
                            intelligence.record_signal(signals, address, result, snapshot)
                            intelligence.update_signal_outcomes(signals, address, snapshot)
                        event_states = {"🔥 BREAKOUT", "☠️ ALTO RISCO", "🔴 DISTRIBUIÇÃO"}
                        event_key = (f"{result.get('state')}|{result.get('entry', {}).get('type')}"
                                     if result.get("state") in event_states or result.get("entry", {}).get("late") else None)
                        previous_event = entry.get("last_smart_signal")
                        if SMART_ALERTS_ENABLED and event_key and event_key != previous_event:
                            symbol = base.get("symbol") or base.get("name") or "TOKEN"
                            smart_notice = (list(entry.get("subscribers", [])),
                                            f"{movement_icon(change)} {signed_percent(change)} • {symbol}\n\n"
                                            f"🧠 Score: {result.get('score') if result.get('score') is not None else '—'}/100\n"
                                            f"{result.get('state')}\n"
                                            f"Setup: {result.get('entry', {}).get('type', 'Dados insuficientes')}\n"
                                            f"Confiança dos dados: {result.get('confidence', 0)}% · não é probabilidade de lucro.",
                                            {"inline_keyboard": [[button("🧠 Analisar", f"ia:{address}"),
                                                                  button("📈 Entrada", f"ie:{address}")],
                                                                 [button("💼 Minha posição", f"ip:{address}")] ]},
                                            [(str(uid), dict(wallet_entry)) for uid, wallet_entry in state.get("wallets", {}).items()
                                             if str(wallet_entry.get("chat_id")) in entry.get("subscribers", [])
                                             and str(wallet_entry.get("chat_id")) == str(uid)])
                            entry["last_smart_signal"] = event_key
                        elif not event_key:
                            entry["last_smart_signal"] = None
                    if abs(change) < threshold or now - float(entry.get("last_alert", 0)) < cooldown:
                        subscribers = []
                    else:
                        subscribers = list(entry.get("subscribers", []))
                    flow_notice = None
                    if SMART_ALERTS_ENABLED and flow_event and flow_summary:
                        window = flow_summary.get("windows", {}).get("15m", {})
                        side = flow_event["side"]
                        symbol = base.get("symbol") or base.get("name") or "TOKEN"
                        if flow_event["type"] == "large_sequence":
                            headline = "🐋 COMPRAS GRANDES EM SEQUÊNCIA" if side == "buy" else "🚨 VENDAS GRANDES EM SEQUÊNCIA"
                        elif flow_event["type"] == "acceleration":
                            headline = "🔥 FLUXO COMPRADOR ACELERANDO" if side == "buy" else "🔥 FLUXO VENDEDOR ACELERANDO"
                        elif flow_event["type"] == "large_trade":
                            headline = "🔥 COMPRA GRANDE" if side == "buy" else "🚨 VENDA GRANDE"
                        else:
                            headline = "🔥 FLUXO COMPRADOR" if side == "buy" else "🚨 FLUXO VENDEDOR"
                        liquidity = intelligence.number((pair.get("liquidity") or {}).get("usd"))
                        share = f"\n📊 ~{flow_event['usd'] / liquidity * 100:.1f}% da liquidez" if liquidity and liquidity > 0 else ""
                        price_delta = flow_summary.get("price_change_pct")
                        price_line = f"\n📈 Preço: {price_delta:+.2f}%" if price_delta is not None else ""
                        score = result.get("score") if INTELLIGENCE_ENABLED else None
                        flow_lines = [f"🐋 {symbol}", "", headline, "", "Últimos 15 min:",
                                      f"🟢 Compras: {wallet_usd(window.get('buy_usd', 0))}",
                                      f"🔴 Vendas: {wallet_usd(window.get('sell_usd', 0))}",
                                      f"💵 Líquido: {wallet_usd(window.get('net_usd', 0))}{share}{price_line}"]
                        if flow_event["type"] in {"large_trade", "large_sequence"}:
                            flow_lines.append(f"⚡ Operações relevantes agrupadas: {flow_event['count']}")
                        if score is not None:
                            flow_lines.append(f"\n🧠 Score: {score}/100")
                        if INTELLIGENCE_ENABLED and result.get("exit_risk") is not None:
                            flow_lines.append(f"🚨 Exit Risk: {result['exit_risk']}/100")
                        flow_text = "\n".join(flow_lines)
                        flow_wallets = [(str(uid), dict(wallet_entry)) for uid, wallet_entry in state.get("wallets", {}).items()
                                        if str(wallet_entry.get("chat_id")) in entry.get("subscribers", [])]
                        flow_notice = (list(entry.get("subscribers", [])), flow_text,
                                       {"inline_keyboard": [[button("🧠 Analisar", f"ia:{address}")]]}, flow_wallets)
                if smart_notice:
                    smart_subscribers, smart_text, smart_markup, smart_wallets = smart_notice
                    for chat_id in smart_subscribers:
                        personalized_text = smart_text
                        for _wallet_user, wallet_entry in smart_wallets:
                            if str(wallet_entry.get("chat_id")) != str(chat_id):
                                continue
                            try:
                                amount, _decimals, position = wallet_token_position(wallet_entry["address"], address)
                                market_price = Decimal(str(pair["priceUsd"]))
                                if amount > 0 and market_price.is_finite() and market_price > 0:
                                    personalized_text += (f"\n\n💼 ALERTA DA SUA CARTEIRA\nSua posição: {wallet_amount(amount)} tokens · "
                                                         f"{wallet_usd(amount * market_price)}.")
                                    if position and position.get("pnl_usd") is not None:
                                        personalized_text += f"\nPnL não realizado: {wallet_usd(position['pnl_usd'])}"
                                        if position.get("pnl_percent") is not None:
                                            personalized_text += f" ({wallet_percent(position['pnl_percent'])}%)"
                                    else:
                                        personalized_text += "\nPnL por posição: indisponível."
                            except (solana_wallet.WalletRpcError, InvalidOperation, TypeError, ValueError) as exc:
                                LOG.info("[WALLET] Não foi possível anexar posição ao alerta inteligente: %s", exc)
                        try:
                            send_message(token, chat_id, personalized_text, smart_markup)
                        except TelegramError as exc:
                            LOG.warning("Telegram não entregou alerta inteligente: %s", exc)
                if flow_notice:
                    flow_subscribers, flow_text, flow_markup, flow_wallets = flow_notice
                    for chat_id in flow_subscribers:
                        personalized_text = flow_text
                        for _wallet_user, wallet_entry in flow_wallets:
                            if str(wallet_entry.get("chat_id")) != str(chat_id):
                                continue
                            try:
                                amount, _decimals, position = wallet_token_position(wallet_entry["address"], address)
                                market_price = Decimal(str(pair["priceUsd"]))
                                if amount > 0 and market_price.is_finite() and market_price > 0:
                                    personalized_text = ("🚨 ALERTA DA SUA POSIÇÃO\n\n" + personalized_text
                                                         + f"\n\n💼 Valor atual: {wallet_usd(amount * market_price)}.")
                                    if position and position.get("pnl_usd") is not None:
                                        personalized_text += f"\nPnL não realizado: {wallet_usd(position['pnl_usd'])}"
                                        if position.get("pnl_percent") is not None:
                                            personalized_text += f" ({wallet_percent(position['pnl_percent'])}%)"
                                    else:
                                        personalized_text += "\nPnL por posição: indisponível."
                            except (solana_wallet.WalletRpcError, InvalidOperation, TypeError, ValueError) as exc:
                                LOG.info("[WALLET] Não foi possível anexar posição ao alerta de fluxo: %s", exc)
                        try:
                            send_message(token, chat_id, personalized_text, flow_markup)
                        except TelegramError as exc:
                            LOG.warning("Telegram não entregou alerta de fluxo para %s: %s", chat_id, exc)
                if not subscribers:
                    continue
                alert_token = base.get("symbol") or base.get("name") or "TOKEN"
                text = f"{movement_icon(change)} {signed_percent(change)} • {alert_token}\n\n" + pair_summary(pair, alert_change=change)
                delivered = False
                for chat_id in subscribers:
                    try:
                        send_message(token, chat_id, text, pair_links_markup(pair))
                        delivered = True
                    except TelegramError as exc:
                        LOG.warning("Telegram não entregou alerta; será tentado novamente no próximo ciclo: %s", exc)
                if delivered:
                    with STATE_LOCK:
                        entry = state["watches"].get(key)
                        if entry is not None:
                            entry["anchor_price"] = price
                            entry["reference_price"] = price
                            entry["last_alert"] = now
            except (TypeError, ValueError, OverflowError, KeyError) as exc:
                LOG.warning("Dados de preço inválidos para token monitorado %s; ignorando neste ciclo: %s", address, exc)


def wallet_movement_text(address: str, record: dict[str, Any]) -> str:
    lines = ["🚨 MOVIMENTAÇÃO NA CARTEIRA", "", record.get("classification", "🔄 Movimentação detectada"), ""]
    assets = record.get("assets") or []
    if assets:
        asset = assets[0]
        quantity = wallet_amount(asset.get("amount"))
        sign = "+" if not quantity.startswith("-") else ""
        lines.extend(["🪙 Token: Token não identificado",
                      f"Quantidade: {sign}{quantity} · {wallet_short_address(asset.get('mint', ''))}"])
    else:
        lines.append("🪙 Token não identificado")
    lines.extend(["", "💵 Valor: indisponível", f"💼 Carteira: {wallet_short_address(address)}"])
    return "\n".join(lines)


def solscan_markup(signature: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "🔎 Ver no Solscan", "url": f"https://solscan.io/tx/{urllib.parse.quote(signature, safe='')}"}]]}


def check_wallets(state: dict[str, Any], token: str) -> None:
    """Check read-only wallet signatures; uncertain transaction types stay generic."""
    with STATE_LOCK:
        wallets = [(str(user_id), dict(entry)) for user_id, entry in state.setdefault("wallets", {}).items()
                   if isinstance(entry, dict) and entry.get("monitoring_enabled", True)]
    for user_id, wallet in wallets:
        address = wallet.get("address")
        if not is_valid_solana_address(address):
            continue
        LOG.info("[WALLET] Checking wallet %s", wallet_short_address(address))
        try:
            signatures = solana_wallet.get_wallet_signatures(address)
            if not signatures:
                with STATE_LOCK:
                    current = state["wallets"].get(user_id)
                    if current:
                        current["last_check"] = time.time()
                continue
            latest_signature = signatures[0]["signature"]
            last_signature = wallet.get("last_signature")
            if not last_signature:
                registered_at = float(wallet.get("registered_at") or 0)
                new_rows = [row for row in signatures if isinstance(row.get("blockTime"), (int, float))
                            and row["blockTime"] >= int(registered_at)] if registered_at else []
                new_rows.reverse()
                if not new_rows:
                    with STATE_LOCK:
                        current = state["wallets"].get(user_id)
                        if current:
                            current["last_signature"] = latest_signature
                            current["last_check"] = time.time()
                            save_state(state)
                    LOG.info("[WALLET] Baseline signature saved for wallet %s", wallet_short_address(address))
                    continue
            else:
                new_rows = []
                for row in signatures:
                    if row.get("signature") == last_signature:
                        break
                    new_rows.append(row)
                new_rows.reverse()
            # Cap per-cycle notifications so a busy wallet cannot flood Telegram.
            for row in new_rows[:5]:
                signature = row["signature"]
                LOG.info("[WALLET] New transaction detected for wallet %s", wallet_short_address(address))
                transaction = solana_wallet.get_wallet_transaction(signature)
                if transaction is None:
                    LOG.warning("[WALLET] Transaction not indexed yet; will retry on the next check")
                    break
                meta = transaction.get("meta") or {}
                if meta.get("err") is not None:
                    with STATE_LOCK:
                        current = state["wallets"].get(user_id)
                        if current:
                            current["last_signature"] = signature
                            current["last_check"] = time.time()
                            save_state(state)
                    continue
                record = {
                    "signature": signature,
                    "classification": solana_wallet.classify_transaction(transaction),
                    "timestamp": transaction.get("blockTime") or row.get("blockTime") or time.time(),
                    "assets": solana_wallet.extract_token_changes(transaction, address),
                }
                LOG.info("[WALLET] Transaction classified as generic movement")
                current = state["wallets"].get(user_id)
                if current and current.get("alerts_enabled", True):
                    try:
                        send_message(token, current.get("chat_id") or user_id,
                                     wallet_movement_text(address, record), solscan_markup(signature))
                    except TelegramError as exc:
                        LOG.warning("[WALLET] Could not deliver movement alert; will retry: %s", exc)
                        break
                    LOG.info("[WALLET] Alert sent for wallet %s", wallet_short_address(address))
                with STATE_LOCK:
                    current = state["wallets"].get(user_id)
                    if current:
                        current.setdefault("history", []).insert(0, record)
                        current["history"] = current["history"][:20]
                        current["last_signature"] = signature
                        current["last_check"] = time.time()
                        save_state(state)
        except solana_wallet.WalletRpcError as exc:
            LOG.warning("[WALLET] RPC error for wallet %s: %s", wallet_short_address(address), exc)
        except TelegramError as exc:
            LOG.warning("[WALLET] Telegram error while processing wallet %s: %s", wallet_short_address(address), exc)
        except Exception:
            LOG.exception("[WALLET] Unexpected error while checking wallet %s", wallet_short_address(address))


def market_monitor(state: dict[str, Any], token: str, threshold: float, cooldown: float,
                   wallet_interval: float = WALLET_CHECK_INTERVAL_SECONDS) -> None:
    failures = 0
    last_wallet_check = 0.0
    last_price_check = 0.0
    while not STOP.is_set():
        with STATE_LOCK:
            has_watches = bool(state.setdefault("watches", {}))
            has_wallets = bool(state.setdefault("wallets", {}))
        try:
            now = time.monotonic()
            if has_watches and now - last_price_check >= PRICE_CHECK_SECONDS:
                check_prices(state, token, threshold, cooldown)
                last_price_check = now
            if has_wallets and now - last_wallet_check >= wallet_interval:
                check_wallets(state, token)
                last_wallet_check = now
            failures = 0
            with STATE_LOCK:
                save_state(state)
            deadlines = []
            if has_watches:
                deadlines.append(PRICE_CHECK_SECONDS - (time.monotonic() - last_price_check))
            if has_wallets:
                deadlines.append(wallet_interval - (time.monotonic() - last_wallet_check))
            wait_seconds = max(1, min(deadlines)) if deadlines else PRICE_CHECK_SECONDS
            STOP.wait(wait_seconds)
        except Exception as exc:
            failures += 1
            delay = min(300, 10 * (2 ** min(failures - 1, 5)))
            LOG.exception("Erro no ciclo de monitoramento; nova tentativa em %ss: %s", delay, exc)
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
        wallet_interval = env_float("WALLET_CHECK_INTERVAL_SECONDS", 60, 15, 3600)
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
            try:
                configure_bot_commands(token)
            except TelegramUnauthorized:
                raise
            except TelegramError as exc:
                LOG.warning("Não foi possível atualizar o menu de comandos do Telegram: %s", exc)
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

    LOG.info("Worker contínuo iniciado; tokens monitorados: %d; carteiras: %d", len(state["watches"]), len(state.get("wallets", {})))
    monitor = threading.Thread(target=market_monitor, args=(state, token, threshold, cooldown, wallet_interval), name="market-monitor", daemon=True)
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
            updates = telegram(token, "getUpdates", {"offset": poll_offset, "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS, "allowed_updates": ["message", "callback_query"]}) or []
            if updates:
                first_poll = False
            failures = 0
            for update in updates:
                try:
                    if update.get("callback_query"):
                        handle_callback_update(update, state, token, threshold, allowed_ids)
                    else:
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
