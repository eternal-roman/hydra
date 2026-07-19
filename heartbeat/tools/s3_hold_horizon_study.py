"""S3 hold-horizon study — per-coin optimal hold with confidence bounds.

Question (user-directed, 2026-07-19): if the S3 classifier selects the
right leg for continuation, what is the per-coin optimal-but-
high-confidence hold period — precisely, not via blind K=20/K=50 — and
how often is the selection actually right at each K? Real money rides
on this, so every rate ships with its confidence interval and its
leave-one-year-out stability, and the known biases are stated.

Upstream is IDENTICAL to the promoted S3 bakeoff + exit-policy gate
(imported): daily bars, walk-forward yearly folds, frozen 6-feature
logistic, gate = train-p75, entry b1 close, 26 bps/side.

Measurements per asset (BTC/ETH primary; ZEC reported — classifier
gate failed, stays non-tradable unless separately re-gated):

1. PER-ENTRY forward net-return curves r_k, k=1..60 daily bars, two
   families: pure hold (mark at close of entry+k) and stop-composed
   (close<L0 disaster stop first — the adopted X1 stop — else exit at
   close of entry+k). Entries taken independently (overlap disclosed:
   clustered entries double-count shared market moves at large k).
2. Hit-rate vs K table with Wilson 95% CIs, exact x/n counts — the
   direct answer to "is it right every time at K=20/50?" (it is not;
   see the JSON).
3. Optimal-hold distribution: per entry argmax_k of the stop-composed
   curve; median/IQR/histogram per asset.
4. K* selection: argmax over K_GRID of the bootstrap 2.5th-percentile
   of mean net return (stop-composed, per-entry, seed=7, 10k draws) —
   maximizing a LOWER confidence bound, not the point estimate.
   Disclosed bias: K* is selected on the full sample (no third
   walk-forward layer is possible at n~20-30/asset); stability is
   probed by re-running the selection leave-one-year-out.
5. SEQUENCED (one-position) arm P&L at each K in K_GRID for
   comparability with the exit-policy gate's T_K controls.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/s3_hold_horizon_study.py
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import random
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

K_MAX = 60
K_GRID = [3, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60]
BOOT_N, BOOT_SEED = 10_000, 7
OUT = HEARTBEAT_ROOT.parent / "research" / "data" / "s3" / "s3_hold_horizon.json"


def wilson95(x: int, n: int) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.959964
    p = x / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(center - half, 4), round(center + half, 4))


def boot_mean_lb(rets: list[float], rng: random.Random,
                 q: float = 0.025) -> float | None:
    """Bootstrap q-quantile of the mean (percentile method)."""
    if not rets:
        return None
    n = len(rets)
    means = sorted(sum(rng.choice(rets) for _ in range(n)) / n
                   for _ in range(BOOT_N))
    return means[int(q * BOOT_N)]


def gated_entries(pair: str, cfg, low_days_by_pair, candles_by_pair):
    """Walk-forward gated entries: (entry_idx, setup, year). Identical
    classifier path to the exit-policy gate."""
    candles = candles_by_pair[pair]
    setups = sim.causal_setups(candles, cfg)
    flags = shock_flags(candles)
    build_features(candles, setups, flags, low_days_by_pair)
    labeled = [s for s in setups if s["label"] is not None]
    out = []
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
        lo, hi = cut, year_ts(y + 1) - 1
        for s in setups:
            if prob(s) < thr_hi:
                continue
            e = sim.entry_index(candles, s, 1)
            if e is None:
                continue
            ts = candles[e].open_ts
            if ts < lo or ts > hi:
                continue
            out.append((e, s, y))
    return candles, out


def forward_curves(candles, entries):
    """Per-entry net-return curves for k=1..K_MAX, pure and
    stop-composed (close<L0 exits at that close; curve freezes there)."""
    rows = []
    for e, s, y in entries:
        entry = candles[e].close
        L0 = s["low_px"]
        pure, stopped = [], []
        stop_ret = None
        for k in range(1, K_MAX + 1):
            j = e + k
            if j >= len(candles):
                break
            c = candles[j]
            r = c.close / entry - 1.0 - 2 * sim.FEE
            pure.append(r)
            if stop_ret is None and c.close < L0:
                stop_ret = r          # stop fills at this close; frozen after
            stopped.append(stop_ret if stop_ret is not None else r)
        if pure:
            rows.append({"entry_idx": e, "year": y, "entry_ts": candles[e].open_ts,
                         "pure": pure, "stopped": stopped,
                         "stopped_out": stop_ret is not None})
    return rows


def curve_table(rows, family: str) -> dict:
    """Per-K stats over entries whose curve reaches K (truncation stated)."""
    out = {}
    for K in K_GRID:
        rets = [r[family][K - 1] for r in rows if len(r[family]) >= K]
        if not rets:
            out[str(K)] = {"n": 0}
            continue
        x = sum(1 for r in rets if r > 0)
        lo, hi = wilson95(x, len(rets))
        out[str(K)] = {"n": len(rets), "pos": x,
                       "hit_rate": round(x / len(rets), 3),
                       "wilson95": [lo, hi],
                       "avg_ret_pct": round(statistics.mean(rets) * 100, 3),
                       "median_ret_pct": round(statistics.median(rets) * 100, 3),
                       "p10_pct": round(sim.pct(sorted(rets), 0.10) * 100, 3),
                       "worst_pct": round(min(rets) * 100, 3)}
    return out


def kstar_select(rows, rng) -> dict:
    """K* = argmax over K_GRID of bootstrap 2.5% LB of mean stop-composed
    return; only Ks reached by >= 80% of entries are eligible (truncation
    guard). Returns selection + per-K LBs."""
    n_all = len(rows)
    lbs = {}
    for K in K_GRID:
        rets = [r["stopped"][K - 1] for r in rows if len(r["stopped"]) >= K]
        if n_all == 0 or len(rets) < 0.8 * n_all:
            continue
        lb = boot_mean_lb(rets, rng)
        lbs[str(K)] = round(lb * 100, 3) if lb is not None else None
    if not lbs:
        return {"k_star": None, "boot_lb_by_k_pct": {}}
    k_star = max(lbs, key=lambda k: lbs[k])
    return {"k_star": int(k_star), "boot_lb_by_k_pct": lbs,
            "k_star_lb_pct": lbs[k_star]}


def main() -> int:
    cfg = load_config(None)
    candles_by_pair = {p: candles_from_sqlite(DB, p, 24) for p in ASSETS}
    low_days_by_pair = {p: fresh_low_days(c) for p, c in candles_by_pair.items()}
    rng = random.Random(BOOT_SEED)

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "design_note": __doc__.split("Usage")[0],
              "fee_per_side": sim.FEE, "k_max": K_MAX, "k_grid": K_GRID,
              "assets": {}}

    for pair in ASSETS:
        candles, entries = gated_entries(pair, cfg, low_days_by_pair,
                                         candles_by_pair)
        rows = forward_curves(candles, entries)
        res = {"n_gated_entries": len(rows),
               "n_stopped_out": sum(1 for r in rows if r["stopped_out"]),
               "per_entry_pure": curve_table(rows, "pure"),
               "per_entry_stopped": curve_table(rows, "stopped")}

        # optimal-hold distribution (stop-composed argmax)
        opts = []
        for r in rows:
            best_k = max(range(len(r["stopped"])),
                         key=lambda i: r["stopped"][i]) + 1
            opts.append(best_k)
        if opts:
            res["optimal_hold"] = {
                "median": statistics.median(opts),
                "iqr": [sim.pct(sorted(opts), 0.25), sim.pct(sorted(opts), 0.75)],
                "histogram": {f"{a}-{b}": sum(1 for o in opts if a <= o <= b)
                              for a, b in [(1, 5), (6, 10), (11, 20), (21, 30),
                                           (31, 45), (46, 60)]}}

        # K* with LOYO stability
        sel = kstar_select(rows, rng)
        loyo = {}
        for y in sorted({r["year"] for r in rows}):
            sub = [r for r in rows if r["year"] != y]
            loyo[str(y)] = kstar_select(sub, rng).get("k_star")
        sel["loyo_k_star"] = loyo
        vals = [v for v in loyo.values() if v is not None]
        sel["loyo_stable"] = (len(set(vals)) == 1) if vals else None
        res["k_star_selection"] = sel

        # sequenced one-position P&L at each K (stop-composed), for
        # comparability with the exit-policy gate's T_K controls
        seq = {}
        for K in K_GRID:
            trades, in_pos = [], -1
            for e, s, y in entries:
                if e <= in_pos:
                    continue
                entry, L0 = candles[e].close, s["low_px"]
                exit_px = k_exit = None
                for j in range(e + 1, min(e + K + 1, len(candles))):
                    if candles[j].close < L0:
                        exit_px, k_exit = candles[j].close, j
                        break
                    if j == e + K:
                        exit_px, k_exit = candles[j].close, j
                if exit_px is None:
                    k_exit, exit_px = len(candles) - 1, candles[-1].close
                trades.append({"ret": exit_px / entry - 1.0 - 2 * sim.FEE,
                               "reason": "hold", "hold": k_exit - e,
                               "entry_ts": candles[e].open_ts})
                in_pos = k_exit
            st = sim.stats(trades)
            if trades:
                rets = sorted(t["ret"] for t in trades)
                st["worst_trade_pct"] = round(rets[0] * 100, 3)
            seq[str(K)] = st
        res["sequenced_stop_composed"] = seq

        report["assets"][pair] = res
        print(f"\n== {pair}: {len(rows)} gated entries "
              f"({res['n_stopped_out']} hit the L0 close-stop)")
        print(f"  optimal-hold: {res.get('optimal_hold')}")
        print(f"  K*: {sel.get('k_star')} (boot 2.5% LB {sel.get('k_star_lb_pct')}%) "
              f"LOYO {sel['loyo_k_star']} stable={sel['loyo_stable']}")
        for K in K_GRID:
            t = res["per_entry_stopped"][str(K)]
            if t.get("n"):
                print(f"    K={K:>2}: n={t['n']:>2} hit {t['pos']}/{t['n']}"
                      f"={t['hit_rate']:.3f} CI{t['wilson95']} "
                      f"avg {t['avg_ret_pct']:+.2f}% med {t['median_ret_pct']:+.2f}% "
                      f"p10 {t['p10_pct']:+.2f}% worst {t['worst_pct']:+.2f}%")

    OUT.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
