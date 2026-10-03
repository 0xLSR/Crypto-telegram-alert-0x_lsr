"""Read-only, history-based market context. Scores describe confluence, not odds."""
from __future__ import annotations

import math
import statistics
import time
from typing import Any

HORIZONS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
MAX_SNAPSHOTS_PER_TOKEN = 2200
MIN_LIQUIDITY_USD = 5_000


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def make_snapshot(pair: dict[str, Any], timestamp: float) -> dict[str, Any] | None:
    price = number(pair.get("priceUsd"))
    if price is None or price <= 0:
        return None
    return {
        "timestamp": float(timestamp), "price": price,
        "volume_24h": number((pair.get("volume") or {}).get("h24")),
        "market_cap": number(pair.get("marketCap")), "fdv": number(pair.get("fdv")),
        "liquidity": number((pair.get("liquidity") or {}).get("usd")),
        "price_change_24h": number((pair.get("priceChange") or {}).get("h24")),
    }


def append_snapshot(history: dict[str, list[dict[str, Any]]], address: str, pair: dict[str, Any],
                    timestamp: float, retention_hours: float = 24,
                    interval_seconds: float = 60) -> dict[str, Any] | None:
    snapshot = make_snapshot(pair, timestamp)
    if snapshot is None:
        return None
    rows = history.setdefault(address, [])
    if rows and timestamp - float(rows[-1].get("timestamp", 0)) < interval_seconds:
        return None
    rows.append(snapshot)
    cutoff = timestamp - max(1, retention_hours) * 3600
    history[address] = [row for row in rows[-MAX_SNAPSHOTS_PER_TOKEN:]
                        if number(row.get("timestamp")) is not None and row["timestamp"] >= cutoff]
    return snapshot


def _return_at(rows: list[dict[str, Any]], seconds: int) -> float | None:
    if len(rows) < 2:
        return None
    latest = rows[-1]
    target = float(latest["timestamp"]) - seconds
    candidates = [row for row in rows[:-1] if float(row["timestamp"]) <= target]
    if not candidates:
        return None
    base = candidates[-1]
    elapsed = float(latest["timestamp"]) - float(base["timestamp"])
    if elapsed > seconds * 1.8:
        return None
    old_price = number(base.get("price"))
    price = number(latest.get("price"))
    if not old_price or not price or old_price <= 0:
        return None
    return (price / old_price - 1) * 100


def _score(value: float, low: float, high: float) -> float:
    return max(0.0, min(100.0, (value - low) * 100 / (high - low)))


