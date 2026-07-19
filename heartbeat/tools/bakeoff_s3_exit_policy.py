"""Bakeoff — S3 exit-policy gate (pre-registered follow-up to S3 PROMOTE).

Registration: heartbeat/evidence/bakeoffs/s3_exit_policy_REGISTRATION.md
(committed before this runner executed). Everything upstream of the exit
is identical to bakeoff_s3_daily_classifier (imported, not re-derived);
ONLY the exit rule varies, and every arm — including the incumbent —
runs through the single unified simulator below so arm deltas cannot
come from harness mismatches.

Arms on each fold's gated (train-p75) pool, entry b1 close, 26 bps/side:
  X0 incumbent   touch-stop L0 (fill min(close,L0)) / tgt 3.3*ATR / horizon
  X1 close_stop  stop on close<L0 (fill close) / tgt 3.3*ATR / horizon
  X2 flip        exit at close when daily ensemble < 0.6; horizon cap
  X3 hybrid      exit at close when close<L0 OR ensemble<0.6; horizon cap
  T_K            blind time exit at close of entry+K bars, K in {5,10,20,30,50}

Verdict criteria (BTC+ETH pooled; ZEC reporting-only) are computed
exactly as registered: C1 expectancy (+0.5pp vs X0), C2 fold consistency
(>=60%), C3 information-over-exposure vs T_K* (X2/X3 only; K* = grid
element nearest the arm's pooled median hold, ties -> lower K), C4 tail
bound (worst trade within 10pp of X0's; share of <=-15% trades within
10pp of X0's).

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_s3_exit_policy.py
"""

from __future__ import annotations

import datetime as _dt
import json
import statistics
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config                # noqa: E402
from heartbeat.engine.posterior import sigmoid          # noqa: E402

import paper_bounce_sim as sim                          # noqa: E402
from bounce_geometry_study import candles_from_sqlite   # noqa: E402
from exit_layer_lab import daily_scores, score_at       # noqa: E402
from bakeoff_s3_daily_classifier import (               # noqa: E402
    DB, ASSETS, FEATURES, YEARS, MIN_TRAIN,
    shock_flags, fresh_low_days, build_features,
    fit_logistic, standardizer, zrow, year_ts)

GATE_ASSETS = ["BTC/USD", "ETH/USD"]     # ZEC/USD reported, never gates
TIME_GRID = [5, 10, 20, 30, 50]
CANDIDATES = ["X1_close_stop", "X2_flip", "X3_hybrid"]
ARMS = ["X0_incumbent"] + CANDIDATES + [f"T_{k}" for k in TIME_GRID]
OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "s3_exit_policy.json"


def simulate_exit(candles, pool, arm, scores, lo_ts, hi_ts) -> list[dict]:
    """Unified simulator: sim.simulate sequencing/fee/entry conventions,
    exit rule per arm. Priority within a bar mirrors the incumbent
    (stop, then target, then signal/time), stated in the registration."""
    trades, in_pos = [], -1
    for s in pool:
        e = sim.entry_index(candles, s, 1)
        if e is None:
            continue
        ts = candles[e].open_ts
        if (lo_ts is not None and ts < lo_ts) or (hi_ts is not None and ts > hi_ts):
            continue
        if e <= in_pos:
            continue
        entry = candles[e].close
        L0 = s["low_px"]
        tgt = L0 + sim.TARGET_ATR * s["atr"]
        exit_px = reason = k_exit = None
        for k in range(e + 1, len(candles)):
            c = candles[k]
            if arm == "X0_incumbent":
                if c.low < L0:
                    exit_px, reason = min(c.close, L0), "stop"
                elif c.high >= tgt:
                    exit_px, reason = tgt, "target"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            elif arm == "X1_close_stop":
                if c.close < L0:
                    exit_px, reason = c.close, "stop_close"
                elif c.high >= tgt:
                    exit_px, reason = tgt, "target"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            elif arm == "X2_flip":
                sc = score_at(scores, c.open_ts, 24)
                if sc is not None and sc < 0.6:
                    exit_px, reason = c.close, "flip"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            elif arm == "X3_hybrid":
                sc = score_at(scores, c.open_ts, 24)
                if c.close < L0:
                    exit_px, reason = c.close, "stop_close"
                elif sc is not None and sc < 0.6:
                    exit_px, reason = c.close, "flip"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            else:                               # T_K blind time control
                K = int(arm.split("_")[1])
                if k - e >= K:
                    exit_px, reason = c.close, "time"
            if exit_px is not None:
                k_exit = k
                break
        if exit_px is None:
            k_exit, exit_px, reason = len(candles) - 1, candles[-1].close, "eod"
        trades.append({"entry_ts": ts, "ret": exit_px / entry - 1.0 - 2 * sim.FEE,
                       "reason": reason, "hold": k_exit - e,
                       "year": _dt.datetime.fromtimestamp(ts, _dt.UTC).year})
        in_pos = k_exit
    return trades


