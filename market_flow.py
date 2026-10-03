"""Read-only trade-flow analysis built from GeckoTerminal's documented pool trades API."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any

WINDOWS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
MAX_STORED_TRADES = 1000


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> float | None:
    number = _number(value)
    if number is not None and number > 0:
        return number
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def parse_trades(response: Any, token_address: str) -> list[dict[str, Any]]:
    """Normalize only documented Gecko trade fields; unknown `kind` stays indeterminate."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ValueError("Resposta de trades GeckoTerminal inválida.")
    trades = []
    for row in response["data"]:
        if not isinstance(row, dict) or row.get("type") != "trade":
            continue
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            continue
        timestamp = _timestamp(attrs.get("block_timestamp"))
        usd = _number(attrs.get("volume_in_usd"))
        trade_id = row.get("id") or attrs.get("tx_hash")
        if timestamp is None or usd is None or usd <= 0 or not isinstance(trade_id, str):
            continue
        kind = str(attrs.get("kind", "")).casefold()
        # Querying with token=<mint> asks GeckoTerminal to orient trades to this token.
        if kind not in {"buy", "sell"}:
            kind = "unknown"
        trades.append({"id": trade_id[:180], "timestamp": timestamp, "usd": usd, "side": kind})
    return trades


def summarize(trades: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else datetime.now().timestamp()
    result: dict[str, Any] = {"available": False, "windows": {}, "trade_count": 0,
                              "large_buys": 0, "large_sells": 0, "acceleration": {},
                              "sequence": None, "divergence": None}
    valid = [row for row in trades if _number(row.get("timestamp")) is not None
             and _number(row.get("usd")) is not None and row["usd"] > 0]
    if not valid:
        return result
    result["available"] = True
    result["trade_count"] = len(valid)
    for name, seconds in WINDOWS.items():
        rows = [row for row in valid if 0 <= now - row["timestamp"] <= seconds]
        buys = sum(row["usd"] for row in rows if row.get("side") == "buy")
        sells = sum(row["usd"] for row in rows if row.get("side") == "sell")
        known = sum(1 for row in rows if row.get("side") in {"buy", "sell"})
        if rows:
            result["windows"][name] = {"buy_usd": buys, "sell_usd": sells,
                                       "net_usd": buys - sells, "known_trades": known,
                                       "unknown_trades": len(rows) - known}
    return result


def classify_trade(usd: float, liquidity_usd: float | None, recent_volume_usd: float | None) -> str:
    """Classify by reserve/turnover share, not an absolute USD cutoff."""
    if usd <= 0:
        return "small"
    liquidity_share = usd / liquidity_usd if liquidity_usd and liquidity_usd > 0 else 0.0
    turnover_share = usd / recent_volume_usd if recent_volume_usd and recent_volume_usd > 0 else 0.0
    relative = max(liquidity_share, turnover_share)
    if relative >= 0.05:
        return "very_large"
    if relative >= 0.01:
        return "large"
    if relative >= 0.001:
        return "medium"
    return "small"


def _trade_class(usd: float, liquidity: float | None, volume: float | None) -> str:
    return classify_trade(usd, liquidity, volume)


def sanitize_flow(flow: Any) -> dict[str, Any]:
    """Keep only bounded, validated fields for safe backward-compatible state loading."""
    if not isinstance(flow, dict):
        return {}
    raw = flow.get("recent_trades", [])
    trades = []
    if isinstance(raw, list):
        for row in raw[-MAX_STORED_TRADES:]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            timestamp, usd = _number(row.get("timestamp")), _number(row.get("usd"))
            if timestamp is None or usd is None or usd <= 0:
                continue
            side = row.get("side") if row.get("side") in {"buy", "sell", "unknown"} else "unknown"
            category = row.get("size_class") if row.get("size_class") in {"small", "medium", "large", "very_large"} else None
            clean = {"id": row["id"][:180], "timestamp": timestamp, "usd": usd, "side": side}
            if category:
                clean["size_class"] = category
            trades.append(clean)
    result = {"recent_trades": trades, "last_check": _number(flow.get("last_check")),
              "last_fetch_at": _number(flow.get("last_fetch_at")),
              "last_error": str(flow.get("last_error", ""))[:300],
              "last_alert_at": _number(flow.get("last_alert_at")) or 0,
              "last_event_signature": str(flow.get("last_event_signature", ""))[:500],
              "available": bool(flow.get("available", False)),
              "initialized": bool(flow.get("initialized", False)),
              "possibly_truncated": bool(flow.get("possibly_truncated", False))}
    if trades:
        result["summary"] = summarize(trades)
    return result


def update_flow(flow: dict[str, Any] | None, incoming: list[dict[str, Any]], *, now: float,
                liquidity_usd: float | None, previous_price: float | None = None,
                current_price: float | None = None, cooldown_seconds: float = 1800) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Dedupe new trades, retain bounded 24h history, summarize and emit one cooled event."""
    state = dict(flow) if isinstance(flow, dict) else {}
    initializing = not state.get("initialized", False)
    state.pop("last_error", None)
    old = state.get("recent_trades", [])
    history = [dict(item) for item in old if isinstance(item, dict)] if isinstance(old, list) else []
    prior_volume_15m = sum(float(row["usd"]) for row in history
                           if _number(row.get("timestamp")) is not None
                           and 0 <= now - float(row["timestamp"]) <= 900
                           and _number(row.get("usd")) is not None)
    known_ids = {row.get("id") for row in history}
    new = []
    for row in incoming:
        if not isinstance(row, dict) or row.get("id") in known_ids:
            continue
        if _number(row.get("timestamp")) is None or _number(row.get("usd")) is None or row["usd"] <= 0:
            continue
        known_ids.add(row["id"])
        new.append(dict(row))
    history.extend(new)
    history = [row for row in history if 0 <= now - float(row["timestamp"]) <= 86400][-MAX_STORED_TRADES:]
    history.sort(key=lambda row: float(row["timestamp"]))
    summary = summarize(history, now)
    window15 = summary["windows"].get("15m")
    baseline_volume = (window15["buy_usd"] + window15["sell_usd"]) if window15 else None
    recent_volume = prior_volume_15m or (baseline_volume if initializing else None)
    large_new = []
    for row in new:
        category = _trade_class(float(row["usd"]), liquidity_usd, recent_volume)
        row["size_class"] = category
        if (row.get("side") in {"buy", "sell"} and category in {"large", "very_large"}
                and 0 <= now - float(row["timestamp"]) <= 900):
            large_new.append(row)
    # Keep classifications for recent trades so alerts are deduplicated across process restarts.
    for row in history:
        if not row.get("size_class"):
            row["size_class"] = _trade_class(float(row["usd"]), liquidity_usd, recent_volume)
    summary["large_buys"] = sum(r["usd"] for r in history if r.get("side") == "buy" and r.get("size_class") in {"large", "very_large"} and now-r["timestamp"] <= 900)
    summary["large_sells"] = sum(r["usd"] for r in history if r.get("side") == "sell" and r.get("size_class") in {"large", "very_large"} and now-r["timestamp"] <= 900)
    summary["liquidity_share_15m"] = ((summary["large_buys"] + summary["large_sells"]) / liquidity_usd
                                      if liquidity_usd and liquidity_usd > 0 else None)
    recent5 = [r for r in history if 0 <= now-r["timestamp"] <= 300 and r.get("side") in {"buy", "sell"}]
    previous5 = [r for r in history if 300 < now-r["timestamp"] <= 600 and r.get("side") in {"buy", "sell"}]
    acceleration = {}
    from_zero = []
    for side in ("buy", "sell"):
        current_total = sum(r["usd"] for r in recent5 if r["side"] == side)
        prior_total = sum(r["usd"] for r in previous5 if r["side"] == side)
        if prior_total > 0:
            acceleration[side] = current_total / prior_total
        elif current_total > 0:
            from_zero.append(side)
    summary["acceleration"] = acceleration
    summary["acceleration_from_zero"] = from_zero
    seq = [r for r in history if 0 <= now-r["timestamp"] <= 900
           and r.get("side") in {"buy", "sell"} and r.get("size_class") in {"large", "very_large"}]
    sequence = seq[-3:]
    summary["sequence"] = sequence[-1]["side"] if len(sequence) == 3 and len({r["side"] for r in sequence}) == 1 else None
    if previous_price and current_price and previous_price > 0:
        summary["price_change_pct"] = (current_price / previous_price - 1) * 100
        if window15 and window15["known_trades"]:
            net = window15["net_usd"]
            summary["divergence"] = ("price_up_selling" if summary["price_change_pct"] > 1 and net < 0
                                      else "price_down_buying" if summary["price_change_pct"] < -1 and net > 0
                                      else "price_up_buying" if summary["price_change_pct"] > 1 and net > 0
                                      else "price_down_selling" if summary["price_change_pct"] < -1 and net < 0
                                      else None)
    event = None
    new_recent = [row for row in new if 0 <= now - float(row["timestamp"]) <= 900]
    if new_recent and window15 and not initializing:
        net = window15["net_usd"]
        # Smart flow only fires on observed relative-size trades or sustained net flow >1% reserves.
        directional = ("buy" if net > 0 else "sell" if net < 0 else None)
        if (summary["sequence"]
                and any(row["id"] in {item["id"] for item in new_recent} for row in sequence)):
            side = summary["sequence"]
            selected = sequence
            event = {"type": "large_sequence", "side": side, "usd": sum(r["usd"] for r in selected),
                     "count": len(selected), "trade_ids": [r["id"] for r in selected]}
        elif large_new:
            side_totals = {side: sum(r["usd"] for r in large_new if r["side"] == side) for side in ("buy", "sell")}
            side = max(side_totals, key=side_totals.get)
            selected = [row for row in large_new if row["side"] == side]
            event = {"type": "large_trade", "side": side, "usd": side_totals[side],
                     "count": len(selected), "trade_ids": [r["id"] for r in selected]}
        elif summary["acceleration_from_zero"] and any(row.get("side") in summary["acceleration_from_zero"] and now-row["timestamp"] <= 300 for row in new_recent):
            side = summary["acceleration_from_zero"][0]
            current_total = sum(r["usd"] for r in recent5 if r["side"] == side)
            if liquidity_usd and current_total / liquidity_usd >= 0.01:
                event = {"type": "acceleration", "side": side, "usd": current_total,
                         "count": sum(1 for r in recent5 if r["side"] == side),
                         "trade_ids": [r["id"] for r in recent5 if r["side"] == side]}
        elif any(value >= 2 for value in acceleration.values()) and any(row.get("side") in acceleration and now-row["timestamp"] <= 300 for row in new_recent):
            side = max(acceleration, key=acceleration.get)
            current_total = sum(r["usd"] for r in recent5 if r["side"] == side)
            if liquidity_usd and current_total / liquidity_usd >= 0.01:
                event = {"type": "acceleration", "side": side, "usd": current_total,
                         "count": sum(1 for r in recent5 if r["side"] == side),
                         "trade_ids": [r["id"] for r in recent5 if r["side"] == side]}
        elif directional and liquidity_usd and abs(net) / liquidity_usd >= 0.01:
            event = {"type": "net_flow", "side": directional,
                     "usd": abs(net), "count": window15["known_trades"], "trade_ids": [r["id"] for r in new_recent]}
    if event:
        signature = f"{event['type']}:{event['side']}:{','.join(event['trade_ids'][-5:])}"
        previous_signature = state.get("last_event_signature")
        last_alert = _number(state.get("last_alert_at")) or 0
        if signature == previous_signature or now - last_alert < cooldown_seconds:
            event = None
        else:
            state["last_event_signature"] = signature
            state["last_alert_at"] = now
    state.update(last_check=now, last_analysis_at=now, available=summary["available"], recent_trades=history,
                 initialized=True,
                 buy_volume_15m=(window15 or {}).get("buy_usd"), sell_volume_15m=(window15 or {}).get("sell_usd"),
                 net_flow_15m=(window15 or {}).get("net_usd"), large_buys=summary["large_buys"],
                 large_sells=summary["large_sells"], summary=summary)
    return state, event


def intelligence_score(flow_summary: dict[str, Any]) -> float | None:
    if not flow_summary.get("available"):
        return None
    window = flow_summary.get("windows", {}).get("15m")
    if not window or not window.get("known_trades"):
        return None
    gross = window["buy_usd"] + window["sell_usd"]
    return 50 + (50 * window["net_usd"] / gross if gross > 0 else 0)
