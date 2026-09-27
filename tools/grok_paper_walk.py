"""Causal paper walk. The decision maker is an injected Grok opinion.

BacktestRunner is not used. At hour T the opinion sees the engine state
built only from candles at or before T. A post-only limit fills, or is
rejected, against hour T+1. The same-bar close is not a fill. A daily
bar is seeded only after that day has closed. The live futures snapshot
is not copied onto earlier hours.

Hydra's default tape is 60 minutes. The live interval set is 1/5/15/30/60,
so a 4-hour bar is not a second decision tape in this walk.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from hydra_backtest import Candle, PendingOrder, SimulatedFiller
from hydra_brain import HydraBrain
from hydra_engine import HydraEngine, SIZING_COMPETITION

from tools.grok_paper_emulation import (
    MAKER_FEE_RATE,
    equal_variance_allocation,
    install_players,
    sample_variance,
)

Decider = Callable[[float, Dict[str, dict]], Dict[str, dict]]


def _ts(bar: dict) -> float:
    return float(bar["timestamp"])


def _candle(bar: dict) -> Candle:
    return Candle(
        open=float(bar["open"]),
        high=float(bar["high"]),
        low=float(bar["low"]),
        close=float(bar["close"]),
        volume=float(bar.get("volume") or 0.0),
        timestamp=_ts(bar),
    )


def score_window(stamps: Sequence[float], score_hours: int, skip_last_hours: int, interval_s: int = 3600):
    """Last `score_hours` decision hours, leaving `skip_last_hours` off the end.

    The skipped tail can still fill the last order. It is not a decision hour.
    """
    if not stamps:
        raise ValueError("no bars")
    if score_hours < 1 or skip_last_hours < 0:
        raise ValueError("bad score window")
    last_decision = stamps[-1] - skip_last_hours * interval_s
    score_end = last_decision + interval_s
    score_start = score_end - score_hours * interval_s
    return score_start, score_end


def common_timestamps(bars: Dict[str, Sequence[dict]]) -> List[float]:
    sets = [{_ts(bar) for bar in rows} for rows in bars.values()]
    if not sets:
        return []
    shared = set.intersection(*sets) if len(sets) > 1 else sets[0]
    return sorted(shared)


def completed_daily(daily: Sequence[dict], asof: float) -> List[dict]:
    """Daily bars whose close is already known at `asof`.

    Kraken stamps a daily candle with its open. That day's close exists
    one day later.
    """
    out = []
    for bar in daily or []:
        if _ts(bar) + 86400 <= asof and float(bar.get("close") or 0) > 0:
            out.append(bar)
    return out


def new_engine(pair: str, cash: float, hold_through: Optional[bool] = None) -> HydraEngine:
    kwargs: Dict[str, Any] = dict(
        initial_balance=cash,
        asset=pair,
        sizing=dict(SIZING_COMPETITION),
        candle_interval=60,
    )
    if hold_through is not None:
        kwargs["hold_through"] = hold_through
    return HydraEngine(**kwargs)


def _equity(engines: Dict[str, HydraEngine]) -> float:
    total = 0.0
    for eng in engines.values():
        price = eng.prices[-1] if eng.prices else 0.0
        total += eng.balance + eng.position.size * price
    return total


def _packet(eng: HydraEngine, state: dict, n: int, total: int) -> dict:
    candles = state.get("candles") or []
    return {
        "asset": state.get("asset"),
        "history_through": candles[-1]["t"] if candles else None,
        "bar_index": n,
        "bar_count": total,
        "price": state.get("price"),
        "regime": state.get("regime"),
        "strategy": state.get("strategy"),
        "signal": state.get("signal"),
        "indicators": state.get("indicators"),
        "trend": state.get("trend"),
        "volatility": state.get("volatility"),
        "volume": state.get("volume"),
        "position": state.get("position"),
        "portfolio": state.get("portfolio"),
        "halted": state.get("halted"),
        "daily_trend_score": eng.daily_trend_score(),
        "daily_trend_long": eng.daily_trend_long(),
        "prior_closes": [c.get("c") for c in candles[-5:]],
        "prior_times": [c.get("t") for c in candles[-5:]],
        "cvd_divergence_sigma": eng.cvd_divergence_sigma(),
    }


def _close_lots(
    lots: List[dict],
    amount: float,
    price: float,
    sell_fee: float,
    fill_ts: float,
    decision_ts: float,
) -> List[dict]:
    closed: List[dict] = []
    left = float(amount)
    while left > 1e-12 and lots:
        lot = lots[0]
        take = min(left, float(lot["amount"]))
        buy_fee = float(lot["fee"]) * (take / float(lot["amount"]))
        sell_part = float(sell_fee) * (take / float(amount)) if amount else 0.0
        gross = (price - float(lot["price"])) * take
        net = gross - buy_fee - sell_part
        notional = float(lot["price"]) * take
        closed.append({
            "pair": lot["pair"],
            "decision_ts": lot["decision_ts"],
            "fill_ts": lot["fill_ts"],
            "exit_decision_ts": decision_ts,
            "exit_fill_ts": fill_ts,
            "entry_price": lot["price"],
            "exit_price": price,
            "amount": take,
            "entry_notional": notional,
            "buy_fee_usd": buy_fee,
            "sell_fee_usd": sell_part,
            "gross_usd": gross,
            "net_usd": net,
            "roi_on_notional": (net / notional) if notional else 0.0,
        })
        lot["amount"] = float(lot["amount"]) - take
        lot["fee"] = float(lot["fee"]) - buy_fee
        left -= take
        if lot["amount"] <= 1e-8:
            lots.pop(0)
    return closed


def _finish(
    engines, pairs, alloc, before, closed, lots, intents, score_start, score_end, decision_ts,
) -> dict:
    after = _equity(engines)
    open_marks = []
    for pair, open_lots in lots.items():
        price = engines[pair].prices[-1] if engines[pair].prices else 0.0
        for lot in open_lots:
            gross = (price - float(lot["price"])) * float(lot["amount"])
            net = gross - float(lot["fee"])
            notional = float(lot["price"]) * float(lot["amount"])
            open_marks.append({
                "pair": pair,
                "decision_ts": lot["decision_ts"],
                "fill_ts": lot["fill_ts"],
                "entry_price": lot["price"],
                "mark_price": price,
                "amount": lot["amount"],
                "buy_fee_usd": lot["fee"],
                "unrealized_net_usd": net,
                "roi_on_notional": (net / notional) if notional else 0.0,
            })
    return {
        "status": "complete",
        "interval_min": 60,
        "decision_source": "Grok opinion injected at each scored hour",
        "fill_model": "next-bar post-only",
        "before_usd": before,
        "after_usd": after,
        "net_usd": after - before,
        "accumulated_roi": ((after - before) / before) if before else 0.0,
        "realized_net_usd": sum(row["net_usd"] for row in closed),
        "fees_usd": (
            sum(row["buy_fee_usd"] + row["sell_fee_usd"] for row in closed)
            + sum(row["buy_fee_usd"] for row in open_marks)
        ),
        "closed_trades": closed,
        "open_marks": open_marks,
        "intents": intents,
        "weights": {pair: alloc["weights"][i] for i, pair in enumerate(pairs)},
        "cash": {pair: alloc["cash"][i] for i, pair in enumerate(pairs)},
        "score_start": score_start,
        "score_end": score_end,
        "bars": len(decision_ts),
    }


def walk_book(
    bars: Dict[str, Sequence[dict]],
    daily: Dict[str, Sequence[dict]],
    decisions: Optional[Dict[str, Dict[str, dict]]],
    nav: float,
    score_start: float,
    score_end: float,
    decider: Optional[Decider] = None,
    fill_model: str = "realistic",
    hold_through: Optional[bool] = None,
    brain: Optional[HydraBrain] = None,
    on_opinion: Optional[Callable[[float, Dict[str, dict], Dict[str, dict]], None]] = None,
) -> dict:
    """Walk scored hours. Missing Grok seats stop the walk before that order.

    `decisions` maps str(int(hour)) to pair -> {quant, risk, strategist}.
    `decider` is the test seam and returns pair -> action fields. It is
    called with the packet for that hour only.
    """
    pairs = list(bars)
    stamps = common_timestamps(bars)
    by_ts = {pair: {_ts(bar): bar for bar in rows} for pair, rows in bars.items()}
    decision_ts = [t for t in stamps if score_start <= t < score_end]
    if not decision_ts:
        raise ValueError("score window has no bars")

    train = {pair: [by_ts[pair][t] for t in stamps if t < score_start] for pair in pairs}
    sigmas = []
    for pair in pairs:
        closes = [float(bar["close"]) for bar in train[pair]]
        _var, sigma, _n = sample_variance(closes)
        sigmas.append(sigma)
    alloc = equal_variance_allocation(sigmas, nav)
    engines = {
        pair: new_engine(pair, alloc["cash"][i], hold_through=hold_through)
        for i, pair in enumerate(pairs)
    }
    for pair in pairs:
        engines[pair].seed_daily_closes(completed_daily(daily.get(pair) or [], score_start))
        for bar in train[pair]:
            engines[pair].ingest_candle(bar)

    before = _equity(engines)
    filler = SimulatedFiller(fill_model, MAKER_FEE_RATE * 10_000.0)
    pending: Dict[str, Optional[dict]] = {p: None for p in pairs}
    lots: Dict[str, List[dict]] = {p: [] for p in pairs}
    closed: List[dict] = []
    intents: List[dict] = []
    seat_table: Dict[str, dict] = {}
    if decider is None:
        if brain is None:
            os.environ.setdefault("HYDRA_BRAIN_JSONL", "grok_paper_walk_brain.jsonl")
            brain = HydraBrain(xai_key="emulated-no-http")
            if brain.primary_model != "grok-4.7":
                raise RuntimeError(f"expected grok-4.7, got {brain.primary_model}")
        install_players(brain, seat_table)

    index = {t: i for i, t in enumerate(stamps)}

    def resolve(bar_ts: float) -> None:
        for pair in pairs:
            order = pending[pair]
            if order is None:
                continue
            eng = engines[pair]
            fill = filler.try_fill(order["order"], _candle(by_ts[pair][bar_ts]))
            snap = order["snapshot"]
            if not fill.filled:
                eng.restore_position(snap)
                intents.append({
                    "pair": pair, "decision_ts": order["decision_ts"],
                    "action": order["action"], "status": "rejected", "why": fill.reason,
                })
                pending[pair] = None
                continue
            applied = eng.true_up_fill(
                side=order["action"], amount=order["amount"],
                fill_price=fill.fill_price, pre_trade_snapshot=snap,
                reason="next_bar_post_only", strategy=order["strategy"],
                confidence=order["confidence"],
            )
            if not applied:
                eng.restore_position(snap)
                intents.append({
                    "pair": pair, "decision_ts": order["decision_ts"],
                    "action": order["action"], "status": "rejected", "why": "true_up_skipped",
                })
                pending[pair] = None
                continue
            eng.balance = max(0.0, eng.balance - float(fill.fee_paid))
            if order["action"] == "BUY":
                lots[pair].append({
                    "pair": pair, "price": fill.fill_price, "amount": order["amount"],
                    "fee": float(fill.fee_paid), "decision_ts": order["decision_ts"],
                    "fill_ts": bar_ts,
                })
            else:
                closed.extend(_close_lots(
                    lots[pair], order["amount"], fill.fill_price,
                    float(fill.fee_paid), bar_ts, order["decision_ts"],
                ))
            intents.append({
                "pair": pair, "decision_ts": order["decision_ts"], "fill_ts": bar_ts,
                "action": order["action"], "status": "filled",
                "fill_price": fill.fill_price, "amount": order["amount"],
                "fee_usd": float(fill.fee_paid),
            })
            pending[pair] = None

    def opinions(ts: float, raw: Dict[str, dict], view: Dict[str, dict]) -> Optional[Dict[str, dict]]:
        if decider is not None:
            return decider(ts, view)
        key = str(int(ts))
        seats = (decisions or {}).get(key)
        if not isinstance(seats, dict) or any(pair not in seats for pair in pairs):
            return None
        out = {}
        for pair in pairs:
            seat_table.clear()
            seat_table[pair] = seats[pair]
            decision = brain.deliberate(raw[pair])
            out[pair] = {
                "action": decision.final_signal,
                "confidence": float(decision.confidence_adj or 0.0),
                "size_multiplier": float(decision.size_multiplier or 0.0),
                "reason": decision.combined_summary,
                "escalated": bool(decision.escalated),
                "fallback": bool(decision.fallback),
            }
        return out

    for n, ts in enumerate(decision_ts, start=1):
        resolve(ts)
        raw: Dict[str, dict] = {}
        view: Dict[str, dict] = {}
        for pair in pairs:
            eng = engines[pair]
            eng.ingest_candle(by_ts[pair][ts])
            state = eng.tick(generate_only=True)
            raw[pair] = state
            view[pair] = _packet(eng, state, n, len(decision_ts))
        chosen = opinions(ts, raw, view)
        if chosen is not None and on_opinion is not None:
            on_opinion(ts, view, chosen)
        if chosen is None:
            return {
                "status": "need_decision",
                "timestamp": ts,
                "bar_index": n,
                "bar_count": len(decision_ts),
                "pairs": view,
                "equity_before_usd": before,
                "weights": {pair: alloc["weights"][i] for i, pair in enumerate(pairs)},
                "cash": {pair: alloc["cash"][i] for i, pair in enumerate(pairs)},
            }
        for pair in pairs:
            order = chosen.get(pair) or {}
            action = str(order.get("action") or "HOLD").upper()
            eng = engines[pair]
            if action not in ("BUY", "SELL") or order.get("fallback"):
                intents.append({
                    "pair": pair, "decision_ts": ts, "action": action,
                    "status": "no_order", "why": order.get("reason") or action,
                })
                continue
            snap = eng.snapshot_position()
            eng._last_friction_skip = None
            trade = eng.execute_signal(
                action,
                float(order.get("confidence") or 0.0),
                reason=str(order.get("reason") or ""),
                strategy=str(view[pair].get("strategy") or "MOMENTUM"),
                size_multiplier=float(order.get("size_multiplier") if order.get("size_multiplier") is not None else 1.0),
                decision_cost_usd=0.0,
            )
            if trade is None:
                skip = getattr(eng, "_last_friction_skip", None) or {}
                if skip:
                    why = (
                        f"friction expected {skip.get('expected_move_pct')}% "
                        f"< hurdle {skip.get('hurdle_pct')}%"
                    )
                else:
                    why = "execute_signal returned None (rails, confidence, or size)"
                intents.append({
                    "pair": pair, "decision_ts": ts, "action": action,
                    "status": "blocked", "why": why,
                })
                continue
            if index[ts] + 1 >= len(stamps):
                eng.restore_position(snap)
                intents.append({
                    "pair": pair, "decision_ts": ts, "action": action,
                    "status": "expired", "why": "no next bar",
                })
                continue
            pending[pair] = {
                "order": PendingOrder(
                    pair=pair, side=trade.action, limit_price=trade.price,
                    size=trade.amount, placed_tick=n, pre_trade_snapshot=snap,
                ),
                "snapshot": snap,
                "decision_ts": ts,
                "action": trade.action,
                "amount": trade.amount,
                "strategy": str(view[pair].get("strategy") or "MOMENTUM"),
                "confidence": float(order.get("confidence") or 0.0),
            }

    last = decision_ts[-1]
    nxt = index[last] + 1
    if nxt < len(stamps):
        resolve(stamps[nxt])
    for pair in pairs:
        order = pending[pair]
        if order is None:
            continue
        engines[pair].restore_position(order["snapshot"])
        intents.append({
            "pair": pair, "decision_ts": order["decision_ts"],
            "action": order["action"], "status": "expired", "why": "no next bar",
        })
        pending[pair] = None
    return _finish(
        engines, pairs, alloc, before, closed, lots, intents,
        score_start, score_end, decision_ts,
    )
