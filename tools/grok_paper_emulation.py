#!/usr/bin/env python3
"""One Hydra decision cycle on paper money, with the three brain seats injected.

Sequence:
  1. Read live public Kraken candles and tickers (BTC/USD, ETH/USD, ZEC/USD).
     No order, cancel, or paper-order command is issued.
  2. Sample-variance of completed hourly log returns. Split the cash so
     weight_i * sigma_i is the same number on every pair (equal variance
     contribution). The engine then stores those exact closes, so the book's
     variance is the live series, not a redraw.
  3. Seed daily closes, ingest the hourly tape, and take one
     tick(generate_only=True) on the real HydraEngine (competition sizing,
     hold-through, friction, 15% breaker).
  4. Grok 4.7 plays Market Quant, Risk Manager, and Strategist from a
     decisions file. HydraBrain is constructed with an xAI provider only, so
     Claude Opus 5.5 is never selected, and the HTTP caller is replaced
     before deliberate().
  5. deliberate() runs on every pair so the real merge is visible. The order
     path still matches the agent: a HOLD never reaches the rules or
     execute_signal. A BUY or SELL is rewritten by that merge, then by
     R1-R11 and QFE, then booked in memory. A 16 bps maker fee is debited,
     matching the paper agent's fill.

  python tools/grok_paper_emulation.py self-check
  python tools/grok_paper_emulation.py prepare
  python tools/grok_paper_emulation.py apply --decisions PATH
  python tools/grok_paper_emulation.py walk --decisions PATH

`walk` scores a 60-minute window one hour at a time. Each Grok opinion
is locked before the next hour can fill or reject the post-only order.
The report is before/after dollars, accumulated ROI, and net ROI per
closed trade.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_brain import HydraBrain  # noqa: E402
from hydra_derivatives_stream import DerivativesStream  # noqa: E402
from hydra_engine import HydraEngine, SIZING_COMPETITION  # noqa: E402
from hydra_kraken_cli import KrakenCLI  # noqa: E402
from hydra_quant_rules import apply_rules, evaluate_qfe  # noqa: E402
from hydra_rm_features import cross_pair_corr, realized_vol_pct  # noqa: E402

PAIRS = ("BTC/USD", "ETH/USD", "ZEC/USD")
NAV_DEFAULT = 20000.0
HOURLY_S = 3600
MAKER_FEE_RATE = 0.0016  # paper agent: base maker tier on the synthetic fill
MIN_RETURNS = 30
DEFAULT_OUT = Path(os.environ.get("TEMP") or ".") / "hydra-grok-paper-cycle"


def _halt_flatten(state: dict) -> bool:
    """Same predicate as hydra_agent._is_halt_flatten."""
    sig = state.get("signal") if isinstance(state, dict) else None
    if not isinstance(sig, dict):
        return False
    return (
        str(sig.get("action") or "") == "SELL"
        and str(sig.get("reason") or "").startswith("HALT FLATTEN")
    )


def assert_source_has_no_orders() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    for name in ("paper_buy", "paper_sell", "order_buy", "order_sell", "cancel_order"):
        if ("KrakenCLI." + name) in text:
            raise SystemExit("order helper referenced: " + name)


def equal_variance_allocation(sigmas: Sequence[float], nav: float) -> Dict[str, Any]:
    """Weights proportional to 1/sigma, so every sleeve has the same w*sigma.

    That product is the volatility contribution. Squaring it makes the
    variance contribution equal too. Cash is rounded to cents; the leftover
    penny stays on the largest weight so the book sums to nav.
    """
    if nav <= 0 or not math.isfinite(nav):
        raise ValueError("nav must be a positive finite number")
    if any((not math.isfinite(s)) or s <= 0 for s in sigmas):
        raise ValueError("sigma must be finite and positive")
    inv = [1.0 / s for s in sigmas]
    z = sum(inv)
    weights = [v / z for v in inv]
    cash = [round(nav * w, 2) for w in weights]
    drift = round(nav - sum(cash), 2)
    k = max(range(len(weights)), key=lambda i: (weights[i], -i))
    cash[k] = round(cash[k] + drift, 2)
    contribution = [weights[i] * sigmas[i] for i in range(len(sigmas))]
    return {
        "weights": weights,
        "cash": cash,
        "contribution": contribution,
        "contribution_span": max(contribution) - min(contribution),
    }


def sample_variance(closes: Sequence[float]) -> Tuple[float, float, int]:
    rets: List[float] = []
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            raise ValueError("non-positive close in the live series")
        rets.append(math.log(b / a))
    if len(rets) < MIN_RETURNS:
        raise ValueError(f"need {MIN_RETURNS} hourly returns, got {len(rets)}")
    var = statistics.variance(rets)
    return var, math.sqrt(var), len(rets)


def clean_candles(rows: Sequence[dict]) -> List[dict]:
    by_ts: Dict[float, dict] = {}
    for row in rows or []:
        try:
            ts = float(row.get("timestamp") or 0)
            close = float(row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or close <= 0:
            continue
        by_ts[ts] = row
    return [by_ts[k] for k in sorted(by_ts)]


def completed_bars(candles: Sequence[dict], now: float, interval_s: int) -> Tuple[List[dict], Optional[dict]]:
    """Drop the forming bar. Kraken stamps a candle with its open time."""
    if not candles:
        return [], None
    bucket = int(now // interval_s) * interval_s
    last = candles[-1]
    if float(last["timestamp"]) >= bucket:
        return list(candles[:-1]), last
    return list(candles), None


def series_matches(engine_prices: Sequence[float], source_closes: Sequence[float]) -> bool:
    if not engine_prices:
        return False
    tail = list(source_closes[-len(engine_prices):])
    if len(tail) != len(engine_prices):
        return False
    return all(
        math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-8)
        for a, b in zip(engine_prices, tail)
    )


def hourly_return_map(candles: Sequence[dict]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for prev, cur in zip(candles, candles[1:]):
        a = float(prev["close"])
        b = float(cur["close"])
        if a > 0 and b > 0:
            out.append((float(cur["timestamp"]), math.log(b / a)))
    return out


def pearson_aligned(
    a: Sequence[Tuple[float, float]],
    b: Sequence[Tuple[float, float]],
    n: int = 24,
) -> Optional[float]:
    am = {ts: r for ts, r in a}
    bm = {ts: r for ts, r in b}
    keys = sorted(set(am) & set(bm))
    use = keys[-n:]
    if len(use) < 12:
        return None
    return cross_pair_corr(
        [am[k] for k in use], [bm[k] for k in use], min_samples=12,
    )


def _ticker_row(ticker: dict) -> dict:
    """Accept KrakenCLI's flat bid/ask row or the v0.4.1 pair envelope.

    `kraken ticker` currently returns ``{PAIR: {bid_price, ask_price,
    last_price}}``. KrakenCLI.ticker() only unwraps the legacy ``c``/``a``/``b``
    shape, so the emulation reads the envelope itself.
    """
    if not isinstance(ticker, dict) or "error" in ticker:
        return {}
    if "bid" in ticker or "bid_price" in ticker:
        return ticker
    for val in ticker.values():
        if isinstance(val, dict) and ("bid_price" in val or "bid" in val):
            return val
    return {}


def spread_of(ticker: dict) -> Optional[dict]:
    row = _ticker_row(ticker)
    try:
        bid = float(row["bid"] if "bid" in row else row["bid_price"])
        ask = float(row["ask"] if "ask" in row else row["ask_price"])
        last = float(row.get("price") or row.get("last_price") or row.get("last") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "spread_bps": round((ask - bid) / mid * 10000.0, 2),
    }


def make_engine(pair: str, cash: float, hourly: Sequence[dict], daily: Sequence[dict]) -> HydraEngine:
    eng = HydraEngine(
        initial_balance=cash,
        asset=pair,
        sizing=dict(SIZING_COMPETITION),
        candle_interval=60,
    )
    eng.seed_daily_closes(list(daily))
    for bar in hourly:
        eng.ingest_candle(bar)
    return eng


def snap_fields(snap: Any) -> Dict[str, Any]:
    if snap is None:
        return {
            "funding_bps_8h": None,
            "oi_delta_1h_pct": None,
            "oi_price_regime": None,
            "basis_apr_pct": None,
            "staleness_s": 1_000_000.0,
            "synthetic_pair": False,
            "basis_available": True,
            "fetch_error_streak": 1,
            "mark_price": None,
        }
    stale = snap.staleness_s
    if not isinstance(stale, (int, float)) or stale != stale or stale == float("inf"):
        stale_out = 1_000_000.0
    else:
        stale_out = round(float(stale), 1)
    return {
        "funding_bps_8h": snap.funding_bps_8h,
        "funding_predicted_bps": snap.funding_predicted_bps,
        "oi_delta_1h_pct": snap.oi_delta_1h_pct,
        "oi_delta_24h_pct": snap.oi_delta_24h_pct,
        "oi_price_regime": snap.oi_price_regime,
        "basis_apr_pct": snap.basis_apr_pct,
        "staleness_s": stale_out,
        "synthetic_pair": bool(snap.synthetic),
        "basis_available": bool(snap.basis_available),
        "fetch_error_streak": int(snap.fetch_error_streak),
        "mark_price": snap.mark_price,
        "open_interest": snap.open_interest,
    }


def build_qi(eng: HydraEngine, saved: dict) -> Dict[str, Any]:
    """Production shape for a covered pair. cross_pair_corr stays None.

    The three-core book has no TradingTriangle, so the agent leaves that
    field empty. Live Pearson is reported beside the book, not stuffed
    into the field the rules would treat as the triangle statistic.
    """
    dicts = [{"close": c.close, "ts": c.timestamp} for c in eng.candles]
    return {
        "funding_bps_8h": saved.get("funding_bps_8h"),
        "oi_delta_1h_pct": saved.get("oi_delta_1h_pct"),
        "oi_price_regime": saved.get("oi_price_regime"),
        "basis_apr_pct": saved.get("basis_apr_pct"),
        "staleness_s": saved.get("staleness_s"),
        "synthetic_pair": bool(saved.get("synthetic_pair")),
        "basis_available": saved.get("basis_available", True),
        "derivatives_covered": True,
        "cvd_divergence_sigma": eng.cvd_divergence_sigma(),
        "realized_vol_1h_pct": realized_vol_pct(dicts, 60),
        "realized_vol_24h_pct": realized_vol_pct(dicts, 1440),
        "drawdown_velocity_pct_per_hr": None,
        "fill_rate_24h": None,
        "avg_slippage_bps_24h": None,
        "minutes_since_last_trade": None,
        "cross_pair_corr_24h": None,
    }


def slim_state(state: dict) -> dict:
    keep = (
        "asset", "price", "regime", "strategy", "signal", "indicators",
        "trend", "volatility", "volume", "position", "portfolio",
        "halted", "halt_reason", "candle_interval", "friction_skip",
    )
    out = {k: state.get(k) for k in keep}
    candles = state.get("candles") or []
    out["last_5"] = [{"t": c.get("t"), "c": c.get("c")} for c in candles[-5:]]
    return out


def _rule_rows(result: Any) -> List[dict]:
    return [
        {
            "rule_id": f.rule_id,
            "name": f.name,
            "effect": f.effect,
            "size_mult": f.size_mult,
            "reason": f.reason,
        }
        for f in result.triggered
    ]


def bind_decision(state: dict, decision: Any) -> dict:
    """Rewrite state['signal'] the way hydra_agent._apply_brain does.

    Size is brain (quant x risk, already on the decision) times the rule
    stack. A rule force-hold becomes HOLD unless this signal is the
    breaker's flatten. An OVERRIDE is re-scored on the new direction.
    QFE can restore a profitable SELL the rules just blocked.
    """
    if decision.fallback:
        return {
            "fallback": True,
            "final_action": state["signal"]["action"],
            "size_multiplier": 0.0,
            "why": "brain fallback; no order",
        }

    engine_action = state["signal"]["action"]
    rules_triggered: List[dict] = []
    rules_force_hold = False
    rules_force_hold_reason = ""
    rules_size_mult = 1.0
    quant_out = {
        "positioning_bias": decision.positioning_bias or "",
        "force_hold": False,
    }
    qi = state.get("quant_indicators") or None
    disabled = os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") == "1"
    if not disabled:
        rr = apply_rules(engine_action, quant_out, qi)
        rules_triggered = _rule_rows(rr)
        rules_force_hold = rr.force_hold
        rules_force_hold_reason = rr.force_hold_reason
        rules_size_mult = rr.size_multiplier

    brain_size = decision.size_multiplier
    try:
        brain_size = float(1.0 if brain_size is None else brain_size)
    except (TypeError, ValueError):
        brain_size = 1.0
    if not math.isfinite(brain_size):
        brain_size = 1.0
    brain_size = max(0.0, min(1.5, brain_size))
    final_size = max(0.0, min(1.5, brain_size * rules_size_mult))
    if rules_force_hold:
        final_size = 0.0

    if _halt_flatten(state):
        pass
    elif rules_force_hold:
        state["signal"]["action"] = "HOLD"
        state["signal"]["reason"] = (
            f"[QUANT RULES FORCE_HOLD] {rules_force_hold_reason}"
        )
    elif decision.action == "OVERRIDE":
        state["signal"]["action"] = decision.final_signal
        state["signal"]["reason"] = f"[AI OVERRIDE] {decision.combined_summary}"
        if (
            not disabled
            and decision.final_signal
            and decision.final_signal != engine_action
        ):
            rr2 = apply_rules(decision.final_signal, quant_out, qi)
            rules_size_mult = rr2.size_multiplier
            rules_triggered = _rule_rows(rr2)
            final_size = max(0.0, min(1.5, brain_size * rules_size_mult))
            if rr2.force_hold:
                rules_force_hold = True
                rules_force_hold_reason = rr2.force_hold_reason
                final_size = 0.0
                state["signal"]["action"] = "HOLD"
                state["signal"]["reason"] = (
                    f"[QUANT RULES FORCE_HOLD post-OVERRIDE] {rr2.force_hold_reason}"
                )
    elif decision.action == "ADJUST":
        state["signal"]["reason"] = f"[AI ADJUSTED] {decision.combined_summary}"

    qfe_active = False
    qfe_reason = ""
    if (
        engine_action == "SELL"
        and state["signal"]["action"] == "HOLD"
        and not disabled
    ):
        pos = state.get("position") or {}
        pos_size = float(pos.get("size") or 0)
        avg_entry = float(pos.get("avg_entry") or 0)
        price = float(state.get("price") or 0)
        if pos_size > 0 and avg_entry > 0 and price > 0:
            pnl_pct = (price - avg_entry) / avg_entry * 100.0
            qfe = evaluate_qfe(
                position_size=pos_size,
                unrealized_pnl_pct=pnl_pct,
                quant_indicators=qi,
                positioning_bias=decision.positioning_bias or "",
            )
            if qfe.force_exit:
                qfe_active = True
                qfe_reason = qfe.force_exit_reason
                state["signal"]["action"] = "SELL"
                state["signal"]["reason"] = f"[QFE PROFIT EXIT] {qfe_reason}"
                final_size = 1.0

    return {
        "fallback": False,
        "engine_action": engine_action,
        "brain_action": decision.action,
        "brain_signal": decision.final_signal,
        "escalated": bool(decision.escalated),
        "final_action": state["signal"]["action"],
        "final_reason": state["signal"]["reason"],
        "size_multiplier": final_size,
        "brain_size": brain_size,
        "rules_size": rules_size_mult,
        "rules": rules_triggered,
        "rules_force_hold": rules_force_hold,
        "rules_force_hold_reason": rules_force_hold_reason,
        "qfe_active": qfe_active,
        "qfe_reason": qfe_reason,
        "summary": decision.combined_summary,
        "analyst_reasoning": decision.analyst_reasoning,
        "risk_reasoning": decision.risk_reasoning,
        "strategist_reasoning": decision.strategist_reasoning,
        "positioning_bias": decision.positioning_bias,
        "risk_flags": list(decision.risk_flags or []),
        "portfolio_health": decision.portfolio_health,
        "decision_cost_usd": float(decision.decision_cost_usd or 0.0),
    }


def _clamp_mult(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(out):
        return 1.0
    return max(0.0, min(1.5, out))


def validate_players(doc: dict, pairs: Sequence[str]) -> Dict[str, dict]:
    if doc.get("model") != "grok-4.7":
        raise SystemExit("decisions.model must be grok-4.7")
    if doc.get("emulated") is not True:
        raise SystemExit("decisions.emulated must be true (this file is not an API response)")
    table = doc.get("pairs")
    if not isinstance(table, dict):
        raise SystemExit("decisions.pairs missing")
    for pair in pairs:
        seat = table.get(pair)
        if not isinstance(seat, dict):
            raise SystemExit(f"missing players for {pair}")
        quant = seat.get("quant")
        risk = seat.get("risk")
        strat = seat.get("strategist")
        if not all(isinstance(x, dict) for x in (quant, risk, strat)):
            raise SystemExit(f"{pair} needs quant, risk, and strategist objects")
        scenario = quant.get("scenario") or {}
        try:
            total = (
                float(scenario["p_up"])
                + float(scenario["p_flat"])
                + float(scenario["p_down"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"{pair} quant scenario is incomplete") from exc
        if not 0.98 <= total <= 1.02:
            raise SystemExit(f"{pair} quant probabilities sum to {total:.4f}")
        if quant.get("suggested_action") not in ("BUY", "SELL", "HOLD"):
            raise SystemExit(f"{pair} quant suggested_action invalid")
        if risk.get("decision") not in ("CONFIRM", "ADJUST", "OVERRIDE"):
            raise SystemExit(f"{pair} risk decision invalid")
        if risk.get("final_action") not in ("BUY", "SELL", "HOLD"):
            raise SystemExit(f"{pair} risk final_action invalid")
        if strat.get("decision") not in ("CONFIRM", "OVERRIDE"):
            raise SystemExit(f"{pair} strategist decision invalid")
        if strat.get("final_action") not in ("BUY", "SELL", "HOLD"):
            raise SystemExit(f"{pair} strategist final_action invalid")
        quant["size_multiplier"] = _clamp_mult(quant.get("size_multiplier", 1.0))
        risk["size_multiplier"] = _clamp_mult(risk.get("size_multiplier", 1.0))
        quant["force_hold"] = bool(quant.get("force_hold"))
    return table


def install_players(brain: HydraBrain, players: Dict[str, dict]) -> None:
    def run_quant(state: dict):
        parsed = dict(players[state["asset"]]["quant"])
        parsed["size_multiplier"] = _clamp_mult(parsed.get("size_multiplier"))
        parsed["force_hold"] = bool(parsed.get("force_hold"))
        return parsed, 0, 0

    def run_risk(state: dict, analyst: dict):
        parsed = dict(players[state["asset"]]["risk"])
        parsed["size_multiplier"] = _clamp_mult(parsed.get("size_multiplier"))
        return parsed, 0, 0

    def run_strategist(state: dict, analyst: dict, risk: dict):
        return dict(players[state["asset"]]["strategist"]), 0, 0

    def no_http(*_a: Any, **_k: Any):
        raise RuntimeError("LLM HTTP is disabled; the three seats are injected")

    brain._run_quant = run_quant
    brain._run_risk_manager = run_risk
    brain._run_strategist = run_strategist
    brain._call_llm = no_http
    brain._call_llm_with_tools = no_http


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def self_check() -> None:
    assert_source_has_no_orders()
    sigmas = (0.01, 0.02, 0.04)
    alloc = equal_variance_allocation(sigmas, NAV_DEFAULT)
    if abs(alloc["contribution_span"]) > 1e-12:
        raise SystemExit(f"contributions diverged: {alloc['contribution']}")
    if abs(sum(alloc["weights"]) - 1.0) > 1e-12:
        raise SystemExit("weights do not sum to 1")
    if abs(sum(alloc["cash"]) - NAV_DEFAULT) > 0.001:
        raise SystemExit(f"cash sums to {sum(alloc['cash'])}")
    closes = [100.0]
    for i in range(1, 41):
        closes.append(closes[-1] * (1.0 + (0.001 if i % 2 else -0.0004)))
    var, sigma, n = sample_variance(closes)
    if n != 40 or var <= 0 or not math.isclose(sigma, math.sqrt(var)):
        raise SystemExit("sample variance helper failed")
    # Identity: a copied tail has the same sample variance as its source.
    tail = closes[-25:]
    v1, _, _ = sample_variance(closes)
    copied = list(closes)
    v2, _, _ = sample_variance(copied)
    if not math.isclose(v1, v2, rel_tol=1e-15):
        raise SystemExit("copied series changed variance")
    if not series_matches(tail, closes):
        raise SystemExit("series identity failed")
    live_row = {"BTC/USD": {"bid_price": 100.0, "ask_price": 100.1, "last_price": 100.05}}
    quote = spread_of(live_row)
    if quote is None or not math.isclose(quote["spread_bps"], 9.995, abs_tol=0.02):
        raise SystemExit(f"ticker envelope parse failed: {quote}")
    flat = spread_of({"bid": 100.0, "ask": 100.1, "price": 100.05})
    if flat is None or flat["bid"] != 100.0:
        raise SystemExit("flat ticker parse failed")
    print("self-check ok")
    print(f"  example w*sigma span {alloc['contribution_span']:.3e}")
    print(f"  example cash {alloc['cash']}")


def _fetch_market(now: float) -> Tuple[dict, dict]:
    raw: Dict[str, Any] = {}
    for pair in PAIRS:
        hourly, _cursor = KrakenCLI.ohlc_paged(pair, interval=60, since=0)
        daily, _cursor = KrakenCLI.ohlc_paged(pair, interval=1440, since=0)
        ticker = KrakenCLI.ticker(pair)
        if isinstance(hourly, dict) and "error" in hourly:
            raise SystemExit(f"ohlc 60 {pair}: {hourly['error']}")
        hourly_c = clean_candles(hourly if isinstance(hourly, list) else [])
        daily_c = clean_candles(daily if isinstance(daily, list) else [])
        if len(hourly_c) < MIN_RETURNS + 1:
            raise SystemExit(f"{pair} returned {len(hourly_c)} hourly bars")
        raw[pair] = {
            "hourly": hourly_c,
            "daily": daily_c,
            "ticker": ticker if isinstance(ticker, dict) else {},
        }
        print(
            f"  fetched {pair}: {len(hourly_c)} hourly, {len(daily_c)} daily",
            flush=True,
        )

    # Public futures read sits outside KrakenCLI, so space it from the last REST call.
    time.sleep(2.0)
    deriv_error = ""
    snaps: Dict[str, dict] = {}
    try:
        stream = DerivativesStream(list(PAIRS))
        updated = stream.poll_once()
        for pair in PAIRS:
            snaps[pair] = snap_fields(stream.latest(pair))
        if not updated:
            deriv_error = "futures poll returned no update"
    except Exception as exc:
        deriv_error = f"{type(exc).__name__}: {exc}"
        for pair in PAIRS:
            snaps[pair] = snap_fields(None)
    return raw, {"snaps": snaps, "error": deriv_error, "now": now}


def _heartbeat(pairs: Sequence[str]) -> Dict[str, Any]:
    try:
        from hydra_heartbeat_surface import HeartbeatSurface
        surface = HeartbeatSurface(list(pairs))
        return {p: surface.indicator_block(p) for p in pairs}
    except Exception as exc:
        return {p: {"status": "unavailable", "why": f"{type(exc).__name__}: {exc}"} for p in pairs}


def prepare(out_dir: Path, nav: float) -> None:
    assert_source_has_no_orders()
    now = time.time()
    print("fetching live public market data", flush=True)
    raw, deriv = _fetch_market(now)
    completed: Dict[str, List[dict]] = {}
    stats: Dict[str, dict] = {}
    sigmas: List[float] = []
    for pair in PAIRS:
        done, forming = completed_bars(raw[pair]["hourly"], now, HOURLY_S)
        if len(done) < MIN_RETURNS + 1:
            raise SystemExit(f"{pair} has only {len(done)} completed hourly bars")
        closes = [float(c["close"]) for c in done]
        var, sigma, n_ret = sample_variance(closes)
        ann = sigma * math.sqrt(24 * 365) * 100.0
        completed[pair] = done
        stats[pair] = {
            "variance": var,
            "sigma": sigma,
            "n_returns": n_ret,
            "ann_vol_pct": ann,
            "first_ts": done[0]["timestamp"],
            "last_completed_ts": done[-1]["timestamp"],
            "forming": forming is not None,
        }
        sigmas.append(sigma)

    alloc = equal_variance_allocation(sigmas, nav)
    if alloc["contribution_span"] > 1e-9:
        raise SystemExit("equal-variance allocation failed on live sigmas")

    hb = _heartbeat(PAIRS)
    ret_maps = {p: hourly_return_map(completed[p]) for p in PAIRS}
    corr = {
        "BTC/USD~ETH/USD": pearson_aligned(ret_maps["BTC/USD"], ret_maps["ETH/USD"]),
        "BTC/USD~ZEC/USD": pearson_aligned(ret_maps["BTC/USD"], ret_maps["ZEC/USD"]),
        "ETH/USD~ZEC/USD": pearson_aligned(ret_maps["ETH/USD"], ret_maps["ZEC/USD"]),
        "window_hours": 24,
        "note": (
            "Live hourly Pearson. Production cross_pair_corr_24h is null "
            "because the three-core book has no TradingTriangle."
        ),
    }

    brief_pairs: Dict[str, Any] = {}
    market_pairs: Dict[str, Any] = {}
    for i, pair in enumerate(PAIRS):
        cash = alloc["cash"][i]
        eng = make_engine(pair, cash, raw[pair]["hourly"], raw[pair]["daily"])
        source_closes = [float(c["close"]) for c in raw[pair]["hourly"]]
        if not series_matches(eng.prices, source_closes):
            raise SystemExit(f"{pair} engine closes are not the live tail")
        tail = source_closes[-len(eng.prices):]
        v_src, _, _ = sample_variance(tail if len(tail) > MIN_RETURNS else source_closes)
        v_eng, _, _ = sample_variance(list(eng.prices) if len(eng.prices) > MIN_RETURNS else tail)
        if not math.isclose(v_src, v_eng, rel_tol=1e-9, abs_tol=0.0):
            raise SystemExit(f"{pair} engine variance != live variance")
        state = eng.tick(generate_only=True)
        qi = build_qi(eng, deriv["snaps"][pair])
        state["quant_indicators"] = qi
        quote = spread_of(raw[pair]["ticker"])
        if quote:
            state["spread"] = quote
        row = slim_state(state)
        row.update({
            "cash": cash,
            "weight": alloc["weights"][i],
            "sigma_hourly": stats[pair]["sigma"],
            "variance_hourly": stats[pair]["variance"],
            "ann_vol_pct": stats[pair]["ann_vol_pct"],
            "variance_contribution": alloc["contribution"][i],
            "n_returns": stats[pair]["n_returns"],
            "window": [stats[pair]["first_ts"], stats[pair]["last_completed_ts"]],
            "forming_bar": stats[pair]["forming"],
            "bars_in_engine": len(eng.prices),
            "daily_bars": len(raw[pair]["daily"]),
            "daily_trend_score": eng.daily_trend_score(),
            "daily_trend_long": eng.daily_trend_long(),
            "daily_realized_vol_pct": eng.daily_realized_vol_pct(),
            "spread": quote,
            "quant_indicators": qi,
            "heartbeat": hb.get(pair),
            "derivatives_mark": deriv["snaps"][pair].get("mark_price"),
        })
        brief_pairs[pair] = row
        market_pairs[pair] = {
            "hourly": raw[pair]["hourly"],
            "daily": raw[pair]["daily"],
            "ticker": raw[pair]["ticker"],
            "cash": cash,
            "weight": alloc["weights"][i],
            "sigma": stats[pair]["sigma"],
            "variance": stats[pair]["variance"],
            "ann_vol_pct": stats[pair]["ann_vol_pct"],
            "derivatives": deriv["snaps"][pair],
            "expected_signal": state["signal"],
            "heartbeat": hb.get(pair),
        }
        sig = state["signal"]
        print(
            f"  {pair} cash={cash:.2f} ann_vol={stats[pair]['ann_vol_pct']:.1f}% "
            f"engine={sig['action']} conf={sig['confidence']} "
            f"daily_long={eng.daily_trend_long()} regime={state['regime']}",
            flush=True,
        )

    market = {
        "nav": nav,
        "asof_unix": now,
        "pairs_order": list(PAIRS),
        "mode": "competition",
        "fill_model": "engine close via execute_signal; 16 bps maker fee; no exchange order",
        "model_ignored": "claude-opus-5-5",
        "players": "grok-4.7 quant + risk + strategist, injected, no HTTP",
        "derivatives_error": deriv["error"],
        "variance_proof": {
            "method": "w_i = (1/sigma_i) / sum(1/sigma); sigma = sqrt(sample variance of completed hourly log returns)",
            "contribution_span": alloc["contribution_span"],
            "contributions": {
                pair: alloc["contribution"][i] for i, pair in enumerate(PAIRS)
            },
            "identity": "HydraEngine.prices equals the tail of the live hourly closes",
        },
        "live_hourly_corr": corr,
        "pairs": market_pairs,
    }
    brief = {
        "nav": nav,
        "asof_unix": now,
        "live_hourly_corr": corr,
        "derivatives_error": deriv["error"],
        "variance_proof": market["variance_proof"],
        "pairs": brief_pairs,
        "gate": (
            "deliberate() runs for every pair. The order path still skips "
            "HOLD, matching hydra_agent._apply_brain."
        ),
    }
    _dump(out_dir / "market.json", market)
    _dump(out_dir / "brief.json", brief)
    print(f"wrote {out_dir / 'brief.json'}")
    print(f"wrote {out_dir / 'market.json'}")


def _brain(out_dir: Path) -> HydraBrain:
    os.environ["HYDRA_BRAIN_JSONL"] = str(out_dir / "brain.jsonl")
    brain = HydraBrain(xai_key="emulated-no-http")
    if brain.primary_provider != "xai" or brain.primary_model != "grok-4.7":
        raise SystemExit(
            f"expected grok-4.7 on xai, got {brain.primary_provider} {brain.primary_model}"
        )
    if not brain.has_strategist:
        raise SystemExit("strategist seat was not constructed")
    return brain


def apply_cycle(out_dir: Path, decisions_path: Path) -> dict:
    assert_source_has_no_orders()
    market = _load(out_dir / "market.json")
    players_doc = _load(decisions_path)
    players = validate_players(players_doc, market["pairs_order"])
    brain = _brain(out_dir)
    install_players(brain, players)

    results = []
    nav0 = 0.0
    nav1 = 0.0
    fees = 0.0
    for pair in market["pairs_order"]:
        spec = market["pairs"][pair]
        eng = make_engine(pair, spec["cash"], spec["hourly"], spec["daily"])
        source_closes = [float(c["close"]) for c in spec["hourly"]]
        if not series_matches(eng.prices, source_closes):
            raise SystemExit(f"{pair} replay is not the prepared live tail")
        state = eng.tick(generate_only=True)
        got = state["signal"]
        exp = spec["expected_signal"]
        if got["action"] != exp["action"] or got["reason"] != exp["reason"]:
            raise SystemExit(f"{pair} signal drifted since prepare")
        qi = build_qi(eng, spec["derivatives"])
        state["quant_indicators"] = qi
        quote = spread_of(spec.get("ticker") or {})
        if quote:
            state["spread"] = quote
        seat = players[pair]
        transcript = {
            "quant": {
                "suggested_action": seat["quant"].get("suggested_action"),
                "conviction": seat["quant"].get("conviction"),
                "size_multiplier": seat["quant"].get("size_multiplier"),
                "force_hold": seat["quant"].get("force_hold"),
                "signal_agreement": seat["quant"].get("signal_agreement"),
                "positioning_bias": seat["quant"].get("positioning_bias"),
                "scenario": seat["quant"].get("scenario"),
                "indicators_used": seat["quant"].get("indicators_used"),
                "key_factors": seat["quant"].get("key_factors"),
                "concern": seat["quant"].get("concern"),
                "reasoning": seat["quant"].get("reasoning"),
            },
            "risk": {
                "decision": seat["risk"].get("decision"),
                "final_action": seat["risk"].get("final_action"),
                "size_multiplier": seat["risk"].get("size_multiplier"),
                "risk_metrics": seat["risk"].get("risk_metrics"),
                "risk_flags": seat["risk"].get("risk_flags"),
                "portfolio_health": seat["risk"].get("portfolio_health"),
                "reasoning": seat["risk"].get("reasoning"),
            },
            "strategist": {
                "decision": seat["strategist"].get("decision"),
                "final_action": seat["strategist"].get("final_action"),
                "reasoning": seat["strategist"].get("reasoning"),
            },
        }
        engine_action = got["action"]
        # The three injected runners go through HydraBrain.deliberate on every
        # pair. The order path still matches _apply_brain: a HOLD never
        # reaches bind_decision or execute_signal.
        decision = brain.deliberate(state)
        merge = {
            "action": decision.action,
            "final_signal": decision.final_signal,
            "confidence_adj": decision.confidence_adj,
            "size_multiplier": decision.size_multiplier,
            "escalated": bool(decision.escalated),
            "fallback": bool(decision.fallback),
            "decision_cost_usd": float(decision.decision_cost_usd or 0.0),
            "summary": decision.combined_summary,
            "model": brain.primary_model,
        }
        binding = None
        trade_out = None
        fee = 0.0
        why = ""
        if engine_action == "HOLD":
            why = (
                "engine HOLD; order path skipped "
                "(hydra_agent._apply_brain returns before the brain)"
            )
        else:
            binding = bind_decision(state, decision)
            if binding.get("fallback"):
                why = binding.get("why") or "fallback"
            elif state["signal"]["action"] in ("BUY", "SELL"):
                eng._last_friction_skip = None
                trade = eng.execute_signal(
                    state["signal"]["action"],
                    float(state["signal"]["confidence"]),
                    reason=state["signal"]["reason"],
                    strategy=state.get("strategy") or "MOMENTUM",
                    size_multiplier=float(binding["size_multiplier"]),
                    decision_cost_usd=float(binding.get("decision_cost_usd") or 0.0),
                )
                if trade is None:
                    why = (
                        f"execute_signal returned None; friction="
                        f"{eng._last_friction_skip}"
                    )
                else:
                    fee = float(trade.value) * MAKER_FEE_RATE
                    eng.balance -= fee
                    fees += fee
                    trade_out = {
                        "action": trade.action,
                        "price": trade.price,
                        "amount": trade.amount,
                        "notional": trade.value,
                        "fee_usd": fee,
                        "confidence": trade.confidence,
                        "reason": trade.reason,
                    }
                    why = "filled on the in-memory book"
            else:
                why = state["signal"]["reason"]

        price = eng.prices[-1] if eng.prices else 0.0
        equity = eng.balance + eng.position.size * price
        nav0 += spec["cash"]
        nav1 += equity
        results.append({
            "pair": pair,
            "cash_start": spec["cash"],
            "weight": spec["weight"],
            "sigma_hourly": spec["sigma"],
            "ann_vol_pct": spec["ann_vol_pct"],
            "engine_signal": exp,
            "regime": state["regime"],
            "price": state["price"],
            "spread": quote,
            "daily_trend_long": eng.daily_trend_long(),
            "daily_trend_score": eng.daily_trend_score(),
            "on_order_path": engine_action != "HOLD",
            "transcript": transcript,
            "merge": merge,
            "binding": binding,
            "trade": trade_out,
            "why": why,
            "fee_usd": fee,
            "position_size": eng.position.size,
            "avg_entry": eng.position.avg_entry,
            "balance": eng.balance,
            "equity": equity,
            "pnl_usd": equity - spec["cash"],
        })
        print(
            f"  {pair} engine={engine_action} merge={merge['final_signal']} "
            f"escalated={merge['escalated']} "
            f"equity={equity:.2f} pnl={equity - spec['cash']:+.2f} {why}",
            flush=True,
        )

    out = {
        "model": "grok-4.7",
        "provider": brain.primary_provider,
        "claude_constructed": False,
        "llm_http": False,
        "orders_sent": 0,
        "nav_start": nav0,
        "nav_end": nav1,
        "pnl_usd": nav1 - nav0,
        "fees_usd": fees,
        "portfolio_note": players_doc.get("portfolio") or "",
        "variance_proof": market.get("variance_proof"),
        "live_hourly_corr": market.get("live_hourly_corr"),
        "derivatives_error": market.get("derivatives_error"),
        "fill_model": market.get("fill_model"),
        "pairs": results,
    }
    _dump(out_dir / "results.json", out)
    print(f"portfolio {nav0:.2f} -> {nav1:.2f}  pnl {nav1 - nav0:+.4f}  fees {fees:.4f}")
    print(f"wrote {out_dir / 'results.json'}")
    return out


def ledger_cmd(out_dir: Path, history_path: Path, warmup_bars: int, skip_last: int) -> None:
    """Record one opinion per pair per scored hour and the resulting book."""
    from tools.grok_paper_ledger import ledger_opinion, summarize, write_history
    from tools.grok_paper_walk import common_timestamps, walk_book

    market = _load(out_dir / "market.json")
    bars = {}
    daily = {}
    now = float(market["asof_unix"])
    for pair in market["pairs_order"]:
        done, _forming = completed_bars(market["pairs"][pair]["hourly"], now, HOURLY_S)
        bars[pair] = done
        daily[pair] = market["pairs"][pair]["daily"]
    stamps = common_timestamps(bars)
    if len(stamps) <= warmup_bars + skip_last + 2:
        raise SystemExit(
            f"need more than {warmup_bars + skip_last + 2} aligned hours, have {len(stamps)}"
        )
    score_start = stamps[warmup_bars]
    score_end = stamps[-skip_last] if skip_last else stamps[-1] + HOURLY_S
    opinions: List[dict] = []

    def on_opinion(ts: float, view: dict, chosen: dict) -> None:
        for pair, opinion in chosen.items():
            opinions.append({
                "ts": ts, "pair": pair, "view": view[pair], "opinion": opinion,
            })

    result = walk_book(
        bars, daily, {}, float(market["nav"]), score_start, score_end,
        decider=ledger_opinion, fill_model="realistic", on_opinion=on_opinion,
    )
    if result.get("status") != "complete":
        raise SystemExit(f"ledger walk did not finish: {result.get('status')}")
    outcomes = {
        (float(item["decision_ts"]), item["pair"]): item
        for item in result["intents"]
    }
    count = write_history(history_path, opinions, outcomes)
    summary = summarize(result, opinions)
    summary["history"] = str(history_path)
    summary["warmup_bars"] = warmup_bars
    summary["skip_last_bars"] = skip_last
    summary_path = history_path.with_suffix(".summary.json")
    _dump(summary_path, summary)
    print(
        f"opinions {count}  before {summary['before_usd']:.2f}  "
        f"after {summary['after_usd']:.2f}  net {summary['net_usd']:+.2f}  "
        f"roi {summary['accumulated_roi'] * 100:.3f}%"
    )
    print(f"actions {summary['actions']}")
    print(f"outcomes {summary['outcomes']}")
    print(f"closed {len(summary['closed_trades'])}  open {len(summary['open_marks'])}")
    for trade in summary["closed_trades"]:
        print(
            f"  {trade['pair']} net {trade['net_usd']:+.2f}  "
            f"roi {trade['roi_on_notional'] * 100:.3f}%"
        )
    print(f"wrote {history_path}")
    print(f"wrote {summary_path}")


def walk_cmd(out_dir: Path, decisions_path: Path, score_hours: int, skip_last_hours: int) -> None:
    """One causal pass. Stops when the next scored hour has no Grok seats."""
    from tools.grok_paper_walk import common_timestamps, score_window, walk_book

    market = _load(out_dir / "market.json")
    bars = {}
    daily = {}
    now = float(market["asof_unix"])
    for pair in market["pairs_order"]:
        done, _forming = completed_bars(market["pairs"][pair]["hourly"], now, HOURLY_S)
        bars[pair] = done
        daily[pair] = market["pairs"][pair]["daily"]
    stamps = common_timestamps(bars)
    start, end = score_window(stamps, score_hours, skip_last_hours, HOURLY_S)
    decisions = {}
    if decisions_path.is_file():
        raw = _load(decisions_path)
        if raw.get("model") not in (None, "grok-4.7"):
            raise SystemExit("walk decisions must be grok-4.7")
        decisions = raw.get("bars") or {}
    os.environ["HYDRA_BRAIN_JSONL"] = str(out_dir / "walk_brain.jsonl")
    result = walk_book(
        bars, daily, decisions, float(market["nav"]), start, end,
        fill_model="realistic",
    )
    if result["status"] == "need_decision":
        _dump(out_dir / "pending.json", result)
        print(
            f"need decision {result['bar_index']}/{result['bar_count']} "
            f"at {int(result['timestamp'])}"
        )
        print(f"wrote {out_dir / 'pending.json'}")
        raise SystemExit(3)
    _dump(out_dir / "walk_results.json", result)
    print(
        f"before {result['before_usd']:.2f}  after {result['after_usd']:.2f}  "
        f"net {result['net_usd']:+.2f}  roi {result['accumulated_roi'] * 100:.3f}%"
    )
    print(f"closed {len(result['closed_trades'])}  open {len(result['open_marks'])}")
    for trade in result["closed_trades"]:
        print(
            f"  {trade['pair']} net {trade['net_usd']:+.2f}  "
            f"roi {trade['roi_on_notional'] * 100:.3f}%  "
            f"in {trade['entry_price']:.4f} out {trade['exit_price']:.4f}"
        )
    print(f"wrote {out_dir / 'walk_results.json'}")


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description="Grok 4.7 paper emulation cycle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("self-check")
    prep = sub.add_parser("prepare")
    prep.add_argument("--out", default=str(DEFAULT_OUT))
    prep.add_argument("--nav", type=float, default=NAV_DEFAULT)
    app = sub.add_parser("apply")
    app.add_argument("--out", default=str(DEFAULT_OUT))
    app.add_argument("--decisions", default="")
    walk = sub.add_parser("walk")
    walk.add_argument("--out", default=str(DEFAULT_OUT))
    walk.add_argument("--decisions", default="")
    walk.add_argument("--score-hours", type=int, default=24)
    walk.add_argument("--skip-last-hours", type=int, default=6)
    ledger = sub.add_parser("ledger")
    ledger.add_argument("--out", default=str(DEFAULT_OUT))
    ledger.add_argument(
        "--history",
        default=str(ROOT / "research" / "grok_paper_opinions" / "60m_ledger.jsonl"),
    )
    ledger.add_argument("--warmup-bars", type=int, default=240)
    ledger.add_argument("--skip-last", type=int, default=6)
    args = parser.parse_args(list(argv))
    if args.cmd == "self-check":
        self_check()
        return
    out_dir = Path(args.out)
    if args.cmd == "prepare":
        prepare(out_dir, float(args.nav))
        return
    if args.cmd == "ledger":
        ledger_cmd(
            out_dir, Path(args.history), int(args.warmup_bars), int(args.skip_last),
        )
        return
    if args.cmd == "walk":
        decisions = Path(args.decisions) if args.decisions else out_dir / "walk_decisions.json"
        walk_cmd(out_dir, decisions, int(args.score_hours), int(args.skip_last_hours))
        return
    decisions = Path(args.decisions) if args.decisions else out_dir / "decisions.json"
    apply_cycle(out_dir, decisions)


if __name__ == "__main__":
    main(sys.argv[1:])