def analyze(snapshots: list[dict[str, Any]], pair: dict[str, Any] | None = None,
            timestamp: float | None = None, flow: dict[str, Any] | None = None,
            whale_flow_weight: float = 10) -> dict[str, Any]:
    """Combine available market and locally sampled history without filling gaps."""
    rows = sorted((row for row in snapshots if number(row.get("price")) and number(row.get("timestamp"))),
                  key=lambda row: float(row["timestamp"]))
    current = make_snapshot(pair, timestamp if timestamp is not None else time.time()) if pair else None
    changed = bool(current and rows and any(current.get(key) != rows[-1].get(key) for key in current if key != "timestamp"))
    if current and (not rows or (current["timestamp"] > rows[-1]["timestamp"] and changed)):
        rows_for_calc = rows + [current]
    else:
        rows_for_calc = rows
    if not rows_for_calc and not pair:
        return {"score": None, "entry_score": None, "exit_risk": None, "confidence": 0,
                "state": "⚪ SEM DADOS", "metrics": {}, "components": {}, "entry": {}, "reasons": ["Dados de mercado indisponíveis."],
                "insufficient": True}

    latest = rows_for_calc[-1] if rows_for_calc else current or {}
    metrics: dict[str, Any] = {f"return_{name}": _return_at(rows_for_calc, seconds) for name, seconds in HORIZONS.items()}
    if metrics["return_24h"] is None:
        metrics["return_24h"] = number(latest.get("price_change_24h"))
    price = number(latest.get("price"))
    volume = number(latest.get("volume_24h"))
    market_cap = number(latest.get("market_cap"))
    liquidity = number(latest.get("liquidity"))
    metrics.update(price=price, volume_24h=volume, market_cap=market_cap, fdv=number(latest.get("fdv")),
                   liquidity=liquidity, volume_to_market_cap=(volume / market_cap if volume is not None and market_cap and market_cap > 0 else None))
    span = float(rows_for_calc[-1]["timestamp"]) - float(rows_for_calc[0]["timestamp"]) if len(rows_for_calc) > 1 else 0
    metrics["history_points"] = len(rows_for_calc)
    metrics["history_span_seconds"] = span
    r5, r15, r1h, r4h = (metrics.get("return_" + key) for key in ("5m", "15m", "1h", "4h"))
    metrics["price_acceleration"] = r5 - (r15 / 3) if r5 is not None and r15 is not None else None
    if len(rows_for_calc) >= 2:
        prior_volumes = [number(row.get("volume_24h")) for row in rows_for_calc[:-1]]
        prior_volumes = [item for item in prior_volumes if item is not None and item > 0]
        metrics["volume_acceleration"] = (volume / statistics.mean(prior_volumes[-5:]) - 1) * 100 if volume and prior_volumes else None
    else:
        metrics["volume_acceleration"] = None

    prices = [number(row.get("price")) for row in rows_for_calc[-30:]]
    prices = [item for item in prices if item is not None and item > 0]
    metrics["recent_high"] = max(prices[:-1]) if len(prices) >= 2 else None
    metrics["recent_low"] = min(prices[:-1]) if len(prices) >= 2 else None
    high, low = metrics["recent_high"], metrics["recent_low"]
    metrics["distance_from_high_pct"] = (price / high - 1) * 100 if price and high else None
    metrics["distance_from_low_pct"] = (price / low - 1) * 100 if price and low else None
    prior_prices = prices[:-1]
    metrics["breakout"] = bool(price and prior_prices and price > max(prior_prices))
    metrics["false_breakout_risk"] = bool(metrics["breakout"] and metrics["volume_acceleration"] is not None
                                          and metrics["volume_acceleration"] <= 0)
    metrics["support_lost"] = bool(price and prior_prices and price < min(prior_prices))
    metrics["drawdown_pct"] = (price / max(prices) - 1) * 100 if price and prices else None
    sma = statistics.mean(prices[:-1]) if len(prices) >= 2 else None
    metrics["sma_recent"] = sma
    metrics["distance_from_sma_pct"] = (price / sma - 1) * 100 if price and sma else None
    returns = [(b / a - 1) * 100 for a, b in zip(prices, prices[1:]) if a > 0]
    metrics["volatility_pct"] = statistics.pstdev(returns) if len(returns) >= 3 else None
    metrics["consistency_pct"] = (sum(value > 0 for value in returns[-15:]) / min(15, len(returns)) * 100
                                  if len(returns) >= 5 else None)
    metrics["rejection_high"] = bool(metrics["distance_from_high_pct"] is not None
                                     and metrics["distance_from_high_pct"] <= -3)

    components: dict[str, float] = {}
    momentum_returns = [v for v in (r5, r15, metrics.get("return_30m"), r1h) if v is not None]
    if momentum_returns:
        components["momentum"] = _score(statistics.mean(momentum_returns), -20, 30)
    if volume is not None and volume > 0:
        components["volume"] = _score(volume, 0, max(1, (market_cap or volume * 20) * .25))
    if metrics["volume_acceleration"] is not None:
        components["volume_acceleration"] = _score(metrics["volume_acceleration"], -50, 100)
    if metrics["price_acceleration"] is not None:
        components["price_acceleration"] = _score(metrics["price_acceleration"], -10, 10)
    if high is not None and low is not None and high > low:
        components["structure"] = 85 if metrics["breakout"] else 65 if price > (high + low) / 2 else 35
    elif metrics.get("return_24h") is not None:
        components["structure"] = _score(metrics["return_24h"], -50, 50)
    if liquidity is not None:
        components["liquidity"] = _score(liquidity, 0, 100_000)
    if metrics["volatility_pct"] is not None:
        components["volatility"] = max(0, 100 - _score(metrics["volatility_pct"], 5, 50))
    if metrics["volume_to_market_cap"] is not None:
        components["turnover"] = _score(metrics["volume_to_market_cap"], 0, 1)
    if market_cap is not None and market_cap > 0:
        components["market_cap"] = _score(math.log10(market_cap), 3, 9)
    fdv = metrics.get("fdv")
    if fdv is not None and market_cap is not None and market_cap > 0:
        components["fdv_overhang"] = max(0, 100 - _score(fdv / market_cap, 1, 10))
    if metrics["consistency_pct"] is not None:
        components["consistency"] = metrics["consistency_pct"]
    from market_flow import intelligence_score
    flow_score = intelligence_score(flow or {})
    if flow_score is not None:
        components["whale_flow"] = flow_score
        flow_window = (flow or {}).get("windows", {}).get("15m", {})
        metrics["flow_buy_usd_15m"] = flow_window.get("buy_usd")
        metrics["flow_sell_usd_15m"] = flow_window.get("sell_usd")
        metrics["flow_net_usd_15m"] = flow_window.get("net_usd")
        metrics["flow_divergence"] = (flow or {}).get("divergence")
    weights = {"momentum": 22, "volume": 8, "volume_acceleration": 8, "price_acceleration": 8,
               "structure": 18, "liquidity": 14, "volatility": 5, "turnover": 5,
               "market_cap": 5, "fdv_overhang": 3, "consistency": 4}
    if flow_score is not None:
        try:
            configured_flow_weight = max(0.0, min(20.0, float(whale_flow_weight)))
        except (TypeError, ValueError, OverflowError):
            configured_flow_weight = 10.0
        remainder = 100.0 - configured_flow_weight
        original_total = sum(weights.values())
        weights = {key: value * remainder / original_total for key, value in weights.items()}
        weights["whale_flow"] = configured_flow_weight
    score_components = {key: value for key, value in components.items() if weights.get(key, 0) > 0}
    if score_components:
        score = round(sum(score_components[key] * weights[key] for key in score_components) / sum(weights[key] for key in score_components))
        if liquidity is not None and liquidity < MIN_LIQUIDITY_USD:
            score = min(score, 35)
    else:
        score = None

    enough_history = len(rows_for_calc) >= 5 and span >= 4 * 60
    entry_score = score
    late = bool((r1h is not None and r1h >= 50) or (r4h is not None and r4h >= 100) or
                (metrics["distance_from_sma_pct"] is not None and metrics["distance_from_sma_pct"] >= 35))
    if entry_score is not None and late:
        entry_score = max(0, entry_score - 30)
    exit_risk = 0
    exit_signals = []
    if r15 is not None and r15 < -5:
        exit_risk += 20; exit_signals.append("Momentum de curto prazo negativo")
    if metrics["support_lost"]:
        exit_risk += 40; exit_signals.append("Suporte recente perdido")
    if metrics["rejection_high"]:
        exit_risk += 15; exit_signals.append("Recuo relevante após máxima recente")
    if metrics["drawdown_pct"] is not None and metrics["drawdown_pct"] < -15:
        exit_risk += min(30, int(abs(metrics["drawdown_pct"]) / 2)); exit_signals.append("Drawdown relevante")
    if r15 is not None and r15 < 0 and metrics["volume_acceleration"] is not None and metrics["volume_acceleration"] > 20:
        exit_risk += 20; exit_signals.append("Queda com aceleração de volume")
    if flow_score is not None and r15 is not None and r15 < 0 and (metrics.get("flow_net_usd_15m") or 0) < 0:
        exit_risk += 10; exit_signals.append("Queda com fluxo vendedor observado")
    if liquidity is not None and liquidity < MIN_LIQUIDITY_USD:
        exit_risk += 20; exit_signals.append("Liquidez muito baixa")
    exit_risk = min(100, exit_risk)
    if not enough_history and (liquidity is None or liquidity >= MIN_LIQUIDITY_USD):
        exit_risk = None
        exit_signals = []

    if liquidity is not None and liquidity < MIN_LIQUIDITY_USD:
        state = "☠️ ALTO RISCO"
    elif (exit_risk or 0) >= 70:
        state = "🔴 DISTRIBUIÇÃO"
    elif metrics["breakout"] and enough_history and (metrics["volume_acceleration"] or 0) > 0:
        state = "🔥 BREAKOUT"
    elif metrics["false_breakout_risk"]:
        state = "🟡 ATENÇÃO"
    elif late:
        state = "🟠 CORREÇÃO" if metrics["drawdown_pct"] and metrics["drawdown_pct"] < -5 else "🟡 ATENÇÃO"
    elif metrics["drawdown_pct"] is not None and -30 < metrics["drawdown_pct"] < -5:
        state = "🟠 CORREÇÃO"
    elif score is not None and score >= 65 and (r15 or 0) > 0:
        state = "🟢 MOMENTUM"
    elif score is None:
        state = "⚪ SEM DADOS"
    elif score >= 45:
        state = "🟡 ATENÇÃO"
    else:
        state = "🔵 NEUTRO"

    entry: dict[str, Any] = {"type": "Dados insuficientes", "late": late, "confirmation": None,
                             "invalidation": low, "tp1": None, "tp2": None, "risk_reward": None}
    if enough_history and score is not None and liquidity is not None and liquidity >= MIN_LIQUIDITY_USD:
        if metrics["breakout"] and (metrics["volume_acceleration"] or 0) > 0:
            flow_net = metrics.get("flow_net_usd_15m")
            if flow_score is not None and flow_net is not None and flow_net > 0:
                entry["type"] = "🔥 BREAKOUT + FLUXO COMPRADOR"
                metrics["flow_breakout_status"] = "possible_confirmation"
            elif flow_score is not None:
                entry["type"] = "⚠️ POSSÍVEL FALSO BREAKOUT · FLUXO NÃO CONFIRMA"
                metrics["flow_breakout_status"] = "possible_false_breakout"
            else:
                entry["type"] = "🔥 BREAKOUT + VOLUME · FLUXO INDISPONÍVEL"
            entry["confirmation"] = price
        elif metrics["drawdown_pct"] is not None and -25 <= metrics["drawdown_pct"] <= -3 and not metrics["support_lost"]:
            entry["type"] = ("🟢 PULLBACK / RETOMADA POTENCIAL"
                              if (r5 or 0) > 0 and (metrics["volume_acceleration"] or 0) > 0
                              else "🟠 PULLBACK EM OBSERVAÇÃO")
        elif score >= 65 and not late and (r15 or 0) > 0 and (metrics["volume_acceleration"] or 0) > 0:
            entry["type"] = "🟢 ENTRADA POTENCIAL"
            entry["confirmation"] = high
        elif score >= 65 and not late:
            entry["type"] = "🟡 AGUARDAR CONFIRMAÇÃO"
        if late:
            entry["type"] = "🔴 ENTRADA TARDIA"
        if low and price and low < price:
            entry["zone"] = (low, price)
            if high and high > price:
                entry["tp1"] = high
                risk = price - low
                entry["risk_reward"] = (high - price) / risk if risk > 0 else None

    available_ratio = len(components) / len(weights)
    history_factor = min(1.0, len(rows_for_calc) / 30, span / (4 * 3600)) if rows_for_calc else 0
    confidence = round(100 * (0.45 * available_ratio + 0.55 * history_factor))
    if not enough_history:
        confidence = min(confidence, 35)
    reasons = []
    if metrics["breakout"]: reasons.append("Preço acima da máxima da amostra recente")
    if metrics["false_breakout_risk"]: reasons.append("Rompimento sem confirmação de aceleração do volume")
    if metrics["support_lost"]: reasons.append("Preço abaixo da mínima da amostra recente")
    if metrics["volume_acceleration"] is not None: reasons.append("Aceleração de volume observável")
    if late: reasons.append("Movimento estendido; risco de entrada tardia")
    if liquidity is not None and liquidity < MIN_LIQUIDITY_USD: reasons.append("Liquidez abaixo do piso conservador do modelo")
    if not enough_history: reasons.append("⏳ Dados insuficientes para análise completa.")
    return {"score": score, "entry_score": entry_score, "exit_risk": exit_risk, "confidence": confidence,
            "state": state, "metrics": metrics, "components": components, "entry": entry,
            "exit_signals": exit_signals, "reasons": reasons, "insufficient": not enough_history}


