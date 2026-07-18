"""Cross-check backfilled trade tape against HYDRA's canonical OHLC store.

Aggregates the heartbeat parquet tape into candles with the SAME builder
the posterior uses (candles_from_trades) and diffs them against
hydra_history.sqlite rows for the identical UTC hour buckets. Both sides
are Kraken venue data, so O/H/L/C and volume should agree to numerical
noise; material divergence means one side's data is wrong and the
posterior must not be trusted until resolved.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/verify_tape_vs_sqlite.py --pair SOL/USD \
        --db ../hydra_history.sqlite [--tf 1h] [--json out.json]

Exit codes: 0 = clean, 1 = mismatches above tolerance, 2 = no overlap.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heartbeat.config import load_config          # noqa: E402
from heartbeat.engine.candle import candles_from_trades, tf_seconds  # noqa: E402
from heartbeat.store import Store                  # noqa: E402

# Tolerances: price ends of the hour can differ by one trade when Kraken's
# own bucketing tie-breaks a boundary trade differently; volume likewise.
PRICE_REL_TOL = 0.002       # 0.2% on O/H/L/C
VOL_REL_TOL = 0.02          # 2% on volume
MAX_BAD_FRACTION = 0.01     # >1% of overlapping candles bad => fail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--db", default="../hydra_history.sqlite")
    ap.add_argument("--json", default=None, help="write full report here")
    args = ap.parse_args()

    cfg = load_config(None)
    store = Store(cfg["store"]["root"])
    trades = store.read_tape(args.pair, args.tf)
    if not trades:
        print(f"no tape for {args.pair} {args.tf}")
        return 2
    candles = candles_from_trades(trades, args.tf, include_final=False)
    # first candle of the tape is partial (backfill starts mid-hour): skip it
    candles = candles[1:]
    by_open = {int(c.open_ts): c for c in candles if c.trade_count > 0}

    grain = tf_seconds(args.tf)
    con = sqlite3.connect(args.db)
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume, source FROM ohlc "
        "WHERE pair=? AND grain_sec=? ORDER BY ts", (args.pair, grain)).fetchall()
    # kraken_rest wins over kraken_archive/tape when the same hour exists twice
    pref = {"kraken_rest": 2, "kraken_archive": 1, "tape": 0}
    db_by_open: dict[int, tuple] = {}
    for ts, o, h, l, c, v, src in rows:
        cur = db_by_open.get(int(ts))
        if cur is None or pref.get(src, 0) >= pref.get(cur[5], 0):
            db_by_open[int(ts)] = (o, h, l, c, v, src)

    overlap = sorted(set(by_open) & set(db_by_open))
    if not overlap:
        print(f"no overlapping hours between tape and {args.db}")
        return 2

    bad: list[dict] = []
    max_price_rel = 0.0
    max_vol_rel = 0.0
    vol_rels: list[float] = []
    for ts in overlap:
        tc = by_open[ts]
        o, h, l, c, v, src = db_by_open[ts]
        prices_ok = True
        worst = 0.0
        for a, b in ((tc.open, o), (tc.high, h), (tc.low, l), (tc.close, c)):
            rel = abs(a - b) / max(abs(b), 1e-9)
            worst = max(worst, rel)
            if rel > PRICE_REL_TOL:
                prices_ok = False
        vol_rel = abs(tc.volume - v) / max(v, 1e-9)
        vol_rels.append(vol_rel)
        max_price_rel = max(max_price_rel, worst)
        max_vol_rel = max(max_vol_rel, vol_rel)
        if not prices_ok or vol_rel > VOL_REL_TOL:
            bad.append({"open_ts": ts, "source": src,
                        "tape": [tc.open, tc.high, tc.low, tc.close, tc.volume],
                        "db": [o, h, l, c, v],
                        "worst_price_rel": round(worst, 6),
                        "vol_rel": round(vol_rel, 6)})

    vol_rels.sort()
    med_vol = vol_rels[len(vol_rels) // 2]
    frac_bad = len(bad) / len(overlap)
    report = {
        "pair": args.pair, "tf": args.tf,
        "tape_candles": len(candles), "db_hours": len(db_by_open),
        "overlap_hours": len(overlap),
        "bad_hours": len(bad), "bad_fraction": round(frac_bad, 5),
        "max_price_rel": round(max_price_rel, 6),
        "median_vol_rel": round(med_vol, 6),
        "max_vol_rel": round(max_vol_rel, 6),
        "verdict": "PASS" if frac_bad <= MAX_BAD_FRACTION else "FAIL",
        "worst_examples": sorted(bad, key=lambda b: -b["vol_rel"])[:10],
    }
    print(json.dumps({k: v for k, v in report.items() if k != "worst_examples"},
                     indent=2))
    for b in report["worst_examples"][:5]:
        print("  BAD", b)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"wrote {args.json}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