def ext_stats(trades) -> dict:
    """sim.stats plus the registered tail metrics."""
    out = sim.stats(trades)
    if trades:
        rets = sorted(t["ret"] for t in trades)
        out["worst_trade_pct"] = round(rets[0] * 100, 3)
        out["p10_trade_pct"] = round(sim.pct(rets, 0.10) * 100, 3)
        out["share_le_m15"] = round(sum(1 for r in rets if r <= -0.15) / len(rets), 3)
        out["median_hold"] = statistics.median(t["hold"] for t in trades)
    return out


def run_asset(pair: str, cfg, low_days_by_pair, candles_by_pair) -> dict:
    candles = candles_by_pair[pair]
    setups = sim.causal_setups(candles, cfg)
    flags = shock_flags(candles)
    build_features(candles, setups, flags, low_days_by_pair)
    labeled = [s for s in setups if s["label"] is not None]
    scores = daily_scores(pair)

    trades = {arm: [] for arm in ARMS}
    folds_meta = []
    for y in YEARS:
        cut = year_ts(y)
        train = [s for s in labeled
                 if s["resolve_ts"] is not None and s["resolve_ts"] < cut]
        ys = [1 if s["label"] == "reversal" else 0 for s in train]
        if len(train) < MIN_TRAIN[pair] or len(set(ys)) < 2:
            continue
        mu, sd = standardizer([[s["x"][f] for f in FEATURES] for s in train])
        X = [zrow([s["x"][f] for f in FEATURES], mu, sd) for s in train]
        b0, w = fit_logistic(X, ys)

        def prob(s):
            return sigmoid(b0 + sum(wj * xj for wj, xj in
                                    zip(w, zrow([s["x"][f] for f in FEATURES],
                                                mu, sd))))

        thr_hi = sim.pct(sorted(prob(s) for s in train), 0.75)
        gated = [s for s in setups if prob(s) >= thr_hi]
        lo, hi = cut, year_ts(y + 1) - 1
        for arm in ARMS:
            trades[arm] += simulate_exit(candles, gated, arm, scores, lo, hi)
        folds_meta.append({"year": y, "train_n": len(train),
                           "thr_p75": round(thr_hi, 4)})

    res = {"n_setups": len(setups), "n_labeled": len(labeled),
           "folds_run": folds_meta,
           "arms": {arm: ext_stats(tr) for arm, tr in trades.items()}}
    res["_trades"] = trades
    return res


def fold_totals(trades_by_arm, arm) -> dict[int, float]:
    out = {}
    for t in trades_by_arm[arm]:
        out[t["year"]] = out.get(t["year"], 0.0) + t["ret"]
    return out


