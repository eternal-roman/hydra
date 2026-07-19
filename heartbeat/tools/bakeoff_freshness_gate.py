"""Bakeoff 2 — S1: capitulation-freshness gate (pre-registered, round 1).

Frozen design (heartbeat/evidence/ABI_FUNNEL_2026-07-18.md §6, S1):
  * daily bars (resampled 1h sqlite); BTC/USD + ZEC/USD primary,
    ETH/USD pre-registered as expected-FAIL control;
  * setups from paper_bounce_sim.causal_setups on daily candles;
  * gate (parameter-free, FROZEN): setup's shock_recency <= 2 bars —
    bars since the last daily |return| > 2*stdev(previous 20 returns),
    evaluated at the SETUP low bar (its own return counts), cap 10;
  * arms: all-setups (ungated), fresh-gated (recency <= 2), stale-only
    (recency > 2, inverse control), ORACLE ceiling (label == reversal);
  * entry b1 (close of bounce+1), exit target 3.3*ATR / touch-stop at
    setup low (fill min(close, L0)) / 200-bar horizon, 26 bps/side;
  * folds: calendar years 2016..2025 evaluated independently (no
    training — the gate is parameter-free); pooled = one simulation over
    the whole 2016..2025 window (clean position sequencing).

PROMOTE IFF (per primary asset): fresh-gated total return > 0 on >= 60%
of folds AND (fresh-gated - ungated) > +1%/trade pooled AND stale-only
worse than ungated. ETH is expected to FAIL — if ETH passes all three,
the mechanism story is wrong and that is flagged.

Fold-denominator note (registration silent, both reported): strict =
all 10 calendar years (a no-trade year cannot have total > 0, counts as
fail); active = years where the ungated arm produced >= 1 trade.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_freshness_gate.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config                    # noqa: E402

import paper_bounce_sim as sim                              # noqa: E402
from bounce_geometry_study import candles_from_sqlite       # noqa: E402
from bakeoff_s3_daily_classifier import (shock_flags,       # noqa: E402
                                         recency_at, year_ts)

DB = str(HYDRA_ROOT / "hydra_history.sqlite")
PRIMARY = ["BTC/USD", "ZEC/USD"]
CONTROL = "ETH/USD"
YEARS = list(range(2016, 2026))
FRESH_MAX = 2                                # frozen, no sweeps
OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "freshness_gate.json"


def run_asset(pair: str, cfg) -> dict:
    candles = candles_from_sqlite(DB, pair, 24)
    setups = sim.causal_setups(candles, cfg)
    flags = shock_flags(candles)
    for s in setups:
        s["recency"] = recency_at(flags, s["low_idx"])
    pools = {"ungated": setups,
             "fresh": [s for s in setups if s["recency"] <= FRESH_MAX],
             "stale": [s for s in setups if s["recency"] > FRESH_MAX],
             "oracle": [s for s in setups if s["label"] == "reversal"]}

    res = {"n_daily_candles": len(candles), "n_setups": len(setups),
           "n_fresh": len(pools["fresh"]), "n_stale": len(pools["stale"]),
           "folds": {}, "pooled": {}}
    for y in YEARS:
        lo, hi = year_ts(y), year_ts(y + 1) - 1
        res["folds"][y] = {
            arm: sim.stats(sim.simulate(candles, {}, pool, 1, None, None,
                                        True, lo_ts=lo, hi_ts=hi))
            for arm, pool in pools.items()}
    lo, hi = year_ts(YEARS[0]), year_ts(YEARS[-1] + 1) - 1
    for arm, pool in pools.items():
        res["pooled"][arm] = sim.stats(
            sim.simulate(candles, {}, pool, 1, None, None, True,
                         lo_ts=lo, hi_ts=hi))
    return res


def asset_verdict(res: dict) -> dict:
    """Apply the three registered criteria to one asset's results."""
    folds = res["folds"]
    pos = sum(1 for y in YEARS
              if folds[y]["fresh"].get("n", 0) > 0
              and folds[y]["fresh"]["total_ret_pct"] > 0)
    active = [y for y in YEARS if folds[y]["ungated"].get("n", 0) > 0]
    frac_strict = pos / len(YEARS)
    frac_active = pos / len(active) if active else 0.0
    c1 = frac_strict >= 0.60
    c1_active = frac_active >= 0.60

    def avg(arm):
        s = res["pooled"][arm]
        return s.get("avg_ret_pct") if s.get("n") else None

    fresh_avg, ungated_avg, stale_avg = avg("fresh"), avg("ungated"), avg("stale")
    delta = (fresh_avg - ungated_avg
             if fresh_avg is not None and ungated_avg is not None else None)
    c2 = delta is not None and delta > 1.0
    c3 = (stale_avg is not None and ungated_avg is not None
          and stale_avg < ungated_avg)
    return {
        "criterion_1_fresh_total_pos_folds": {
            "positive_folds": pos, "n_folds_strict": len(YEARS),
            "n_folds_active": len(active),
            "frac_strict": round(frac_strict, 3),
            "frac_active": round(frac_active, 3),
            "threshold": ">= 0.60", "pass_strict": c1, "pass_active": c1_active},
        "criterion_2_fresh_minus_ungated_per_trade": {
            "fresh_avg_pct": fresh_avg, "ungated_avg_pct": ungated_avg,
            "delta_pct_per_trade": round(delta, 3) if delta is not None else None,
            "threshold": "> +1.0 %/trade pooled", "pass": c2},
        "criterion_3_stale_worse_than_ungated": {
            "stale_avg_pct": stale_avg, "ungated_avg_pct": ungated_avg,
            "threshold": "stale avg/trade < ungated avg/trade", "pass": c3},
        "PASS_ALL": bool(c1 and c2 and c3),
        "PASS_ALL_active_denominator": bool(c1_active and c2 and c3)}


