"""Bakeoff B (S2) — envelope-protection layer at 1h execution.

Pre-registered in `heartbeat/evidence/ABI_FUNNEL_STOPS_2026-07-18.md` §6 (S2,
F4+F5+F12 with F9's confound as a design constraint). Run EXACTLY as frozen;
FAIL is a successful outcome. Explicitly NOT promotable as "AI exits rescue
bounce entries" — N1 forbids that reading.

Assets: BTC/USD, ETH/USD, SOL/USD at 1h; ZEC/USD 1h pre-registered as
expected-FAIL control (if ZEC PASSES, flag loudly — mechanism story wrong).

Frozen: daily ensemble score = `exit_layer_lab.daily_scores` (verbatim reuse of
the committed 0.4*sma200 + 0.4*ema20x100 + 0.2*don(55/20 close-based) scorer,
long >= 0.6, 210-close warmup) computed on completed UTC days; a 1h bar acts on
the PRIOR completed day's score (`score_at(..., 1)`), so the first 1h close of
a new UTC day is the first to see the newly completed score. Entries = every
10th 1h bar (placebo BY DESIGN — round-2 N1: entry construction is irrelevant).
No price stop; binary long/flat; fees 26 bps/side per entry and per exit.
Folds: calendar years 2016→2026 of an expanding-window series (the score uses
ALL history; folds slice the equity curve at UTC year boundaries; SOL from its
data start).

Arms:
  (a) gate_flip   long only while score >= 0.6; enter at the close of the next
                  every-10th bar while gated long; flatten at the first 1h
                  close after flip down.
  (b) exposure-matched random control — per fold, take arm (a)'s long segments
      (clipped to the fold), shuffle the segment lengths and place them
      non-overlapping at uniformly random positions in the fold (random
      composition of the residual gap), same 2-leg fees per segment; seed 42,
      25 draws, report the mean fold return.  [the beta control]
  (c) inverse gate: long only while score < 0.6 (same 10-bar entry clock),
      exit on flip up — must lose.
  (d) always-in B&H (fees once per full period; fold returns are price ratios).

PROMOTE IFF (each reported separately, BTC/ETH/SOL only; ZEC is the control):
  C1  (a) > (b) pooled (sum of log full-period equity across the 3 assets)
  C2  (a) > (b) on >= 60% of asset-year folds (folds with exposure > 0)
  C3  equity maxDD(a) < 0.7 * maxDD(d) per asset (strict: all 3 assets)
  C4  (c) loses money pooled (sum log equity < 0)
  C5  ZEC fails as predicted (ZEC must NOT satisfy the C1/C2 pattern)

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_envelope_1h.py
Writes: heartbeat/evidence/bakeoffs/envelope_1h.json
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import random
import sys
import time
import zlib
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

import exit_layer_lab as ell                      # noqa: E402  (frozen scorer)
from bounce_geometry_study import candles_from_sqlite  # noqa: E402

DB = str(HYDRA_ROOT / "hydra_history.sqlite")
FEE = 0.0026
GATE = 0.6
CLOCK = 10                                        # every-10th-bar entry clock
SEED, DRAWS = 42, 25
PROMOTABLE = ["BTC/USD", "ETH/USD", "SOL/USD"]
CONTROL = "ZEC/USD"


def year_of(ts):
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).year


def run_gate(candles, scores, inverse=False):
    """Binary long/flat book. Returns (eq_series, long_mask, episodes, n_rt).

    eq_series[k] = equity at close of bar k (fees applied at transition bars);
    long_mask[k] = True when the position accrues the k-1 -> k close return.
    """
    n = len(candles)
    eq = 1.0
    long_ = False
    entry_idx = None
    eq_series = [1.0] * n
    long_mask = [False] * n
    episodes = []
    for k in range(n):
        c = candles[k]
        if long_ and k > 0:
            eq *= c.close / candles[k - 1].close
            long_mask[k] = True
        sc = ell.score_at(scores, c.open_ts, 1)   # prior completed UTC day
        want = (sc is not None) and ((sc < GATE) if inverse else (sc >= GATE))
        if long_ and not want:                    # flatten at this 1h close
            eq *= (1 - FEE)
            episodes.append((entry_idx, k))
            long_ = False
        elif not long_ and want and k % CLOCK == 0 and k < n - 1:
            eq *= (1 - FEE)                       # enter at this 1h close
            long_ = True
            entry_idx = k
        eq_series[k] = eq
    if long_:                                     # mark closed at final close
        eq *= (1 - FEE)
        episodes.append((entry_idx, n - 1))
        eq_series[-1] = eq
    return eq_series, long_mask, episodes, len(episodes)


def max_dd(series):
    peak, mdd = -1e18, 0.0
    for v in series:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return mdd


def random_exposure_fold(candles, f0, f1, seg_lens, rng):
    """One draw: place segments of the given lengths non-overlapping at
    uniformly random positions in bars [f0, f1] (entry at close of start bar,
    exit at close of start+L). Returns fold total return incl. fees."""
    lens = list(seg_lens)
    rng.shuffle(lens)
    span = f1 - f0
    gap = span - sum(lens)
    if gap < 0:                                    # cannot happen: exposure<=span
        lens, gap = [span], 0
    cuts = sorted(rng.uniform(0, gap) for _ in lens)
    total = 1.0
    pos = f0
    prev_cut = 0.0
    for L, cut in zip(lens, cuts):
        pos += int(round(cut - prev_cut))
        prev_cut = cut
        s = min(pos, f1 - L)
        total *= (candles[s + L].close / candles[s].close) * (1 - FEE) ** 2
        pos = s + L
    return total - 1.0


def main() -> int:
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "design": "ABI_FUNNEL_STOPS_2026-07-18.md §6 S2 (frozen)",
              "frozen": {"fee_per_side": FEE, "gate": GATE, "clock": CLOCK,
                         "seed": SEED, "draws": DRAWS,
                         "scorer": "exit_layer_lab.daily_scores (verbatim)"},
              "assets": {}}
    pooled = {"log_a": 0.0, "log_b": 0.0, "log_c": 0.0,
              "folds_a_gt_b": 0, "folds_total": 0}

    for pair in PROMOTABLE + [CONTROL]:
        candles = candles_from_sqlite(DB, pair, 1)
        scores = ell.daily_scores(pair)
        scored = [k for k, c in enumerate(candles)
                  if ell.score_at(scores, c.open_ts, 1) is not None]
        start = scored[0]
        # eval window: first scored bar in/after 2016 (SOL: its own start)
        while year_of(candles[start].open_ts) < 2016:
            start += 1
        eqA, maskA, epsA, nA = run_gate(candles, scores, inverse=False)
        eqC, maskC, epsC, nC = run_gate(candles, scores, inverse=True)

        # fold boundaries: last bar index of each UTC year within [start, end]
        years = sorted({year_of(c.open_ts) for c in candles[start:]})
        last_idx = {}
        for k in range(start, len(candles)):
            last_idx[year_of(candles[k].open_ts)] = k

        def fold_ret(series, y, prev_end):
            return series[last_idx[y]] / series[prev_end] - 1.0

        asset = {"n_candles": len(candles), "eval_start_ts":
                 int(candles[start].open_ts), "n_round_trips_a": nA,
                 "folds": {}, "fold_wins_a_gt_b": 0, "folds_counted": 0}
        prev_end = start
        for y in years:
            f0, f1 = prev_end, last_idx[y]
            if f1 <= f0:
                continue
            ra = fold_ret(eqA, y, f0)
            rc = fold_ret(eqC, y, f0)
            rd = candles[f1].close / candles[f0].close - 1.0
            exp_bars = sum(1 for k in range(f0 + 1, f1 + 1) if maskA[k])
            # arm (a) long segments clipped to this fold
            segs = []
            for e, x in epsA:
                lo, hi = max(e, f0), min(x, f1)
                if hi > lo:
                    segs.append(hi - lo)
            rb = rb_med = rb_geo = None
            if segs:
                # deterministic per (pair, year) sub-seed — zlib.crc32 is
                # stable across processes (builtin hash() is salted)
                rng = random.Random(SEED + zlib.crc32(f"{pair}|{y}".encode()))
                draws = [random_exposure_fold(candles, f0, f1, segs, rng)
                         for _ in range(DRAWS)]
                rb = sum(draws) / len(draws)      # registered: mean of draws
                # supplementary (honesty): the arithmetic mean of draws is
                # Jensen-inflated on volatile tape; median + geometric mean
                # show the FAIL/PASS is not an artifact of that choice
                sd = sorted(draws)
                rb_med = sd[len(sd) // 2]
                rb_geo = math.exp(sum(math.log1p(max(d, -0.9999))
                                      for d in draws) / len(draws)) - 1
            asset["folds"][str(y)] = {
                "a_gate_flip_pct": round(100 * ra, 2),
                "b_exposure_matched_pct": round(100 * rb, 2)
                if rb is not None else None,
                "b_median_draw_pct": round(100 * rb_med, 2)
                if rb_med is not None else None,
                "b_geomean_draw_pct": round(100 * rb_geo, 2)
                if rb_geo is not None else None,
                "c_inverse_pct": round(100 * rc, 2),
                "d_bh_pct": round(100 * rd, 2),
                "exposure_bars": exp_bars, "fold_bars": f1 - f0,
                "exposure_frac": round(exp_bars / (f1 - f0), 3)}
            if rb is not None:
                asset["folds_counted"] += 1
                if ra > rb:
                    asset["fold_wins_a_gt_b"] += 1
                if ra > rb_med:
                    asset["fold_wins_a_gt_b_median"] = \
                        asset.get("fold_wins_a_gt_b_median", 0) + 1
            prev_end = f1

        end = last_idx[years[-1]]
        totA = eqA[end] / eqA[start] - 1.0
        totC = eqC[end] / eqC[start] - 1.0
        totD = (candles[end].close / candles[start].close) * (1 - FEE) ** 2 - 1
        totB, totBmed = 1.0, 1.0
        for y in years:
            f = asset["folds"].get(str(y))
            if f and f["b_exposure_matched_pct"] is not None:
                totB *= 1 + f["b_exposure_matched_pct"] / 100.0
                totBmed *= 1 + f["b_median_draw_pct"] / 100.0
        totB -= 1.0
        totBmed -= 1.0
        ddA = max_dd(eqA[start:end + 1])
        ddD = max_dd([c.close for c in candles[start:end + 1]])
        asset["totals"] = {"a_pct": round(100 * totA, 2),
                           "b_pct": round(100 * totB, 2),
                           "b_median_pct": round(100 * totBmed, 2),
                           "c_pct": round(100 * totC, 2),
                           "d_pct": round(100 * totD, 2)}
        asset["max_dd"] = {"a": round(ddA, 4), "d": round(ddD, 4),
                           "a_lt_0.7d": ddA < 0.7 * ddD}
        report["assets"][pair] = asset
        if pair in PROMOTABLE:
            pooled["log_a"] += math.log1p(totA)
            pooled["log_b"] += math.log1p(totB)
            pooled["log_c"] += math.log1p(totC)
            pooled["folds_a_gt_b"] += asset["fold_wins_a_gt_b"]
            pooled["folds_total"] += asset["folds_counted"]
            pooled["log_b_median"] = pooled.get("log_b_median", 0.0) \
                + math.log1p(totBmed)
            pooled["folds_a_gt_b_median"] = pooled.get(
                "folds_a_gt_b_median", 0) + asset.get(
                "fold_wins_a_gt_b_median", 0)
        print(f"== {pair}: a={asset['totals']['a_pct']}% b={asset['totals']['b_pct']}% "
              f"c={asset['totals']['c_pct']}% d={asset['totals']['d_pct']}% "
              f"ddA={ddA:.3f} ddD={ddD:.3f} rt={nA}")

    # ---- criteria (BTC/ETH/SOL; ZEC = control) ----
    c1 = pooled["log_a"] > pooled["log_b"]
    frac = (pooled["folds_a_gt_b"] / pooled["folds_total"]
            if pooled["folds_total"] else 0.0)
    c2 = frac >= 0.6
    dd_flags = {p: report["assets"][p]["max_dd"]["a_lt_0.7d"]
                for p in PROMOTABLE}
    c3 = all(dd_flags.values())
    c4 = pooled["log_c"] < 0
    z = report["assets"][CONTROL]
    z_frac = (z["fold_wins_a_gt_b"] / z["folds_counted"]
              if z["folds_counted"] else 0.0)
    zec_passes_pattern = (z["totals"]["a_pct"] > z["totals"]["b_pct"]
                          and z_frac >= 0.6)
    c5 = not zec_passes_pattern

    report["criteria"] = {
        "C1_a_gt_b_pooled": {"sum_log_eq_a": round(pooled["log_a"], 4),
                             "sum_log_eq_b": round(pooled["log_b"], 4),
                             "pass": c1},
        "C2_a_gt_b_folds": {"wins": pooled["folds_a_gt_b"],
                            "total": pooled["folds_total"],
                            "frac": round(frac, 3), "threshold": 0.6,
                            "pass": c2},
        "C3_maxdd_a_lt_0.7_d": {"by_asset": dd_flags, "pass": c3},
        "C4_inverse_loses_pooled": {"sum_log_eq_c": round(pooled["log_c"], 4),
                                    "pass": c4},
        "C5_zec_fails_as_predicted": {
            "zec_a_pct": z["totals"]["a_pct"], "zec_b_pct": z["totals"]["b_pct"],
            "zec_fold_frac_a_gt_b": round(z_frac, 3),
            "zec_passes_pattern_FLAG": zec_passes_pattern, "pass": c5},
    }
    report["supplementary_median_control"] = {
        "note": "registered control is mean-of-25-draws; the arithmetic mean "
                "is Jensen-inflated on volatile tape, so the median-draw "
                "comparison is reported to show the verdict is not an "
                "artifact of that choice",
        "sum_log_eq_b_median": round(pooled.get("log_b_median", 0.0), 4),
        "folds_a_gt_b_median": pooled.get("folds_a_gt_b_median", 0),
        "folds_total": pooled["folds_total"]}
    promote = c1 and c2 and c3 and c4 and c5
    report["verdict"] = "PROMOTE" if promote else "FAIL"

    out = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "envelope_1h.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["criteria"], indent=2))
    print("VERDICT:", report["verdict"])
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
