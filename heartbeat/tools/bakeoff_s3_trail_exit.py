"""Bakeoff — S3 trail-exit gate (pre-registered).

Registration: heartbeat/evidence/bakeoffs/s3_trail_exit_REGISTRATION.md
(committed before this runner executed). Everything upstream of the exit
is identical to the promoted S3 bakeoff (imported, not re-derived); only
the exit rule varies, in one unified simulator (X1 incumbent re-run
inside it). Sequencing delta vs bakeoff_s3_exit_policy.py, declared in
the registration: the one-position lock carries ACROSS yearly folds.

Arms on each fold's gated (train-p75) pool, entry b1 close, 26 bps/side:
  X1 incumbent  stop close<L0 (fill close) / tgt 3.3*ATR / 200-bar horizon
  X4a trail     stop close<L0 while unarmed; ARM at close>=L0+3.3*ATR;
                armed: exit close<MA9; horizon cap
  X5 routed     premium_atr > train-median cut -> X4a rule, else X1 rule
  T_K           blind time exit at close of entry+K bars, K in {5,10,20,30,50}

Criteria (BTC+ETH pooled; ZEC reporting-only), from the registration:
C1 expectancy (+0.5pp vs X1), C2 fold consistency (>=60%), C3
info-over-exposure vs T_K* (BOTH candidates), C4 tail bounds, C5 LOYO
verdict stability. Pre-committed decision rule: PASS -> basis flip;
fail C1/C2/C3/C5 with C4 clean -> shadow arm; C4 violation -> drop.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_s3_trail_exit.py
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
from bakeoff_s3_daily_classifier import (               # noqa: E402
    DB, ASSETS, FEATURES, YEARS, MIN_TRAIN,
    shock_flags, fresh_low_days, build_features,
    fit_logistic, standardizer, zrow, year_ts)

GATE_ASSETS = ["BTC/USD", "ETH/USD"]     # ZEC/USD reported, never gates
TIME_GRID = [5, 10, 20, 30, 50]
CANDIDATES = ["X4a_trail", "X5_routed"]
ARMS = ["X1_incumbent"] + CANDIDATES + [f"T_{k}" for k in TIME_GRID]
MA_P = 9
REENTRY_WINDOW_S = 15 * 86400
OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "s3_trail_exit.json"


def trail_exit(candles, closes, s, e, k, armed):
    """One bar of the X4a rule. Returns (exit_px, reason, armed)."""
    c = candles[k]
    L0 = s["low_px"]
    arm_line = L0 + sim.TARGET_ATR * s["atr"]
    if not armed and c.close >= arm_line:
        armed = True
    ma9 = sum(closes[k - MA_P + 1:k + 1]) / MA_P if k >= MA_P - 1 else None
    if not armed and c.close < L0:
        return c.close, "stop_close", armed
    if armed and ma9 is not None and c.close < ma9:
        return c.close, "trail", armed
    if k - s["low_idx"] > sim.HORIZON:
        return c.close, "time", armed
    return None, None, armed


def x1_exit(candles, s, k):
    c = candles[k]
    L0 = s["low_px"]
    tgt = L0 + sim.TARGET_ATR * s["atr"]
    if c.close < L0:
        return c.close, "stop_close"
    if c.high >= tgt:
        return tgt, "target"
    if k - s["low_idx"] > sim.HORIZON:
        return c.close, "time"
    return None, None


def simulate_arm_locked(candles, closes, folds, arm):
    """Correct cross-fold lock: single pass over fold-ordered entries."""
    trades, in_pos = [], -1
    for lo, hi, gated, prem_cut in folds:
        for s in gated:
            e = sim.entry_index(candles, s, 1)
            if e is None:
                continue
            ts = candles[e].open_ts
            if ts < lo or ts > hi or e <= in_pos:
                continue
            entry = candles[e].close
            prem = (entry - s["low_px"]) / s["atr"]
            if arm == "X5_routed":
                mode = "X4a" if prem > prem_cut else "X1"
            elif arm == "X4a_trail":
                mode = "X4a"
            elif arm == "X1_incumbent":
                mode = "X1"
            else:
                mode = "T"
            K = int(arm.split("_")[1]) if mode == "T" else None
            exit_px = reason = k_exit = None
            armed = False
            for k in range(e + 1, len(candles)):
                if mode == "X1":
                    exit_px, reason = x1_exit(candles, s, k)
                elif mode == "X4a":
                    exit_px, reason, armed = trail_exit(
                        candles, closes, s, e, k, armed)
                else:
                    if k - e >= K:
                        exit_px, reason = candles[k].close, "time"
                if exit_px is not None:
                    k_exit = k
                    break
            if exit_px is None:
                k_exit, exit_px, reason = (len(candles) - 1,
                                           candles[-1].close, "eod")
            j = min(e + 60, len(candles) - 1)
            hi60 = max(closes[max(0, e - 60):e]) if e else entry
            trades.append({
                "entry_ts": ts, "exit_ts": candles[k_exit].open_ts,
                "ret": exit_px / entry - 1.0 - 2 * sim.FEE,
                "reason": reason, "hold": k_exit - e,
                "year": _dt.datetime.fromtimestamp(ts, _dt.UTC).year,
                "prem": prem, "breadth": s["x"]["breadth"],
                "breadth_train_med": s["_breadth_med"],
                "leg_depth": s["low_px"] / hi60 - 1,
                "fwd60": candles[j].close / entry - 1.0 - 2 * sim.FEE})
            in_pos = k_exit
    return trades


def ext_stats(trades) -> dict:
    out = sim.stats(trades)
    if trades:
        rets = sorted(t["ret"] for t in trades)
        out["worst_trade_pct"] = round(rets[0] * 100, 3)
        out["share_le_m15"] = round(
            sum(1 for r in rets if r <= -0.15) / len(rets), 3)
        out["median_hold"] = statistics.median(t["hold"] for t in trades)
        out["exit_reasons"] = {}
        for t in trades:
            out["exit_reasons"][t["reason"]] = \
                out["exit_reasons"].get(t["reason"], 0) + 1
    return out


def build_folds(pair, cfg, low_days_by_pair, candles_by_pair):
    candles = candles_by_pair[pair]
    setups = sim.causal_setups(candles, cfg)
    flags = shock_flags(candles)
    build_features(candles, setups, flags, low_days_by_pair)
    labeled = [s for s in setups if s["label"] is not None]
    folds, folds_meta = [], []
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
        gated_train = [s for s in train if prob(s) >= thr_hi]
        prems = []
        for s in gated_train:
            e = sim.entry_index(candles, s, 1)
            if e is not None:
                prems.append((candles[e].close - s["low_px"]) / s["atr"])
        prem_cut = statistics.median(prems) if prems else 1.0
        breadth_med = statistics.median(s["x"]["breadth"] for s in train)
        gated = [s for s in setups if prob(s) >= thr_hi]
        for s in gated:
            s["_breadth_med"] = breadth_med
        folds.append((cut, year_ts(y + 1) - 1, gated, prem_cut))
        folds_meta.append({"year": y, "train_n": len(train),
                           "thr_p75": round(thr_hi, 4),
                           "prem_cut": round(prem_cut, 3),
                           "breadth_train_med": round(breadth_med, 3)})
    return candles, folds, folds_meta, len(setups), len(labeled)


def fold_totals(trades):
    out = {}
    for t in trades:
        out[t["year"]] = out.get(t["year"], 0.0) + t["ret"]
    return out


def pooled_stats(all_trades, arm, drop_year=None):
    tr = [t for p in GATE_ASSETS for t in all_trades[p][arm]
          if drop_year is None or t["year"] != drop_year]
    return ext_stats(tr), tr


def evaluate_criteria(all_trades, drop_year=None):
    """C1-C4 verdict on pooled BTC+ETH (optionally leaving a year out).
    Returns dict per candidate + winner (highest passing pooled avg)."""
    x1_stats, _ = pooled_stats(all_trades, "X1_incumbent", drop_year)
    x1_avg = x1_stats.get("avg_ret_pct")
    res = {"X1_pooled": x1_stats, "candidates": {}}
    passing = []
    for arm in CANDIDATES:
        st_, tr = pooled_stats(all_trades, arm, drop_year)
        avg = st_.get("avg_ret_pct")
        v = {"pooled": st_}
        v["C1_expectancy"] = {
            "value": avg, "threshold": f">= X1 ({x1_avg}) + 0.5",
            "pass": avg is not None and x1_avg is not None
            and avg >= x1_avg + 0.5}
        wins = tot = 0
        detail = {}
        for p in GATE_ASSETS:
            ft_a = fold_totals(all_trades[p][arm])
            ft_b = fold_totals(all_trades[p]["X1_incumbent"])
            for y in sorted(set(ft_a) | set(ft_b)):
                if drop_year is not None and y == drop_year:
                    continue
                tot += 1
                win = ft_a.get(y, 0.0) >= ft_b.get(y, 0.0)
                wins += win
                detail[f"{p.split('/')[0]}_{y}"] = {
                    "arm": round(ft_a.get(y, 0.0) * 100, 2),
                    "x1": round(ft_b.get(y, 0.0) * 100, 2), "win": win}
        v["C2_fold_consistency"] = {
            "wins": wins, "folds": tot,
            "share": round(wins / tot, 3) if tot else None,
            "threshold": ">= 0.60", "detail": detail,
            "pass": tot > 0 and wins / tot >= 0.60}
        med = st_.get("median_hold")
        kstar = min(TIME_GRID, key=lambda k: (abs(k - med), k)) \
            if med is not None else None
        ctrl_stats, _ = pooled_stats(all_trades, f"T_{kstar}", drop_year) \
            if kstar else ({}, [])
        ctrl_avg = ctrl_stats.get("avg_ret_pct")
        v["C3_info_over_exposure"] = {
            "median_hold": med, "k_star": kstar, "control_avg_pct": ctrl_avg,
            "threshold": f">= T_{kstar} ({ctrl_avg}) + 0.5",
            "pass": avg is not None and ctrl_avg is not None
            and avg >= ctrl_avg + 0.5}
        v["C4_tail"] = {
            "worst_pct": st_.get("worst_trade_pct"),
            "x1_worst_pct": x1_stats.get("worst_trade_pct"),
            "share_le_m15": st_.get("share_le_m15"),
            "x1_share_le_m15": x1_stats.get("share_le_m15"),
            "threshold": "worst >= X1_worst - 10pp AND "
                         "share<=-15% <= X1 + 0.10",
            "pass": (st_.get("worst_trade_pct") is not None
                     and st_["worst_trade_pct"]
                     >= x1_stats["worst_trade_pct"] - 10.0
                     and st_["share_le_m15"]
                     <= x1_stats["share_le_m15"] + 0.10)}
        v["pass"] = all(v[c]["pass"] for c in
                        ("C1_expectancy", "C2_fold_consistency",
                         "C3_info_over_exposure", "C4_tail"))
        if v["pass"]:
            passing.append(arm)
        res["candidates"][arm] = v
    winner = None
    if passing:
        winner = max(passing, key=lambda a:
                     res["candidates"][a]["pooled"]["avg_ret_pct"])
    res["passing"], res["winner"] = passing, winner
    return res


def secondary_measurements(all_trades):
    """Registered secondary reports (never gate). Computed on X1 trades."""
    tr = [t for p in GATE_ASSETS for t in all_trades[p]["X1_incumbent"]]
    out = {}
    lo = [t for t in tr if t["breadth"] <= t["breadth_train_med"]]
    hi = [t for t in tr if t["breadth"] > t["breadth_train_med"]]
    out["breadth_horizon_inversion"] = {
        g: {"n": len(v),
            "avg_fwd60_pct": round(100 * statistics.mean(
                t["fwd60"] for t in v), 2) if v else None,
            "stop_rate": round(sum(1 for t in v
                                   if t["reason"] == "stop_close")
                               / len(v), 3) if v else None}
        for g, v in (("low_breadth", lo), ("high_breadth", hi))}
    med_depth = statistics.median(t["leg_depth"] for t in tr)
    deep = [t for t in tr if t["leg_depth"] <= med_depth]
    shallow = [t for t in tr if t["leg_depth"] > med_depth]
    out["leg_depth_split"] = {
        g: {"n": len(v),
            "stop_rate": round(sum(1 for t in v
                                   if t["reason"] == "stop_close")
                               / len(v), 3) if v else None,
            "avg_fwd60_pct": round(100 * statistics.mean(
                t["fwd60"] for t in v), 2) if v else None}
        for g, v in (("deep", deep), ("shallow", shallow))}
    re_, fresh = [], []
    for p in GATE_ASSETS:
        trs = sorted(all_trades[p]["X1_incumbent"],
                     key=lambda t: t["entry_ts"])
        for i, t in enumerate(trs):
            prior = any(q["reason"] == "stop_close"
                        and 0 <= t["entry_ts"] - q["exit_ts"]
                        <= REENTRY_WINDOW_S for q in trs[:i])
            (re_ if prior else fresh).append(t)
    out["post_stop_reentry"] = {
        g: {"n": len(v),
            "avg_ret_pct": round(100 * statistics.mean(
                t["ret"] for t in v), 2) if v else None,
            "avg_fwd60_pct": round(100 * statistics.mean(
                t["fwd60"] for t in v), 2) if v else None}
        for g, v in (("reentry_le15d", re_), ("fresh", fresh))}
    out["note"] = ("registered secondary measurements - reported only, "
                   "never gate; reentry underpowered by design disclosure")
    return out


def main() -> int:
    cfg = load_config(None)
    candles_by_pair = {p: candles_from_sqlite(DB, p, 24) for p in ASSETS}
    low_days_by_pair = {p: fresh_low_days(c) for p, c in
                        candles_by_pair.items()}

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
              "registration": "heartbeat/evidence/bakeoffs/"
                              "s3_trail_exit_REGISTRATION.md",
              "fee_per_side": sim.FEE, "assets": {}, "verdict": {},
              "caveats": [
                  "Registration NOT blind: funnel exploratory numbers for "
                  "both candidates were seen (disclosed in the "
                  "registration; deciders C3/C5 and train-derived premium "
                  "cuts were unseen).",
                  "One-position lock carries across yearly folds "
                  "(declared delta vs bakeoff_s3_exit_policy.py; X1 "
                  "baseline re-run under the same convention).",
                  "MA9 trail uses the setup's own 9-bar MA; no new "
                  "fitted parameter. X5 premium cut is per-fold "
                  "train-pool median.",
                  "ZEC/USD reported only; trail already FAILED there in "
                  "the funnel (-2.05%/trade).",
                  "POWER: ~23-28 trades/asset over the full archive; the "
                  "shadow window remains final authority."]}

    all_trades = {}
    for pair in ASSETS:
        candles, folds, folds_meta, n_setups, n_labeled = build_folds(
            pair, cfg, low_days_by_pair, candles_by_pair)
        closes = [c.close for c in candles]
        trades = {arm: simulate_arm_locked(candles, closes, folds, arm)
                  for arm in ARMS}
        all_trades[pair] = trades
        report["assets"][pair] = {
            "n_setups": n_setups, "n_labeled": n_labeled,
            "folds_run": folds_meta,
            "arms": {arm: ext_stats(tr) for arm, tr in trades.items()}}
        print(f"\n== {pair} ({n_setups} setups)")
        for arm in ARMS:
            print(f"  {arm:>12}: {report['assets'][pair]['arms'][arm]}")

    # ---- full-sample verdict (C1-C4) -----------------------------------
    verdict = evaluate_criteria(all_trades)

    # ---- C5 LOYO verdict stability -------------------------------------
    years = sorted({t["year"] for p in GATE_ASSETS
                    for t in all_trades[p]["X1_incumbent"]})
    winner = verdict["winner"]
    loyo = {}
    stable = True
    for y in years:
        r = evaluate_criteria(all_trades, drop_year=y)
        loyo[str(y)] = {"passing": r["passing"], "winner": r["winner"]}
        if winner is not None:
            # adopted arm must remain passing, or verdict degrade to
            # no-adopt; a flip to the other candidate is instability
            if r["winner"] is not None and r["winner"] != winner:
                stable = False
            if winner not in r["passing"] and r["passing"]:
                stable = False
    verdict["C5_loyo"] = {"years": loyo, "stable": stable,
                          "threshold": "winner never flips; may only "
                                       "degrade to no-adopt"}

    # ---- pre-committed decision rule -----------------------------------
    decision = {}
    for arm in CANDIDATES:
        v = verdict["candidates"][arm]
        if arm == winner and stable:
            decision[arm] = "ADOPT_BASIS"
        elif v["C4_tail"]["pass"]:
            decision[arm] = "SHADOW_ARM_ONLY"
        else:
            decision[arm] = "DROP"
    # a passing non-winner (or unstable winner) with clean C4 -> shadow
    verdict["decision"] = decision
    verdict["ADOPT"] = winner if (winner and stable) else \
        "X1_incumbent (no stable candidate)"
    report["verdict"] = verdict
    report["secondary"] = secondary_measurements(all_trades)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print("\npooled BTC+ETH:")
    for arm in ARMS:
        st_, _ = pooled_stats(all_trades, arm)
        print(f"  {arm:>12}: {st_}")
    print(f"\nVERDICT: {verdict['ADOPT']}  decision={decision}  "
          f"loyo_stable={stable}")
    for arm in CANDIDATES:
        v = verdict["candidates"][arm]
        print(f"  {arm}: " + " ".join(
            f"{c}={'PASS' if v[c]['pass'] else 'FAIL'}"
            for c in ("C1_expectancy", "C2_fold_consistency",
                      "C3_info_over_exposure", "C4_tail")))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
