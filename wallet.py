"""Read-only Solana public-wallet queries. This module never signs or sends transactions."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
import nansen_client

LOG = logging.getLogger("crypto_alert_bot.wallet")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip() or "https://api.mainnet-beta.solana.com"
LAMPORTS_PER_SOL = Decimal("1000000000")
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
PRICE_CACHE_TTL_SECONDS = 60
_PRICE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}


class WalletRpcError(Exception):
    """A safe, read-only RPC communication/response error."""


def solana_rpc(method: str, params: list[Any]) -> Any:
    """Call public JSON-RPC; only read methods are used by this feature."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        SOLANA_RPC_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "crypto-telegram-alert/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not log response bodies: custom RPC providers may echo credential-bearing URLs.
        LOG.warning("[WALLET] RPC error HTTP %s", exc.code)
        raise WalletRpcError(f"Solana RPC respondeu HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        reason = f"{type(exc.reason).__name__}: {exc.reason}"
        LOG.warning("[WALLET] RPC error de rede: %s", reason)
        raise WalletRpcError(f"Erro de rede no Solana RPC: {reason}") from exc
    except (TimeoutError, OSError) as exc:
        LOG.warning("[WALLET] RPC error de rede: %s: %s", type(exc).__name__, exc)
        raise WalletRpcError("Falha de rede no Solana RPC.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOG.warning("[WALLET] RPC error: resposta JSON inválida: %s", exc)
        raise WalletRpcError("O Solana RPC retornou uma resposta inválida.") from exc
    if not isinstance(result, dict):
        raise WalletRpcError("O Solana RPC retornou uma resposta inválida.")
    error = result.get("error")
    if error:
        code = error.get("code", "?") if isinstance(error, dict) else "?"
        message = error.get("message", "erro RPC") if isinstance(error, dict) else "erro RPC"
        LOG.warning("[WALLET] RPC error %s: %s", code, message)
        raise WalletRpcError(f"Solana RPC {code}: {message}")
    if "result" not in result:
        raise WalletRpcError("O Solana RPC não retornou resultado.")
    return result["result"]


def get_wallet_signatures(address: str, limit: int = 20) -> list[dict[str, Any]]:
    result = solana_rpc("getSignaturesForAddress", [address, {"limit": limit, "commitment": "confirmed"}])
    if not isinstance(result, list):
        raise WalletRpcError("O Solana RPC retornou um histórico inválido.")
    return [item for item in result if isinstance(item, dict) and item.get("signature")]


def get_token_balance(address: str, mint: str) -> tuple[Decimal, int]:
    """Read the SPL/Token-2022 balance for one mint without scanning market APIs."""
    total = Decimal(0)
    decimals = 0
    for program_id in TOKEN_PROGRAMS:
        result = solana_rpc("getTokenAccountsByOwner", [address, {"programId": program_id},
                                                          {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise WalletRpcError("O Solana RPC retornou contas de tokens em formato inválido.")
        for account in result["value"]:
            if isinstance(account, dict):
                parsed = _token_amount(account)
                if parsed and parsed[0] == mint:
                    _, amount, decimals = parsed
                    total += amount
    return total, decimals


def get_wallet_transaction(signature: str) -> dict[str, Any] | None:
    result = solana_rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
    return result if isinstance(result, dict) else None


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _price(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _token_amount(account: dict[str, Any]) -> tuple[str, Decimal, int] | None:
    try:
        info = account["account"]["data"]["parsed"]["info"]
        amount = info["tokenAmount"]
        mint = str(info["mint"])
        decimals = int(amount["decimals"])
        ui_amount = _decimal(amount.get("uiAmountString"))
        if ui_amount is None:
            raw = _decimal(amount.get("amount"))
            ui_amount = raw / (Decimal(10) ** decimals) if raw is not None else None
        if ui_amount is None or ui_amount <= 0:
            return None
        return mint, ui_amount, decimals
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _cached_market_data(mint: str, market_data: Callable[[str], dict[str, Any] | None]) -> dict[str, Any] | None:
    cached = _PRICE_CACHE.get(mint)
    now = time.monotonic()
    if cached and now - cached[0] < PRICE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        pair = market_data(mint)
    except Exception:
        _PRICE_CACHE[mint] = (now, None)
        raise
    _PRICE_CACHE[mint] = (now, pair)
    return pair


def get_rpc_wallet_info(address: str, market_data: Callable[[str], dict[str, Any] | None],
                        market_data_many: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Return SOL and SPL holdings; prices/valuation are included only when available."""
    balance = solana_rpc("getBalance", [address, {"commitment": "confirmed"}])
    try:
        sol = Decimal(int(balance["value"])) / LAMPORTS_PER_SOL
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise WalletRpcError("O Solana RPC retornou um saldo inválido.") from exc

    token_accounts: dict[str, tuple[Decimal, int]] = {}
    for program_id in TOKEN_PROGRAMS:
        result = solana_rpc("getTokenAccountsByOwner", [address, {"programId": program_id}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise WalletRpcError("O Solana RPC retornou contas de tokens em formato inválido.")
        for account in result["value"]:
            if not isinstance(account, dict):
                continue
            parsed = _token_amount(account)
            if parsed:
                mint, amount, decimals = parsed
                current, _ = token_accounts.get(mint, (Decimal(0), decimals))
                token_accounts[mint] = (current + amount, decimals)

    sol_price = None
    try:
        pair = _cached_market_data("So11111111111111111111111111111111111111112", market_data)
        sol_price = _price(pair.get("priceUsd")) if pair else None
    except Exception as exc:
        LOG.info("[WALLET] Cotação pública de SOL indisponível: %s", exc)

    tokens = []
    mints = list(token_accounts)
    quotes: dict[str, dict[str, Any] | None] = {}
    if market_data_many:
        # GeckoTerminal accepts batches of up to 30 addresses. Batch first to
        # reduce requests, then use the existing Gecko/Dex fallback per missing mint.
        for offset in range(0, len(mints), 30):
            batch = mints[offset:offset + 30]
            stale = [mint for mint in batch if mint not in _PRICE_CACHE or time.monotonic() - _PRICE_CACHE[mint][0] >= PRICE_CACHE_TTL_SECONDS]
            for mint in batch:
                cached = _PRICE_CACHE.get(mint)
                if cached and time.monotonic() - cached[0] < PRICE_CACHE_TTL_SECONDS:
                    quotes[mint] = cached[1]
            if stale:
                try:
                    batch_quotes = market_data_many(stale)
                    if isinstance(batch_quotes, dict):
                        for mint, pair in batch_quotes.items():
                            if mint in stale and pair and _price(pair.get("priceUsd")) is not None:
                                _PRICE_CACHE[mint] = (time.monotonic(), pair)
                                quotes[mint] = pair
                except Exception as exc:
                    LOG.info("[WALLET] Cotação em lote indisponível para %d tokens: %s", len(stale), exc)
    for mint, (amount, decimals) in token_accounts.items():
        pair = quotes.get(mint)
        if pair is None:
            try:
                pair = _cached_market_data(mint, market_data)
            except Exception as exc:
                LOG.info("[WALLET] Cotação indisponível para mint %s: %s", mint, exc)
        base = (pair or {}).get("baseToken") or {}
        price = _price((pair or {}).get("priceUsd"))
        tokens.append({
            "mint": mint,
            "amount": str(amount),
            "decimals": decimals,
            "name": base.get("name") or "Token não identificado",
            "symbol": base.get("symbol") or "",
            "price_usd": str(price) if price is not None else None,
            "value_usd": str(amount * price) if price is not None else None,
            "market_cap": (pair or {}).get("marketCap"),
            "fdv": (pair or {}).get("fdv"),
            "liquidity_usd": ((pair or {}).get("liquidity") or {}).get("usd"),
            "volume_24h": ((pair or {}).get("volume") or {}).get("h24"),
            "price_change_24h": ((pair or {}).get("priceChange") or {}).get("h24"),
        })

    sol_value = sol * sol_price if sol_price is not None else None
    priced_values = [Decimal(item["value_usd"]) for item in tokens if item["value_usd"] is not None]
    if sol_value is not None:
        priced_values.append(sol_value)
    estimated = sum(priced_values, Decimal(0)) if priced_values else None
    unpriced_tokens = sum(item["value_usd"] is None for item in tokens)
    return {"sol": str(sol), "sol_price_usd": str(sol_price) if sol_price is not None else None,
            "sol_value_usd": str(sol_value) if sol_value is not None else None,
            "tokens": tokens, "token_count": len(token_accounts), "unpriced_tokens": unpriced_tokens,
            "estimated_usd": str(estimated) if estimated is not None else None,
            "is_partial": unpriced_tokens > 0 or sol_value is None, "source": "rpc",
            "portfolio_total_usd": None, "pnl_summary": None, "pnl_usd": None, "pnl_percent": None}


def get_wallet_info(address: str, market_data: Callable[[str], dict[str, Any] | None] | None = None,
                    market_data_many: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Prefer Nansen Profiler balances; transparently fall back to public Solana RPC."""
    enabled = os.getenv("NANSEN_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    api_key = os.getenv("NANSEN_API_KEY", "").strip()
    if enabled and api_key:
        try:
            info = nansen_client.get_wallet_info(address, api_key)
            _enrich_nansen_market_data(info, market_data_many)
            return info
        except nansen_client.NansenError as exc:
            LOG.warning("[WALLET] Nansen indisponível para %s; usando RPC fallback: %s",
                        address[:5] + "…" + address[-4:], exc)
    elif enabled:
        LOG.info("[WALLET] NANSEN_API_KEY ausente; usando RPC fallback")
    else:
        LOG.info("[WALLET] NANSEN_ENABLED=false; usando RPC configurado")
    if market_data is None:
        raise WalletRpcError("Dados Nansen indisponíveis e RPC sem serviço de cotação configurado.")
    return get_rpc_wallet_info(address, market_data, market_data_many)


def _enrich_nansen_market_data(info: dict[str, Any], market_data_many: Callable[[list[str]], dict[str, dict[str, Any]]] | None) -> None:
    """Fill market-only fields from the existing market provider without replacing Nansen values."""
    if not market_data_many:
        return
    tokens = info.get("tokens")
    if not isinstance(tokens, list):
        return
    mints = list(dict.fromkeys(item["mint"] for item in tokens
                               if isinstance(item, dict) and isinstance(item.get("mint"), str)))
    for offset in range(0, len(mints), 30):
        batch = mints[offset:offset + 30]
        try:
            quotes = market_data_many(batch)
        except Exception as exc:
            LOG.info("[WALLET] Mercado não enriqueceu dados de carteira (%s)", type(exc).__name__)
            continue
        if not isinstance(quotes, dict):
            continue
        for item in tokens:
            if not isinstance(item, dict) or item.get("mint") not in batch:
                continue
            pair = quotes.get(item["mint"])
            if not isinstance(pair, dict):
                continue
            base = pair.get("baseToken") or {}
            price = _price(pair.get("priceUsd"))
            if item.get("price_usd") is None and price is not None:
                item["price_usd"] = str(price)
            if item.get("value_usd") is None and price is not None:
                amount = _decimal(item.get("amount"))
                if amount is not None:
                    item["value_usd"] = str(amount * price)
            for key, value in (("market_cap", pair.get("marketCap")), ("fdv", pair.get("fdv")),
                               ("liquidity_usd", (pair.get("liquidity") or {}).get("usd")),
                               ("volume_24h", (pair.get("volume") or {}).get("h24")),
                               ("price_change_24h", (pair.get("priceChange") or {}).get("h24"))):
                if item.get(key) is None and value is not None:
                    item[key] = value
            if not item.get("name") and base.get("name"):
                item["name"] = base["name"]
            if not item.get("symbol") and base.get("symbol"):
                item["symbol"] = base["symbol"]
    priced = [item for item in tokens if item.get("value_usd") is not None]
    values = [_decimal(item.get("value_usd")) for item in priced]
    info["estimated_usd"] = str(sum((value for value in values if value is not None), Decimal(0))) if priced else None
    info["unpriced_tokens"] = len(tokens) - len(priced)
    info["is_partial"] = (info["unpriced_tokens"] > 0 or info.get("portfolio_total_is_partial", False))
    total = _decimal(info.get("portfolio_total_usd")) or _decimal(info.get("estimated_usd"))
    for item in tokens:
        value = _decimal(item.get("value_usd"))
        item["allocation_percent"] = str(value * 100 / total) if value is not None and total else None


def extract_token_changes(transaction: dict[str, Any], wallet_address: str) -> list[dict[str, str]]:
    """Extract public token balance deltas without inferring buy/sell semantics."""
    meta = transaction.get("meta") or {}
    before: dict[str, tuple[Decimal, int]] = {}
    after: dict[str, tuple[Decimal, int]] = {}
    for field, target in (("preTokenBalances", before), ("postTokenBalances", after)):
        for item in meta.get(field) or []:
            if not isinstance(item, dict) or item.get("owner") != wallet_address or not item.get("mint"):
                continue
            amount = item.get("uiTokenAmount") or {}
            raw = _decimal(amount.get("uiAmountString"))
            if raw is None:
                raw_amount = _decimal(amount.get("amount"))
                try:
                    decimals = int(amount.get("decimals", 0))
                except (TypeError, ValueError):
                    continue
                raw = raw_amount / (Decimal(10) ** decimals) if raw_amount is not None else None
            else:
                try:
                    decimals = int(amount.get("decimals", 0))
                except (TypeError, ValueError):
                    continue
            if raw is not None:
                current, _ = target.get(str(item["mint"]), (Decimal(0), decimals))
                target[str(item["mint"])] = (current + raw, decimals)
    changes = []
    for mint in set(before) | set(after):
        previous, decimals = before.get(mint, (Decimal(0), after.get(mint, (Decimal(0), 0))[1]))
        current, _ = after.get(mint, (Decimal(0), decimals))
        delta = current - previous
        if delta:
            changes.append({"mint": mint, "amount": str(delta), "decimals": str(decimals)})
    return changes[:3]


def classify_transaction(_transaction: dict[str, Any]) -> str:
    """Public RPC alone is ambiguous for swaps vs. transfers; do not guess a trade type."""
    return "🔄 Movimentação detectada"
