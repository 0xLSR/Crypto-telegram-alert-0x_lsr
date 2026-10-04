"""Small read-only client for Nansen's documented Solana Profiler API."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

LOG = logging.getLogger("crypto_alert_bot.nansen")
BASE_URL = "https://api.nansen.ai/api/v1"
BALANCE_PATH = "/profiler/address/current-balance"
PNL_PATH = "/profiler/address/pnl-summary"
DEFI_PATH = "/portfolio/defi-holdings"
_BALANCE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PNL_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_DEFI_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_TOKEN_PNL_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_PNL_ERROR = object()


class NansenError(Exception):
    """Safe error without request headers or API credentials."""


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _request(path: str, body: dict[str, Any], api_key: str,
             opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE_URL + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "apikey": api_key, "User-Agent": "crypto-telegram-alert/1.0"},
    )
    open_url = opener or urllib.request.urlopen
    for attempt in range(3):
        try:
            with open_url(request, timeout=15) as response:
                try:
                    value = json.loads(response.read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    LOG.warning("[NANSEN] endpoint=%s retornou JSON inválido", path)
                    raise NansenError("A Nansen retornou uma resposta inválida.") from None
            break
        except urllib.error.HTTPError as exc:
            # API key is sent only in a header; never log exception/body/header contents.
            LOG.warning("[NANSEN] endpoint=%s HTTP %d", path, exc.code)
            if (exc.code == 429 or exc.code >= 500) and attempt < 2:
                try:
                    retry_after = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                except (TypeError, ValueError):
                    retry_after = 0
                time.sleep(min(3.0, max(0.25 * (2 ** attempt), retry_after)))
                continue
            raise NansenError(f"Nansen respondeu HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOG.warning("[NANSEN] endpoint=%s falha de rede (%s)", path, type(exc).__name__)
            if attempt < 2:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise NansenError(f"Falha de rede na Nansen ({type(exc).__name__}).") from None
    if not isinstance(value, dict):
        raise NansenError("A Nansen retornou uma resposta inválida.")
    return value


def fetch_balances(address: str, api_key: str, *, opener: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Read every page (API page limit 1000); never cut holdings to top N."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _request(BALANCE_PATH, {"address": address, "chain": "solana",
                                         "hide_spam_token": False,
                                         "pagination": {"page": page, "per_page": 1000}}, api_key, opener)
        data = payload.get("data")
        if not isinstance(data, list):
            raise NansenError("A resposta de saldos da Nansen tem formato inválido.")
        rows.extend(item for item in data if isinstance(item, dict))
        pagination = payload.get("pagination") or {}
        if not isinstance(pagination, dict) or pagination.get("is_last_page", len(data) < 1000) or not data:
            break
        page += 1
        if page > 1000:
            raise NansenError("A resposta de saldos da Nansen excedeu o limite de páginas.")
    return rows


def fetch_pnl_summary(address: str, api_key: str, *, now: datetime | None = None,
                      opener: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Fetch aggregate 90-day PnL; this is not per-position or 24h PnL."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=90)
    result = _request(PNL_PATH, {"address": address, "chain": "solana",
                                 "date": {"from": start.date().isoformat(), "to": end.date().isoformat()}}, api_key, opener)
    keys = ("realized_pnl_usd", "realized_pnl_percent", "win_rate", "traded_times", "traded_token_count", "top5_tokens")
    summary = {key: result[key] for key in keys if key in result}
    return summary or None


def fetch_token_pnl(address: str, mint: str, api_key: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Fetch documented token-specific realized/unrealized PnL and cost basis."""
    result = _request("/profiler/address/pnl", {
        "address": address, "chain": "solana",
        "filters": {"token_address": mint, "show_realized": True},
    }, api_key, opener)
    rows = result.get("data")
    if not isinstance(rows, list):
        raise NansenError("A resposta de PnL da Nansen tem formato inválido.")
    for row in rows:
        if not isinstance(row, dict) or row.get("token_address") != mint:
            continue
        normalized = {
            "pnl_usd": row.get("pnl_usd_unrealised"),
            "pnl_percent": row.get("roi_percent_unrealised"),
            "pnl_realized_usd": row.get("pnl_usd_realised"),
            "cost_basis_usd": row.get("cost_basis_usd"),
            "holding_usd": row.get("holding_usd"),
        }
        return {key: value for key, value in normalized.items() if value is not None}
    return None


