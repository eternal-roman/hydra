"""Session opinion ledger for a long 60-minute paper walk.

The opinion at each hour uses only that hour's packet. It confirms an
engine BUY only in TREND_UP with the daily ensemble long and RSI still
under 68. It sells an open long when the engine says SELL, the regime
flips to TREND_DOWN, or the daily ensemble is no longer long. Everything
else is a hold. This is the ledger used to save a broad sample, not a
second live brain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO

CHASE_RSI = 68.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _seat(action: str, confidence: float, reason: str, *, engine: str, rsi: float, regime: str, pair: str) -> dict:
    if action == "BUY":
        scenario = {"p_up": 0.46, "p_flat": 0.32, "p_down": 0.22, "expected_move_bps_3candle": 25}
    elif action == "SELL":
        scenario = {"p_up": 0.24, "p_flat": 0.30, "p_down": 0.46, "expected_move_bps_3candle": -20}
    else:
        scenario = {"p_up": 0.33, "p_flat": 0.40, "p_down": 0.27, "expected_move_bps_3candle": 0}
    agree = action == engine or (action == "HOLD" and engine == "HOLD")
    quant = {
        "scenario": scenario,
        "indicators_used": {
            "funding_bps_8h": None,
            "oi_delta_1h_pct": None,
            "oi_price_regime": None,
            "basis_apr_pct": None,
            "cvd_divergence_sigma": None,
        },
        "positioning_bias": "unknown",
        "signal_agreement": agree,
        "suggested_action": action,
        "size_multiplier": 1.0,
        "force_hold": False,
        "force_hold_reason": "",
        "conviction": round(min(0.85, max(0.55, confidence)), 2),
        "reasoning": reason,
        "key_factors": [regime, f"RSI {rsi:.1f}", f"engine {engine}"],
        "concern": None if agree else "opinion differs from the engine",
        "thesis": reason,
    }
    risk = {
        "decision": "CONFIRM",
        "final_action": action,
        "size_multiplier": 1.0,
        "risk_metrics": {
            "position_exposure_pct": 0.0,
            "correlation_cluster": "BTC_CLUSTER" if pair.startswith("BTC/") else "INDEPENDENT",
            "stress_loss_3pct_pct": 0.0,
            "stress_loss_10pct_pct": 0.0,
            "liquidity_score": "UNKNOWN",
            "fat_tail_concern": False,
        },
        "risk_flags": [],
        "portfolio_health": "HEALTHY",
        "reasoning": reason,
    }
    strategist = {
        "final_action": action,
        "decision": "CONFIRM",
        "reasoning": reason,
    }
    return {
        "action": action,
        "confidence": confidence,
        "size_multiplier": 1.0,
        "reason": reason,
        "quant": quant,
        "risk": risk,
        "strategist": strategist,
    }


def opinion_for_pair(row: dict) -> dict:
    """One pair, one hour. `row` is the walk packet slice."""
    ind = row.get("indicators") or {}
    sig = row.get("signal") or {}
    pos = _f((row.get("position") or {}).get("size"))
    regime = str(row.get("regime") or "")
    daily = row.get("daily_trend_long")
    rsi = _f(ind.get("rsi"))
    price = _f(row.get("price"))
    upper = _f(ind.get("bb_upper"))
    engine = str(sig.get("action") or "HOLD").upper()
    conf = _f(sig.get("confidence"))
    above_upper = upper > 0 and price > upper
    chase = rsi >= CHASE_RSI or above_upper
    engine_reason = str(sig.get("reason") or "")
    pair = str(row.get("asset") or "")

    if pos > 0 and (engine == "SELL" or regime == "TREND_DOWN" or daily is False):
        why = (
            f"Sell the open long. Engine {engine}, regime {regime}, "
            f"daily_long {daily}, RSI {rsi:.1f}."
        )
        return _seat("SELL", max(conf, 0.8), why, engine=engine, rsi=rsi, regime=regime, pair=pair)
    if (
        pos <= 0
        and engine == "BUY"
        and regime == "TREND_UP"
        and daily is True
        and not chase
    ):
        why = (
            f"Confirm the trend buy. RSI {rsi:.1f}, regime TREND_UP, "
            f"daily ensemble long. {engine_reason}"
        )
        return _seat("BUY", conf, why, engine=engine, rsi=rsi, regime=regime, pair=pair)
    if pos <= 0 and engine == "BUY" and chase:
        why = (
            f"Engine BUY not taken. RSI {rsi:.1f}, price {price:.4f}, "
            f"upper band {upper:.4f}."
        )
        return _seat("HOLD", conf, why, engine=engine, rsi=rsi, regime=regime, pair=pair)
    why = (
        f"Hold. Regime {regime}, engine {engine}, RSI {rsi:.1f}, "
        f"daily_long {daily}."
    )
    return _seat("HOLD", conf if conf else 0.5, why, engine=engine, rsi=rsi, regime=regime, pair=pair)


def ledger_opinion(ts: float, view: Dict[str, dict]) -> Dict[str, dict]:
    del ts
    return {pair: opinion_for_pair(row) for pair, row in view.items()}


def _interesting(row: dict, opinion: dict) -> bool:
    regime = str(row.get("regime") or "")
    engine = str((row.get("signal") or {}).get("action") or "HOLD")
    return regime != "RANGING" or engine != "HOLD" or opinion["action"] != "HOLD"


def history_row(ts: float, pair: str, row: dict, opinion: dict, outcome: Optional[dict]) -> dict:
    ind = row.get("indicators") or {}
    sig = row.get("signal") or {}
    stored = {
        "ts": ts,
        "pair": pair,
        "price": row.get("price"),
        "regime": row.get("regime"),
        "engine_action": sig.get("action"),
        "engine_confidence": sig.get("confidence"),
        "engine_reason": sig.get("reason"),
        "daily_trend_long": row.get("daily_trend_long"),
        "rsi": ind.get("rsi"),
        "macd_histogram": ind.get("macd_histogram"),
        "bb_upper": ind.get("bb_upper"),
        "bb_lower": ind.get("bb_lower"),
        "action": opinion["action"],
        "confidence": opinion["confidence"],
        "size_multiplier": opinion["size_multiplier"],
        "reason": opinion["reason"],
        "position_size": (row.get("position") or {}).get("size"),
    }
    if outcome:
        stored["outcome"] = outcome.get("status")
        stored["outcome_why"] = outcome.get("why")
        if outcome.get("status") == "filled":
            stored["fill_ts"] = outcome.get("fill_ts")
            stored["fill_price"] = outcome.get("fill_price")
            stored["fee_usd"] = outcome.get("fee_usd")
    if _interesting(row, opinion):
        stored["quant"] = opinion["quant"]
        stored["risk"] = opinion["risk"]
        stored["strategist"] = opinion["strategist"]
    return stored


def write_history(
    path: Path,
    opinions: Sequence[dict],
    outcomes: Dict[tuple, dict],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    handle: TextIO = path.open("w", encoding="utf-8")
    try:
        for item in opinions:
            ts = float(item["ts"])
            pair = item["pair"]
            row = history_row(ts, pair, item["view"], item["opinion"], outcomes.get((ts, pair)))
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            count += 1
    finally:
        handle.close()
    return count


def summarize(report: dict, opinions: List[dict]) -> dict:
    actions: Dict[str, int] = {}
    regimes: Dict[str, int] = {}
    for item in opinions:
        action = item["opinion"]["action"]
        regime = str(item["view"].get("regime") or "")
        actions[action] = actions.get(action, 0) + 1
        regimes[regime] = regimes.get(regime, 0) + 1
    outcomes: Dict[str, int] = {}
    for intent in report.get("intents") or []:
        status = str(intent.get("status") or "")
        outcomes[status] = outcomes.get(status, 0) + 1
    return {
        "interval_min": 60,
        "decision_source": "session ledger opinion, one hour at a time",
        "score_start": report.get("score_start"),
        "score_end": report.get("score_end"),
        "bars": report.get("bars"),
        "opinion_rows": len(opinions),
        "before_usd": report.get("before_usd"),
        "after_usd": report.get("after_usd"),
        "net_usd": report.get("net_usd"),
        "accumulated_roi": report.get("accumulated_roi"),
        "realized_net_usd": report.get("realized_net_usd"),
        "fees_usd": report.get("fees_usd"),
        "actions": actions,
        "regimes": regimes,
        "outcomes": outcomes,
        "closed_trades": report.get("closed_trades") or [],
        "open_marks": report.get("open_marks") or [],
    }
