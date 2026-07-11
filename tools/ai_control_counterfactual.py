#!/usr/bin/env python3
"""Causal counterfactual: baseline rails vs 'more AI control' policies.

NO FORWARD-LOOKING BIAS
-----------------------
- Decisions use only candles/indicators known at tick t (engine state after
  ingesting candle t).
- Fills use ONLY candle t+1 OHLC (post-only next-bar model) — never t+2+.
- Buy-and-hold benchmark uses the same window endpoints only for reporting.
- Policies are pure functions of state at t; no future regime or return labels
  are used to choose actions.

What this answers
-----------------
Not "would Claude/Grok have said X" (that requires online LLM calls).
It answers the *testable* core of "if AI were given more control / less railing":

  Q1. Does *relaxing rails* (lower conf bar, disable friction) improve causal P&L?
  Q2. Does an *AI-shaped discretionary proxy* (regime filters, early flatten,
      skip ranging entries) improve causal P&L vs engine baseline?
  Q3. Where did rails block trades that would have helped or hurt?

Usage:
  python tools/ai_control_counterfactual.py
  python tools/ai_control_counterfactual.py --days 365 --pair SOL/USD
  python tools/ai_control_counterfactual.py --days 90 --pair BTC/USD
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine, SIZING_COMPETITION  # noqa: E402


# ── fill model (next bar only — no lookahead past t+1) ───────────────────

def try_fill_next_bar(
    side: str,
    limit_price: float,
    size: float,
    next_o: float,
    next_h: float,
    next_l: float,
    next_c: float,
    model: str = "realistic",
    fee_bps: float = 16.0,
) -> Tuple[bool, float, float]:
    """Return (filled, fill_price, fee_quote). Uses only next bar OHLC."""
    if size <= 0 or limit_price <= 0:
        return False, 0.0, 0.0
    filled = False
    if side == "BUY":
        # Post-only bid: need trade at/below limit
        if model == "optimistic":
            filled = next_l <= limit_price
        elif model == "pessimistic":
            filled = next_c <= limit_price
        else:  # realistic: body spends material time through limit
            filled = next_l <= limit_price and (
                next_c <= limit_price or (next_o + next_c) / 2.0 <= limit_price
            )
        fill_px = limit_price if filled else 0.0
    else:  # SELL
        if model == "optimistic":
            filled = next_h >= limit_price
        elif model == "pessimistic":
            filled = next_c >= limit_price
        else:
            filled = next_h >= limit_price and (
                next_c >= limit_price or (next_o + next_c) / 2.0 >= limit_price
            )
        fill_px = limit_price if filled else 0.0
    if not filled:
        return False, 0.0, 0.0
    fee = abs(size * fill_px) * (fee_bps / 10_000.0)
    return True, fill_px, fee


# ── policies ─────────────────────────────────────────────────────────────

@dataclass
class PolicyDecision:
    action: str  # BUY | SELL | HOLD
    confidence: float
    size_mult: float
    reason: str
    rail_notes: List[str] = field(default_factory=list)


def _engine_action(state: Dict[str, Any]) -> Tuple[str, float, str, str]:
    sig = state.get("signal") or {}
    action = str(sig.get("action", "HOLD")).upper()
    conf = float(sig.get("confidence") or 0.0)
    reason = str(sig.get("reason") or "")
    strategy = str(state.get("strategy") or "")
    return action, conf, reason, strategy


def policy_baseline(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """Current engine: execute signal as-is; rails applied at execution."""
    action, conf, reason, strategy = _engine_action(state)
    notes = []
    min_c = 0.65
    if action == "BUY" and conf < min_c:
        notes.append(f"rail:min_conf BUY {conf:.3f}<{min_c}")
    if action == "BUY":
        notes.append("rail:friction_gate_may_apply")
    return PolicyDecision(action, conf, 1.0, f"baseline|{strategy}|{reason}", notes)


def policy_loose_conf(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """More control: lower entry bar to 0.50 (AI takes weaker signals)."""
    action, conf, reason, strategy = _engine_action(state)
    notes = ["control:min_conf=0.50"]
    # Execution will use min_conf 0.50 via engine sizer override
    return PolicyDecision(action, conf, 1.0, f"loose_conf|{strategy}|{reason}", notes)


def policy_no_friction(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """More control: disable friction expectancy gate."""
    action, conf, reason, strategy = _engine_action(state)
    return PolicyDecision(
        action, conf, 1.0, f"no_friction|{strategy}|{reason}",
        ["control:friction_disabled"],
    )


def policy_max_loose(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """Max less-railing: conf 0.50 + no friction + 1.25x size on high conf."""
    action, conf, reason, strategy = _engine_action(state)
    mult = 1.25 if conf >= 0.75 else 1.0
    return PolicyDecision(
        action, conf, mult, f"max_loose|{strategy}|{reason}",
        ["control:min_conf=0.50", "control:friction_disabled", f"control:size_mult={mult}"],
    )


def policy_ai_proxy_selective(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """Execute post-filter engine signal (rails applied in HydraEngine.tick)."""
    action, conf, reason, strategy = _engine_action(state)
    return PolicyDecision(
        action, conf, 1.0, f"engine_sel|{strategy}|{reason}",
        ["control:engine_regime_selective"],
    )


def policy_ai_proxy_aggressive(state: Dict[str, Any], pos_size: float, **_kw) -> PolicyDecision:
    """AI with maximum trade freedom: take almost every engine non-HOLD at conf>=0.50."""
    action, conf, reason, strategy = _engine_action(state)
    notes = ["control:ai_proxy_aggressive", "control:min_conf=0.50", "control:friction_disabled"]
    if action in ("BUY", "SELL") and conf >= 0.50:
        mult = 1.5 if conf >= 0.80 else 1.0
        return PolicyDecision(action, conf, mult, f"ai_agg|{strategy}|{reason}", notes)
    if action in ("BUY", "SELL") and conf < 0.50:
        notes.append("still_blocked_conf<0.50")
        return PolicyDecision("HOLD", conf, 1.0, f"ai_agg|too_weak|{reason}", notes)
    return PolicyDecision("HOLD", conf, 1.0, "ai_agg|hold", notes)


POLICIES = {
    "baseline": policy_baseline,
    "loose_conf": policy_loose_conf,
    "no_friction": policy_no_friction,
    "max_loose": policy_max_loose,
    "ai_proxy_selective": policy_ai_proxy_selective,
    "ai_proxy_aggressive": policy_ai_proxy_aggressive,
}


# ── single-policy causal runner ──────────────────────────────────────────

@dataclass
class Pending:
    side: str
    limit_price: float
    size: float
    conf: float
    reason: str
    tick: int


def run_policy(
    candles: List[Dict[str, float]],
    policy_name: str,
    policy_fn,
    *,
    initial: float = 100.0,
    fill_model: str = "realistic",
    fee_bps: float = 16.0,
    mode: str = "competition",
    sample_every: int = 1,
    regime_selective: Optional[bool] = None,
) -> Dict[str, Any]:
    """Causal replay for one policy. Engines are independent per policy."""
    # Env for friction
    prev_fric = os.environ.get("HYDRA_FRICTION_GATE_DISABLED")
    if policy_name in ("no_friction", "max_loose", "ai_proxy_aggressive"):
        os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"
    else:
        os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)

    sizing = dict(
        SIZING_COMPETITION if mode == "competition"
        else {"kelly_multiplier": 0.25, "min_confidence": 0.65, "max_position_pct": 0.30}
    )
    # Research-only conf floors (not live defaults). Selective uses 0.55 to
    # match the causal study; live competition keeps 0.65 when flag is on.
    if policy_name in ("loose_conf", "max_loose", "ai_proxy_aggressive"):
        sizing["min_confidence"] = 0.50
    elif policy_name == "ai_proxy_selective":
        sizing["min_confidence"] = 0.55

    if regime_selective is None:
        regime_selective = policy_name == "ai_proxy_selective"

    eng = HydraEngine(
        initial_balance=initial,
        asset="PAIR",
        sizing=sizing,
        candle_interval=60,  # 1h bars in this experiment
        regime_selective=bool(regime_selective),
    )

    pending: Optional[Pending] = None
    equity_curve: List[float] = []
    decision_log: List[Dict[str, Any]] = []
    trade_log: List[Dict[str, Any]] = []
    fills = 0
    rejects = 0
    blocked_by_exec = 0
    decisions_buy = 0
    decisions_sell = 0

    n = len(candles)
    for t in range(n):
        c = candles[t]
        # 1) Resolve pending against *this* candle (placed at t-1 → fill on t)
        if pending is not None:
            ok, fpx, fee = try_fill_next_bar(
                pending.side, pending.limit_price, pending.size,
                c["open"], c["high"], c["low"], c["close"],
                model=fill_model, fee_bps=fee_bps,
            )
            if ok:
                # Apply fill true-up style: fees from balance
                eng.balance -= fee
                fills += 1
                trade_log.append({
                    "tick": t,
                    "event": "FILL",
                    "side": pending.side,
                    "price": fpx,
                    "size": pending.size,
                    "fee": fee,
                    "reason": pending.reason,
                    "confidence": pending.conf,
                })
            else:
                # Miss → rollback optimistic engine state if still mismatched.
                # execute_signal already mutated; on miss restore via last snap.
                if hasattr(eng, "_cf_snap") and eng._cf_snap is not None:
                    eng.restore_position(eng._cf_snap)
                    eng._cf_snap = None
                rejects += 1
                trade_log.append({
                    "tick": t,
                    "event": "REJECT",
                    "side": pending.side,
                    "price": pending.limit_price,
                    "size": pending.size,
                    "reason": pending.reason,
                })
            pending = None

        # 2) Ingest candle t and generate signal (only past+current)
        eng.ingest_candle({
            "open": c["open"], "high": c["high"], "low": c["low"],
            "close": c["close"], "volume": c.get("volume", 0.0),
            "timestamp": float(c["timestamp"]),
        })
        state = eng.tick(generate_only=True)
        pos = float(eng.position.size)
        dec = policy_fn(state, pos_size=pos)

        if dec.action == "BUY":
            decisions_buy += 1
        elif dec.action == "SELL":
            decisions_sell += 1

        # Sample decision log (all actionable + periodic HOLDs)
        if dec.action != "HOLD" or (t % max(1, sample_every) == 0):
            decision_log.append({
                "tick": t,
                "ts": c["timestamp"],
                "close": c["close"],
                "regime": state.get("regime"),
                "strategy": state.get("strategy"),
                "engine_action": (state.get("signal") or {}).get("action"),
                "engine_conf": (state.get("signal") or {}).get("confidence"),
                "policy_action": dec.action,
                "policy_conf": dec.confidence,
                "policy_reason": dec.reason,
                "rail_notes": dec.rail_notes,
                "pos_size_before": pos,
                "halted": bool(state.get("halted")),
            })

        # 3) Execute decision at t → pending fill on t+1 only
        if dec.action in ("BUY", "SELL") and t + 1 < n:
            eng._cf_snap = eng.snapshot_position()
            trade = eng.execute_signal(
                dec.action,
                float(dec.confidence),
                reason=dec.reason,
                strategy=str(state.get("strategy") or "MOMENTUM"),
                size_multiplier=float(dec.size_mult),
            )
            if trade is None:
                blocked_by_exec += 1
                eng._cf_snap = None
                decision_log[-1]["exec"] = "BLOCKED"
            else:
                pending = Pending(
                    side=trade.action,
                    limit_price=float(trade.price),
                    size=float(trade.amount),
                    conf=float(dec.confidence),
                    reason=dec.reason,
                    tick=t,
                )
                decision_log[-1]["exec"] = "QUEUED"
                decision_log[-1]["limit"] = trade.price
                decision_log[-1]["amount"] = trade.amount
        elif dec.action in ("BUY", "SELL"):
            decision_log[-1]["exec"] = "NO_NEXT_BAR"

        px = c["close"]
        eq = eng.balance + eng.position.size * px
        equity_curve.append(eq)

    # Restore env
    if prev_fric is None:
        os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)
    else:
        os.environ["HYDRA_FRICTION_GATE_DISABLED"] = prev_fric

    final_eq = equity_curve[-1] if equity_curve else initial
    ret_pct = (final_eq / initial - 1.0) * 100.0
    peak = initial
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)

    # Closed round-trips approx from trade log
    closed = sum(1 for x in trade_log if x.get("event") == "FILL" and x.get("side") == "SELL")

    return {
        "policy": policy_name,
        "initial": initial,
        "final_equity": final_eq,
        "return_pct": ret_pct,
        "max_dd_pct": max_dd,
        "fills": fills,
        "rejects": rejects,
        "fill_rate": fills / max(1, fills + rejects),
        "blocked_by_exec": blocked_by_exec,
        "decisions_buy": decisions_buy,
        "decisions_sell": decisions_sell,
        "closed_sell_fills": closed,
        "candles": n,
        "equity_start": equity_curve[0] if equity_curve else initial,
        "equity_end": final_eq,
        "decision_log": decision_log,
        "trade_log": trade_log[-200:],  # tail for review
        "equity_curve_sample": equity_curve[:: max(1, len(equity_curve) // 200)],
    }


def load_candles(db: str, pair: str, grain: int, t0: float, t1: float) -> List[Dict[str, float]]:
    con = sqlite3.connect(db)
    rows = list(con.execute(
        "SELECT ts, open, high, low, close, volume FROM ohlc "
        "WHERE pair=? AND grain_sec=? AND ts>=? AND ts<=? ORDER BY ts ASC",
        (pair, grain, t0, t1),
    ))
    con.close()
    out = []
    for ts, o, h, l, c, v in rows:
        out.append({
            "timestamp": float(ts),
            "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "volume": float(v or 0.0),
        })
    return out


def buy_and_hold(candles: List[Dict[str, float]], initial: float = 100.0) -> Dict[str, float]:
    if not candles:
        return {"return_pct": 0.0, "first": 0.0, "last": 0.0}
    first, last = candles[0]["close"], candles[-1]["close"]
    ret = (last / first - 1.0) * 100.0 if first else 0.0
    return {"return_pct": ret, "first": first, "last": last, "final_equity": initial * (1 + ret / 100.0)}


def extract_disagreements(results: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """Ticks where baseline vs AI proxies disagreed on action (for agent review)."""
    base = {d["tick"]: d for d in results["baseline"]["decision_log"] if d.get("policy_action") != "HOLD" or d.get("engine_action") not in (None, "HOLD")}
    # Index selective
    sel = {d["tick"]: d for d in results["ai_proxy_selective"]["decision_log"]}
    agg = {d["tick"]: d for d in results["ai_proxy_aggressive"]["decision_log"]}
    loose = {d["tick"]: d for d in results["max_loose"]["decision_log"]}

    rows = []
    ticks = sorted(set(base) | set(sel) | set(agg))
    for t in ticks:
        b = base.get(t) or sel.get(t) or agg.get(t)
        if not b:
            continue
        s = sel.get(t, {})
        a = agg.get(t, {})
        m = loose.get(t, {})
        ba = (b.get("policy_action") if t in base else b.get("engine_action"))
        # Prefer baseline log
        if t in results["baseline"]["decision_log"]:
            # find
            pass
        bdec = next((x for x in results["baseline"]["decision_log"] if x["tick"] == t), None)
        sdec = next((x for x in results["ai_proxy_selective"]["decision_log"] if x["tick"] == t), None)
        adec = next((x for x in results["ai_proxy_aggressive"]["decision_log"] if x["tick"] == t), None)
        mdec = next((x for x in results["max_loose"]["decision_log"] if x["tick"] == t), None)
        if not bdec:
            continue
        actions = {
            "baseline": bdec.get("policy_action"),
            "max_loose": mdec.get("policy_action") if mdec else None,
            "ai_proxy_selective": sdec.get("policy_action") if sdec else None,
            "ai_proxy_aggressive": adec.get("policy_action") if adec else None,
        }
        if len(set(x for x in actions.values() if x)) <= 1:
            continue
        rows.append({
            "tick": t,
            "ts": bdec.get("ts"),
            "close": bdec.get("close"),
            "regime": bdec.get("regime"),
            "engine_action": bdec.get("engine_action"),
            "engine_conf": bdec.get("engine_conf"),
            "actions": actions,
            "baseline_reason": bdec.get("policy_reason"),
            "selective_reason": sdec.get("policy_reason") if sdec else None,
            "selective_notes": sdec.get("rail_notes") if sdec else None,
            "baseline_exec": bdec.get("exec"),
            "selective_exec": sdec.get("exec") if sdec else None,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "hydra_history.sqlite"))
    ap.add_argument("--pair", default="SOL/USD")
    ap.add_argument("--grain", type=int, default=3600)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--fill", default="realistic", choices=["optimistic", "realistic", "pessimistic"])
    ap.add_argument("--initial", type=float, default=100.0)
    ap.add_argument("--out", default=str(ROOT / ".hydra-research" / "ai_control_counterfactual.json"))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    row = con.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM ohlc WHERE pair=? AND grain_sec=?",
        (args.pair, args.grain),
    ).fetchone()
    con.close()
    if not row or not row[2]:
        print(f"FAIL: no data for {args.pair} grain={args.grain}")
        return 2
    tmin, tmax, _ = row
    t0 = max(float(tmin), float(tmax) - args.days * 86400)
    t1 = float(tmax)

    candles = load_candles(args.db, args.pair, args.grain, t0, t1)
    if len(candles) < 100:
        print(f"FAIL: only {len(candles)} candles")
        return 2

    bh = buy_and_hold(candles, args.initial)
    print(f"Window {args.pair} grain={args.grain}s days~{args.days} n={len(candles)}")
    print(f"  BH return={bh['return_pct']:.2f}% first={bh['first']:.4f} last={bh['last']:.4f}")
    print(f"  fill_model={args.fill} (next-bar only, no lookahead)")

    results: Dict[str, Any] = {}
    for name, fn in POLICIES.items():
        print(f"  running policy={name} ...", flush=True)
        results[name] = run_policy(
            candles, name, fn,
            initial=args.initial,
            fill_model=args.fill,
            mode="competition",
            sample_every=50,
        )
        r = results[name]
        # Drop heavy logs from console summary
        print(
            f"    ret={r['return_pct']:+.2f}% dd={r['max_dd_pct']:.1f}% "
            f"fills={r['fills']} rejects={r['rejects']} fill_rate={r['fill_rate']:.3f} "
            f"blocked_exec={r['blocked_by_exec']} buy_dec={r['decisions_buy']} sell_dec={r['decisions_sell']}"
        )

    # Causal alpha vs baseline
    base_ret = results["baseline"]["return_pct"]
    comparison = []
    for name, r in results.items():
        comparison.append({
            "policy": name,
            "return_pct": r["return_pct"],
            "max_dd_pct": r["max_dd_pct"],
            "fill_rate": r["fill_rate"],
            "fills": r["fills"],
            "vs_baseline_pp": r["return_pct"] - base_ret,
            "vs_bh_pp": r["return_pct"] - bh["return_pct"],
            "beats_baseline": r["return_pct"] > base_ret,
            "positive_pnl": r["return_pct"] > 0,
            "beats_bh": r["return_pct"] > bh["return_pct"],
        })
    comparison.sort(key=lambda x: x["return_pct"], reverse=True)

    disagreements = extract_disagreements(results)
    # Keep a bounded set for agents (first 80 + last 40 by tick diversity)
    disagreements_sorted = sorted(disagreements, key=lambda x: x["tick"])
    if len(disagreements_sorted) > 120:
        disagreements_export = disagreements_sorted[:80] + disagreements_sorted[-40:]
    else:
        disagreements_export = disagreements_sorted

    # Slim decision logs for export (cap)
    slim_results = {}
    for name, r in results.items():
        slim = {k: v for k, v in r.items() if k not in ("decision_log", "trade_log", "equity_curve_sample")}
        # actionable-only decisions for agent review
        actionable = [d for d in r["decision_log"] if d.get("policy_action") in ("BUY", "SELL") or d.get("exec") == "BLOCKED"]
        slim["actionable_decisions"] = actionable[:300]
        slim["actionable_count"] = len(actionable)
        slim["trade_log_tail"] = r["trade_log"]
        slim_results[name] = slim

    # Answer scaffolding (deterministic from numbers)
    loose_names = ["loose_conf", "no_friction", "max_loose", "ai_proxy_aggressive"]
    selective = "ai_proxy_selective"
    any_loose_beats_base = any(
        results[n]["return_pct"] > base_ret for n in loose_names
    )
    any_loose_positive = any(results[n]["return_pct"] > 0 for n in loose_names)
    selective_beats_base = results[selective]["return_pct"] > base_ret
    selective_positive = results[selective]["return_pct"] > 0

    if any_loose_positive or selective_positive:
        verdict = "MIXED_OR_YES"
        verdict_detail = (
            "At least one more-control policy achieved positive absolute return "
            "on this causal window."
        )
    elif any_loose_beats_base or selective_beats_base:
        verdict = "NO_PROFIT_BUT_RELATIVE_YES"
        verdict_detail = (
            "More control never made absolute P&L positive, but at least one "
            "looser/AI-proxy policy beat baseline rails (lost less)."
        )
    else:
        verdict = "NO"
        verdict_detail = (
            "On this causal historical window, giving more control (lower conf, "
            "no friction, aggressive AI proxy) did not improve vs baseline and "
            "did not produce positive P&L."
        )

    out = {
        "meta": {
            "pair": args.pair,
            "grain_sec": args.grain,
            "days": args.days,
            "n_candles": len(candles),
            "t0": t0,
            "t1": t1,
            "fill_model": args.fill,
            "fee_bps": 16.0,
            "mode": "competition",
            "lookahead": "NONE — fills use only next bar; decisions use state at t only",
            "llm_calls": 0,
            "note": (
                "This measures rail-relaxation and AI-shaped discretionary proxies, "
                "not live Claude/Grok text. Required for full LLM answer: shadow "
                "log of brain decisions on same ticks with frozen prompts."
            ),
        },
        "buy_and_hold": bh,
        "comparison": comparison,
        "verdict": {
            "code": verdict,
            "detail": verdict_detail,
            "baseline_return_pct": base_ret,
            "bh_return_pct": bh["return_pct"],
            "any_loose_beats_baseline": any_loose_beats_base,
            "any_loose_positive": any_loose_positive,
            "selective_beats_baseline": selective_beats_base,
            "selective_positive": selective_positive,
        },
        "policies": slim_results,
        "disagreements_sample": disagreements_export,
        "disagreement_count": len(disagreements_sorted),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\n=== COMPARISON (best return first) ===")
    for c in comparison:
        print(
            f"  {c['policy']:22s} ret={c['return_pct']:+7.2f}% "
            f"vs_base={c['vs_baseline_pp']:+6.2f}pp vs_bh={c['vs_bh_pp']:+6.2f}pp "
            f"dd={c['max_dd_pct']:5.1f}% +pnl={c['positive_pnl']}"
        )
    print(f"\nVERDICT: {verdict}")
    print(verdict_detail)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
