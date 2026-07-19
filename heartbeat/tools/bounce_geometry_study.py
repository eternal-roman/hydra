"""Long-window bounce-GEOMETRY study on archive candles (no posterior).

Question (pre-registered): is the 1h bounce construction profitable in
ANY regime/year before adding flow prediction — and does a trailing exit
(let true reversals run) beat the fee-sized 3.3*ATR label target?

Candle-only: archive dumps carry no aggressor side, so there is no
posterior here — every arm is ALL-setups or ORACLE (perfect foresight,
future data, upper bound). If some year/exit combination shows the ALL
arm near breakeven with a healthy ORACLE ceiling, THAT is the regime
where heartbeat's selection (proven AUC on BTC/ETH) has margin to pay;
if no year clears even at the ORACLE bound, the 1h construction is dead
everywhere and redesign must go finer-grained instead.

Arms per year: entry bounce+1 and bounce+3; exits target3.3 /
trail1.5*ATR / trail2.5*ATR (all with the hard stop below the setup low
and the 200-candle time stop). Fees per side via --fee-bps (26 default).

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bounce_geometry_study.py \
        --pairs BTC/USD,ETH/USD,ZEC/USD [--fee-bps 26]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config       # noqa: E402
from heartbeat.engine.candle import ClosedCandle  # noqa: E402

import paper_bounce_sim as sim                 # noqa: E402


def candles_from_sqlite(db: str, pair: str,
                        bar_hours: int = 1) -> list[ClosedCandle]:
    """1h rows, optionally resampled to bar_hours (UTC-aligned buckets)."""
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM ohlc "
        "WHERE pair=? AND grain_sec=3600 ORDER BY ts", (pair,)).fetchall()
    grain = 3600 * bar_hours
    agg: dict[int, list] = {}
    for ts, o, h, l, c, v in rows:
        b = int(ts) // grain * grain
        cur = agg.get(b)
        if cur is None:
            agg[b] = [o, h, l, c, v]
        else:
            cur[1] = max(cur[1], h)
            cur[2] = min(cur[2], l)
            cur[3] = c
            cur[4] += v
    return [ClosedCandle(open_ts=float(b), close_ts=float(b) + grain,
                         open=o, high=h, low=l, close=c, volume=v,
                         buy_vol=0.0, sell_vol=0.0, trade_count=1, vwap=c)
            for b, (o, h, l, c, v) in sorted(agg.items())]


def year_bounds(candles):
    years = {}
    for c in candles:
        y = _dt.datetime.fromtimestamp(c.open_ts, _dt.UTC).year
        lo, hi = years.get(y, (c.open_ts, c.open_ts))
        years[y] = (min(lo, c.open_ts), max(hi, c.open_ts))
    return dict(sorted(years.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC/USD,ETH/USD,ZEC/USD")
    ap.add_argument("--db", default=str(HYDRA_ROOT / "hydra_history.sqlite"))
    ap.add_argument("--fee-bps", type=float, default=26.0)
    ap.add_argument("--bar-hours", type=int, default=1,
                    help="resample 1h rows to N-hour bars (4 and 24 shrink "
                         "fees relative to ATR)")
    ap.add_argument("--out", default=str(HEARTBEAT_ROOT / "evidence" /
                                         "bounce_geometry_study.json"))
    args = ap.parse_args()
    sim.FEE = args.fee_bps / 10000.0
    cfg = load_config(None)

    exit_arms = [("tgt3.3", None, True, None),
                 ("trail1.5", None, False, 1.5),
                 ("trail2.5", None, False, 2.5)]
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "fee_per_side": sim.FEE, "pairs": {}}
    report["bar_hours"] = args.bar_hours
    for pair in [p.strip() for p in args.pairs.split(",")]:
        candles = candles_from_sqlite(args.db, pair, args.bar_hours)
        if len(candles) < 500:
            print(f"{pair}: only {len(candles)} candles — skipped")
            continue
        setups = sim.causal_setups(candles, cfg)
        oracle = [s for s in setups if s["label"] == "reversal"]
        pr = {"n_candles": len(candles), "n_setups": len(setups),
              "n_labeled_reversal": len(oracle), "years": {}}
        for y, (lo, hi) in year_bounds(candles).items():
            yr = {}
            for off in (1, 3):
                for ex_name, t_x, tgt, trail in exit_arms:
                    for tag, pool in (("all", setups), ("ORACLE", oracle)):
                        tr = sim.simulate(candles, {}, pool, off, None, t_x,
                                          tgt, lo_ts=lo, hi_ts=hi,
                                          trail_mult=trail)
                        s = sim.stats(tr)
                        if s["n"]:
                            yr[f"b{off}.{tag}.{ex_name}"] = s
            pr["years"][y] = yr
        report["pairs"][pair] = pr
        # compact console: per-year ALL b1 arms
        print(f"\n== {pair}: {len(setups)} setups over {len(candles)} candles")
        for y, yr in pr["years"].items():
            row = []
            for ex_name, *_ in exit_arms:
                s = yr.get(f"b1.all.{ex_name}")
                o = yr.get(f"b1.ORACLE.{ex_name}")
                row.append(f"{ex_name}: {s['total_ret_pct'] if s else '-'}%"
                           f" (orc {o['total_ret_pct'] if o else '-'}%,"
                           f" n={s['n'] if s else 0})")
            print(f"  {y}: " + " | ".join(row))
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