def main() -> int:
    cfg = load_config(None)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "design": {
                  "registration": "heartbeat/evidence/ABI_FUNNEL_2026-07-18.md §6 S1",
                  "bars": "daily (resampled from 1h sqlite)",
                  "gate": f"setup shock_recency <= {FRESH_MAX} bars; shock = "
                          "|daily return| > 2*stdev(prev 20 returns), at the "
                          "setup low bar, cap 10 — frozen, parameter-free",
                  "pnl": "entry b1 close, exit tgt 3.3*ATR / touch-stop at L0 "
                         "(fill min(close,L0)) / 200-bar horizon, 26 bps/side",
                  "folds": "calendar years 2016..2025, no training; pooled = "
                           "single simulation over the full window"},
              "fee_per_side": sim.FEE,
              "assets": {}, "verdict": {}, "caveats": [
                  "ZEC/USD daily data starts 2016-10-29 — the 2016 fold has "
                  "almost no history (warmup) and generates few/no setups.",
                  "ZEC 1h gap 2026-01-01..2026-04-20 is OUTSIDE the 2016..2025 "
                  "fold range and does not affect this bakeoff.",
                  "Fold-denominator ambiguity resolved by reporting both "
                  "strict (10 years) and active (years with ungated trades)."]}

    for pair in PRIMARY + [CONTROL]:
        r = run_asset(pair, cfg)
        r["verdict"] = asset_verdict(r)
        report["assets"][pair] = r
        print(f"\n== {pair}: {r['n_setups']} setups "
              f"({r['n_fresh']} fresh / {r['n_stale']} stale)")
        for y in YEARS:
            row = r["folds"][y]
            cells = []
            for arm in ("ungated", "fresh", "stale", "oracle"):
                s = row[arm]
                cells.append(f"{arm} n={s.get('n', 0)} "
                             f"tot={s.get('total_ret_pct', '-')}")
            print(f"  {y}: " + " | ".join(cells))
        for arm in ("ungated", "fresh", "stale", "oracle"):
            print(f"  pooled {arm:>8}: {r['pooled'][arm]}")
        v = r["verdict"]
        print(f"  verdict: c1 strict {v['criterion_1_fresh_total_pos_folds']['pass_strict']}"
              f" (active {v['criterion_1_fresh_total_pos_folds']['pass_active']})"
              f" c2 {v['criterion_2_fresh_minus_ungated_per_trade']['pass']}"
              f" c3 {v['criterion_3_stale_worse_than_ungated']['pass']}"
              f" -> PASS_ALL={v['PASS_ALL']}")

    both_primary = all(report["assets"][p]["verdict"]["PASS_ALL"] for p in PRIMARY)
    eth_passes = report["assets"][CONTROL]["verdict"]["PASS_ALL"]
    report["verdict"] = {
        "primary_assets": PRIMARY,
        "primary_pass": {p: report["assets"][p]["verdict"]["PASS_ALL"]
                         for p in PRIMARY},
        "PROMOTE": bool(both_primary and not eth_passes),
        "eth_control_expected_fail": True,
        "eth_control_passed": eth_passes,
        "mechanism_flag": ("ETH CONTROL PASSED — mechanism story is wrong, "
                           "return to funnel" if eth_passes else None)}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(f"\nVERDICT: {'PROMOTE' if report['verdict']['PROMOTE'] else 'FAIL'} "
          f"| primary {report['verdict']['primary_pass']} "
          f"| ETH control passed = {eth_passes}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