def fetch_defi_holdings(address: str, api_key: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Read Nansen's documented DeFi positions and summary for a wallet."""
    result = _request(DEFI_PATH, {"wallet_address": address}, api_key, opener)
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return None
    total = _number(summary.get("total_value_usd"))
    return {"total_value_usd": str(total) if total is not None else None,
            "protocol_count": summary.get("protocol_count"),
            "token_count": summary.get("token_count"),
            "protocols": result.get("protocols") if isinstance(result.get("protocols"), list) else []}


def get_token_pnl(address: str, mint: str, api_key: str | None = None, *,
                  cache_seconds: int = 86400, opener: Callable[..., Any] | None = None,
                  now: float | None = None) -> dict[str, Any] | None:
    key = (api_key if api_key is not None else os.getenv("NANSEN_API_KEY", "")).strip()
    if not key:
        raise NansenError("NANSEN_API_KEY não configurada.")
    cache_key = (address, mint)
    timestamp = time.monotonic() if now is None else now
    cached = _TOKEN_PNL_CACHE.get(cache_key)
    if cached:
        ttl = 300 if cached[1] is _PNL_ERROR else max(0, cache_seconds)
        if timestamp - cached[0] < ttl:
            if cached[1] is _PNL_ERROR:
                return None
            return cached[1]
    try:
        result = fetch_token_pnl(address, mint, key, opener=opener)
    except NansenError:
        _TOKEN_PNL_CACHE[cache_key] = (timestamp, _PNL_ERROR)
        raise
    _TOKEN_PNL_CACHE[cache_key] = (timestamp, result)
    return result


def normalize_balances(address: str, rows: list[dict[str, Any]], pnl: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize by mint (never ticker), preserving missing price/value as None."""
    holdings: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = row.get("token_address")
        if not isinstance(mint, str) or not mint:
            continue
        amount = _number(row.get("token_amount"))
        if amount is None or amount <= 0:
            continue
        price, value = _number(row.get("price_usd")), _number(row.get("value_usd"))
        item = holdings.get(mint)
        if item is None:
            item = {"mint": mint, "symbol": str(row.get("token_symbol") or ""),
                    "name": str(row.get("token_name") or row.get("token_symbol") or "Token não identificado"),
                    "amount": "0", "price_usd": str(price) if price is not None else None,
                    "value_usd": None, "pnl_usd": None, "pnl_percent": None,
                    "cost_basis_usd": None, "allocation_percent": None,
                    "market_cap": None, "fdv": None, "liquidity_usd": None,
                    "volume_24h": None, "price_change_24h": None}
            holdings[mint] = item
        item["amount"] = str((_number(item["amount"]) or Decimal(0)) + amount)
        if value is not None:
            item["value_usd"] = str((_number(item["value_usd"]) or Decimal(0)) + value)
        elif price is not None:
            item["value_usd"] = str((_number(item["amount"]) or Decimal(0)) * price)
        if item["price_usd"] is None and price is not None:
            item["price_usd"] = str(price)
    tokens = list(holdings.values())
    total = sum((_number(item.get("value_usd")) or Decimal(0) for item in tokens if item.get("value_usd") is not None), Decimal(0))
    priced = sum(item.get("value_usd") is not None for item in tokens)
    for item in tokens:
        value = _number(item.get("value_usd"))
        item["allocation_percent"] = str(value * 100 / total) if value is not None and total else None
    return {"address": address, "sol": None, "sol_price_usd": None, "sol_value_usd": None,
            "tokens": tokens, "token_count": len(tokens), "unpriced_tokens": len(tokens) - priced,
            "estimated_usd": str(total) if priced else None, "portfolio_total_usd": None,
            "is_partial": priced < len(tokens), "source": "nansen", "pnl_summary": pnl,
            "pnl_usd": None, "pnl_percent": None}


def get_wallet_info(address: str, api_key: str | None = None, *, cache_seconds: int | None = None,
                    opener: Callable[..., Any] | None = None, now: float | None = None) -> dict[str, Any]:
    key = (api_key if api_key is not None else os.getenv("NANSEN_API_KEY", "")).strip()
    if not key:
        raise NansenError("NANSEN_API_KEY não configurada.")
    ttl = max(0, cache_seconds if cache_seconds is not None else _cache_seconds())
    timestamp = time.monotonic() if now is None else now
    cached = _BALANCE_CACHE.get(address)
    if cached and timestamp - cached[0] < ttl:
        return dict(cached[1])
    rows = fetch_balances(address, key, opener=opener)
    defi_entry = _DEFI_CACHE.get(address)
    defi_ttl = max(ttl, 86400) if defi_entry and defi_entry[1] is not None else max(ttl, 300)
    if defi_entry and timestamp - defi_entry[0] < defi_ttl:
        defi = defi_entry[1]
    else:
        try:
            defi = fetch_defi_holdings(address, key, opener=opener)
        except NansenError as exc:
            LOG.info("[NANSEN] DeFi holdings indisponíveis (%s); patrimônio DeFi não será somado", str(exc))
            defi = None
        _DEFI_CACHE[address] = (timestamp, defi)
    pnl_entry = _PNL_CACHE.get(address)
    pnl_ttl = max(ttl, 86400) if pnl_entry and pnl_entry[1] is not None else max(ttl, 300)
    if pnl_entry and timestamp - pnl_entry[0] < pnl_ttl:
        pnl = pnl_entry[1]
    else:
        try:
            pnl = fetch_pnl_summary(address, key, opener=opener)
        except NansenError as exc:
            LOG.info("[NANSEN] PnL aggregate indisponível (%s); holdings permanecem disponíveis", str(exc))
            pnl = None
        _PNL_CACHE[address] = (timestamp, pnl)
    normalized = normalize_balances(address, rows, pnl)
    normalized["defi"] = defi
    defi_total = _number((defi or {}).get("total_value_usd")) if defi else None
    wallet_total = _number(normalized.get("estimated_usd"))
    if wallet_total is None and normalized.get("token_count", 0) == 0:
        wallet_total = Decimal(0)
    normalized["portfolio_total_usd"] = str(wallet_total + defi_total) if wallet_total is not None and defi_total is not None else None
    normalized["portfolio_total_is_partial"] = (defi_total is None or normalized.get("is_partial", False))
    _BALANCE_CACHE[address] = (timestamp, normalized)
    return dict(normalized)


def _cache_seconds() -> int:
    try:
        return min(3600, max(0, int(os.getenv("NANSEN_CACHE_SECONDS", "60"))))
    except ValueError:
        return 60
