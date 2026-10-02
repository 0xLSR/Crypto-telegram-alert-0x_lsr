"""Telegram crypto alerts using the public DexScreener API."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.telegram.org/bot{token}/{method}"
DEX_BASE = "https://api.dexscreener.com/latest/dex"
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "data/state.json"))
POLL_SECONDS = 15
PRICE_CHECK_SECONDS = 60
MAX_WATCHES = 100
LOG = logging.getLogger("crypto_alert_bot")


class BotError(Exception):
    pass


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
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
        if not isinstance(value, dict):
            raise ValueError("invalid state")
        value.setdefault("offset", 0)
        value.setdefault("watches", {})
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise BotError("Não foi possível ler data/state.json; verifique o estado salvo.") from exc


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


def http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "crypto-telegram-alert/1.0", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        # Telegram embeds the bot token in the request path. Never log the URL,
        # and redact the token in case an upstream error body echoes the URL.
        path = urllib.parse.urlsplit(url).path
        token_match = re.search(r"/bot([^/]+)/", path)
        if token_match:
            token = token_match.group(1)
            response_body = response_body.replace(token, "[REDACTED]")
            response_body = response_body.replace(f"bot{token}", "bot[REDACTED]")
        LOG.error("Erro HTTP %s; corpo da resposta: %s", exc.code, response_body[:4000])
        raise BotError(f"Falha HTTP {exc.code} na API externa.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BotError("Falha na comunicação com a API externa.") from exc
    return result


def telegram(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    result = http_json(API_BASE.format(token=token, method=method), method="POST", payload=payload or {})
    if not result.get("ok"):
        error_code = result.get("error_code", "desconhecido")
        description = str(result.get("description") or "sem descrição")
        description = description.replace(token, "[REDACTED]")
        raise BotError(f"Telegram API {error_code}: {description}")
    return result.get("result")


def validate_telegram(token: str) -> None:
    """Validate credentials and make long polling available before getUpdates."""
    bot_info = telegram(token, "getMe")
    if not isinstance(bot_info, dict) or not bot_info.get("id"):
        raise BotError("Telegram getMe não retornou a identidade do bot.")
    webhook_removed = telegram(token, "deleteWebhook", {"drop_pending_updates": False})
    if webhook_removed is not True:
        raise BotError("Telegram não confirmou a remoção do webhook.")
    LOG.info(
        "Token validado com getMe (bot id=%s, username=@%s); webhook removido sem descartar atualizações pendentes.",
        bot_info["id"],
        bot_info.get("username", "indisponível"),
    )


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


def get_pair(query: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(query, safe="")
    endpoint = f"{DEX_BASE}/tokens/{encoded}" if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,50}", query) else f"{DEX_BASE}/search?q={encoded}"
    result = http_json(endpoint)
    pairs = [pair for pair in (result.get("pairs") or []) if pair.get("chainId") == "solana"]
    if not pairs:
        return None
    if endpoint.endswith("/search?q=" + encoded):
        exact = [p for p in pairs if query.lower() in {str(p.get("baseToken", {}).get("symbol", "")).lower(), str(p.get("baseToken", {}).get("name", "")).lower()}]
        if exact:
            pairs = exact
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def pair_summary(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    symbol = base.get("symbol") or base.get("name") or "Token"
    change = (pair.get("priceChange") or {}).get("h24")
    change_text = f"{float(change):+.2f}%" if change is not None else "indisponível"
    liquidity = (pair.get("liquidity") or {}).get("usd")
    volume = (pair.get("volume") or {}).get("h24")
    cap = pair.get("marketCap") or pair.get("fdv")
    return (f"{symbol} ({pair.get('chainId', 'Solana')})\n"
            f"Preço: {money(pair.get('priceUsd'))}\nVariação 24h: {change_text}\n"
            f"Liquidez: {compact(liquidity)} · Volume 24h: {compact(volume)}\n"
            f"Market cap/FDV: {compact(cap)}\nEndereço: {base.get('address', 'indisponível')}\n"
            f"Fonte: DexScreener — {pair.get('url', '')}")


def help_text() -> str:
    return ("Comandos disponíveis:\n"
            "/price <TOKEN ou endereço> — consultar preço\n"
            "/watch <endereço Solana> — monitorar token\n"
            "/unwatch <endereço> — parar monitoramento\n"
            "/list — listar tokens monitorados\n"
            "/help — mostrar esta ajuda\n\n"
            "Os alertas são enviados quando o preço se move pelo percentual configurado.")


def handle_update(update: dict[str, Any], state: dict[str, Any], token: str, threshold: float, allowed_ids: set[str]) -> None:
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
                send_message(token, chat_id, pair_summary(pair) if pair else "Token Solana não encontrado no DexScreener.")
        elif command == "/watch":
            if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,50}", arg):
                send_message(token, chat_id, "Informe um endereço Solana válido: /watch <endereço>")
                return
            pair = get_pair(arg)
            if not pair:
                send_message(token, chat_id, "Não encontrei um par Solana para esse endereço.")
                return
            address = pair["baseToken"]["address"]
            key = address.lower()
            if key not in watches and len(watches) >= MAX_WATCHES:
                send_message(token, chat_id, f"Limite de {MAX_WATCHES} tokens monitorados atingido.")
                return
            entry = watches.setdefault(key, {"address": address, "subscribers": [], "anchor_price": float(pair.get("priceUsd") or 0), "last_alert": 0})
            if str(chat_id) not in entry["subscribers"]:
                entry["subscribers"].append(str(chat_id))
            send_message(token, chat_id, "Monitoramento ativado (limiar " + f"{threshold:g}%" + ").\n" + pair_summary(pair))
        elif command == "/unwatch":
            if not arg:
                send_message(token, chat_id, "Uso: /unwatch <endereço>")
                return
            entry = watches.get(arg.lower())
            if entry is None:
                entry = next((item for item in watches.values() if item["address"].lower() == arg.lower()), None)
            if not entry or str(chat_id) not in entry["subscribers"]:
                send_message(token, chat_id, "Esse chat não monitora esse endereço.")
                return
            entry["subscribers"].remove(str(chat_id))
            if not entry["subscribers"]:
                watches.pop(entry["address"].lower(), None)
            send_message(token, chat_id, "Monitoramento removido.")
        elif command == "/list":
            entries = [entry for entry in watches.values() if str(chat_id) in entry["subscribers"]]
            send_message(token, chat_id, "Seus tokens monitorados:\n" + ("\n".join(f"• {e['address']}" for e in entries) if entries else "nenhum"))
    except BotError as exc:
        LOG.warning("Falha ao processar comando: %s", exc)
        send_message(token, chat_id, "Não consegui concluir o comando agora. Tente novamente mais tarde.")


def check_prices(state: dict[str, Any], token: str, threshold: float, cooldown: float) -> None:
    now = time.time()
    for key, entry in list(state["watches"].items()):
        try:
            pair = get_pair(entry["address"])
            if not pair or not pair.get("priceUsd"):
                continue
            price = float(pair["priceUsd"])
            anchor = float(entry.get("anchor_price") or price)
            if anchor <= 0:
                entry["anchor_price"] = price
                continue
            change = (price / anchor - 1) * 100
            if abs(change) >= threshold and now - float(entry.get("last_alert", 0)) >= cooldown:
                text = f"🚨 Alerta de preço: {change:+.2f}% desde o último alerta\n" + pair_summary(pair)
                for chat_id in entry.get("subscribers", []):
                    try:
                        send_message(token, chat_id, text)
                    except BotError:
                        LOG.warning("Não foi possível entregar um alerta para um chat.")
                entry["anchor_price"] = price
                entry["last_alert"] = now
        except (BotError, TypeError, ValueError) as exc:
            LOG.warning("Falha ao consultar um token monitorado: %s", exc)
        time.sleep(0.25)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOG.error("Configure TELEGRAM_BOT_TOKEN no ambiente.")
        return 2
    try:
        threshold = env_float("ALERT_THRESHOLD_PERCENT", 10.0, 0.1, 1000)
        cooldown = env_float("ALERT_COOLDOWN_MINUTES", 30.0, 1, 10080) * 60
        run_seconds = env_float("RUN_FOR_SECONDS", 0.0, 0, 86400)
    except BotError as exc:
        LOG.error("Configuração inválida: %s", exc)
        return 2
    try:
        validate_telegram(token)
    except BotError as exc:
        LOG.error("Falha na validação inicial do Telegram: %s", exc)
        return 2
    allowed_ids = {value.strip() for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if value.strip()}
    state = load_state()
    deadline = time.monotonic() + run_seconds if run_seconds else None
    next_price_check = 0.0
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    LOG.info("Bot iniciado; tokens monitorados: %d", len(state["watches"]))
    while running and (deadline is None or time.monotonic() < deadline):
        try:
            updates = telegram(token, "getUpdates", {"offset": int(state["offset"]), "timeout": 10, "allowed_updates": ["message"]})
            for update in updates:
                handle_update(update, state, token, threshold, allowed_ids)
                state["offset"] = update["update_id"] + 1
                save_state(state)
            if time.monotonic() >= next_price_check:
                check_prices(state, token, threshold, cooldown)
                save_state(state)
                next_price_check = time.monotonic() + PRICE_CHECK_SECONDS
        except BotError as exc:
            LOG.warning("Falha temporária de comunicação: %s", exc)
            time.sleep(POLL_SECONDS)
    save_state(state)
    LOG.info("Bot encerrado; estado salvo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