def record_signal(signals: list[dict[str, Any]], address: str, result: dict[str, Any], snapshot: dict[str, Any]) -> None:
    key = (result.get("state"), result.get("entry", {}).get("type"), (result.get("exit_risk") or 0) >= 70)
    previous = next((item for item in reversed(signals) if item.get("address") == address), None)
    prev_key = (previous.get("state"), previous.get("entry_type"), bool((previous.get("exit_risk") or 0) >= 70)) if previous else None
    if key == prev_key:
        return
    signals.append({"address": address, "timestamp": snapshot["timestamp"], "price": snapshot["price"],
                    "score": result.get("score"), "entry_score": result.get("entry_score"),
                    "exit_risk": result.get("exit_risk"), "state": result.get("state"),
                    "entry_type": result.get("entry", {}).get("type"), "outcomes": {},
                    "max_after": snapshot["price"], "min_after": snapshot["price"]})
    del signals[:-500]


def update_signal_outcomes(signals: list[dict[str, Any]], address: str, snapshot: dict[str, Any]) -> None:
    horizons = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    for signal in signals:
        if signal.get("address") != address:
            continue
        signal_price = number(signal.get("price"))
        if not signal_price or signal_price <= 0:
            continue
        signal["max_after"] = max(number(signal.get("max_after")) or signal_price, snapshot["price"])
        signal["min_after"] = min(number(signal.get("min_after")) or signal_price, snapshot["price"])
        age = snapshot["timestamp"] - signal.get("timestamp", snapshot["timestamp"])
        outcomes = signal.setdefault("outcomes", {})
        for label, seconds in horizons.items():
            if age >= seconds and label not in outcomes:
                outcomes[label] = round((snapshot["price"] / signal_price - 1) * 100, 4)