def main() -> int:
    cfg = load_config(None)
    candles_by_pair = {p: candles_from_sqlite(DB, p, 24) for p in ASSETS}
    low_days_by_pair = {p: fresh_low_days(c) for p, c in candles_by_pair.items()}

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "registration": "heartbeat/evidence/bakeoffs/"
                              "s3_exit_policy_REGISTRATION.md",
              "fee_per_side": sim.FEE, "assets": {}, "verdict": {},
              "caveats": [
                  "Registration was NOT blind: pooled exploratory numbers for "
                  "X1/X2 analogues were seen before criteria were frozen "
                  "(disclosed in the registration; the deciding measurements "
                  "— fold consistency, T_K controls, tail bounds, X3 — were "
                  "unseen).",
                  "Same-bar close fills on all close-decided exits, uniform "
                  "across arms including the incumbent's stop fill.",
                  "Yearly fold pools simulate independently; late-year "
                  "entries hold past Jan 1 (existing convention).",
                  "ZEC/USD reported only; its 2026 fold spans the known 1h "
                  "gap 2026-01-01..2026-04-20.",
                  "POWER: gated pools carry ~19-30 trades/asset over the "
                  "full archive; the shadow window remains the final "
                  "authority on any adopted exit."]}

    all_trades = {}
    for pair in ASSETS:
        r = run_asset(pair, cfg, low_days_by_pair, candles_by_pair)
        all_trades[pair] = r.pop("_trades")
        report["assets"][pair] = r
        print(f"\n== {pair} ({r['n_setups']} setups)")
        for arm in ARMS:
            print(f"  {arm:>14}: {r['arms'][arm]}")

    # ---- pooled BTC+ETH arm stats -----------------------------------------
    pooled = {arm: ext_stats(sum((all_trades[p][arm] for p in GATE_ASSETS), []))
              for arm in ARMS}
    report["pooled_btc_eth"] = pooled

    # ---- registered criteria ----------------------------------------------
    def pooled_avg(arm):
        s = pooled[arm]
        return s.get("avg_ret_pct") if s.get("n") else None

    x0_avg = pooled_avg("X0_incumbent")
    x0 = pooled["X0_incumbent"]
    verdict = {"X0_pooled_avg_pct": x0_avg, "candidates": {}}
    passing = []
    for arm in CANDIDATES:
        v = {"pooled_avg_pct": pooled_avg(arm)}
        # C1 expectancy
        v["C1_expectancy"] = {"value": v["pooled_avg_pct"],
                              "threshold": f">= X0 ({x0_avg}) + 0.5",
                              "pass": v["pooled_avg_pct"] is not None
                              and v["pooled_avg_pct"] >= x0_avg + 0.5}
        # C2 fold consistency over (asset, year) folds with >=1 trade either arm
        wins = tot = 0
        fold_detail = {}
        for p in GATE_ASSETS:
            ft_arm = fold_totals(all_trades[p], arm)
            ft_x0 = fold_totals(all_trades[p], "X0_incumbent")
            for y in sorted(set(ft_arm) | set(ft_x0)):
                tot += 1
                win = ft_arm.get(y, 0.0) >= ft_x0.get(y, 0.0)
                wins += win
                fold_detail[f"{p.split('/')[0]}_{y}"] = {
                    "arm": round(ft_arm.get(y, 0.0) * 100, 2),
                    "x0": round(ft_x0.get(y, 0.0) * 100, 2), "win": win}
        v["C2_fold_consistency"] = {"wins": wins, "folds": tot,
                                    "share": round(wins / tot, 3) if tot else None,
                                    "threshold": ">= 0.60",
                                    "detail": fold_detail,
                                    "pass": tot > 0 and wins / tot >= 0.60}
        # C3 information-over-exposure (X2/X3 only)
        if arm in ("X2_flip", "X3_hybrid"):
            med = pooled[arm].get("median_hold")
            kstar = min(TIME_GRID, key=lambda k: (abs(k - med), k))
            ctrl_avg = pooled_avg(f"T_{kstar}")
            v["C3_info_over_exposure"] = {
                "median_hold": med, "k_star": kstar,
                "control_avg_pct": ctrl_avg,
                "threshold": f">= T_{kstar} ({ctrl_avg}) + 0.5",
                "pass": v["pooled_avg_pct"] is not None and ctrl_avg is not None
                and v["pooled_avg_pct"] >= ctrl_avg + 0.5}
        # C4 tail bound
        v["C4_tail"] = {
            "worst_pct": pooled[arm].get("worst_trade_pct"),
            "x0_worst_pct": x0.get("worst_trade_pct"),
            "share_le_m15": pooled[arm].get("share_le_m15"),
            "x0_share_le_m15": x0.get("share_le_m15"),
            "threshold": "worst >= X0_worst - 10pp AND share<=-15% <= X0 + 0.10",
            "pass": (pooled[arm].get("worst_trade_pct") is not None
                     and pooled[arm]["worst_trade_pct"]
                     >= x0["worst_trade_pct"] - 10.0
                     and pooled[arm]["share_le_m15"]
                     <= x0["share_le_m15"] + 0.10)}
        needed = ["C1_expectancy", "C2_fold_consistency", "C4_tail"]
        if arm in ("X2_flip", "X3_hybrid"):
            needed.append("C3_info_over_exposure")
        v["pass"] = all(v[c]["pass"] for c in needed)
        if v["pass"]:
            passing.append(arm)
        verdict["candidates"][arm] = v

    winner = max(passing, key=pooled_avg) if passing else None
    verdict["passing"] = passing
    verdict["ADOPT"] = winner or "X0_incumbent (no candidate passed)"
    report["verdict"] = verdict

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print("\npooled BTC+ETH:")
    for arm in ARMS:
        print(f"  {arm:>14}: {pooled[arm]}")
    print(f"\nVERDICT: adopt {verdict['ADOPT']} (passing: {passing})")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
