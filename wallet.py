"""Read-only Solana public-wallet queries. This module never signs or sends transactions."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

LOG = logging.getLogger("crypto_alert_bot.wallet")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip() or "https://api.mainnet-beta.solana.com"
LAMPORTS_PER_SOL = Decimal("1000000000")
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


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


def get_wallet_transaction(signature: str) -> dict[str, Any] | None:
    result = solana_rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
    return result if isinstance(result, dict) else None


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


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


def get_wallet_info(address: str, market_data: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
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
        pair = market_data("So11111111111111111111111111111111111111112")
        sol_price = _decimal(pair.get("priceUsd")) if pair else None
    except Exception as exc:
        LOG.info("[WALLET] Cotação pública de SOL indisponível: %s", exc)

    tokens = []
    # Limit public market lookups to the five largest raw token quantities.
    for mint, (amount, decimals) in sorted(token_accounts.items(), key=lambda item: item[1][0], reverse=True)[:5]:
        pair = None
        try:
            pair = market_data(mint)
        except Exception as exc:
            LOG.info("[WALLET] Cotação indisponível para mint %s: %s", mint, exc)
        base = (pair or {}).get("baseToken") or {}
        price = _decimal((pair or {}).get("priceUsd"))
        tokens.append({
            "mint": mint,
            "amount": str(amount),
            "decimals": decimals,
            "name": base.get("name") or "Token não identificado",
            "symbol": base.get("symbol") or "",
            "price_usd": str(price) if price is not None else None,
            "value_usd": str(amount * price) if price is not None else None,
        })

    # Total is only shown when every held asset has a usable public quote.
    estimated = sol * sol_price if sol_price is not None else None
    if estimated is not None and len(token_accounts) <= 5 and all(item["value_usd"] is not None for item in tokens):
        estimated += sum((Decimal(item["value_usd"]) for item in tokens), Decimal(0))
    else:
        estimated = None
    return {"sol": str(sol), "sol_price_usd": str(sol_price) if sol_price is not None else None,
            "tokens": tokens, "token_count": len(token_accounts),
            "estimated_usd": str(estimated) if estimated is not None else None}


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
