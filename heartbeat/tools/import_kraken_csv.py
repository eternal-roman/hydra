"""Import a Kraken public trade-history CSV into hydra_history.sqlite ohlc.

Kraken's downloadable TimeAndSales dumps are `timestamp,price,volume`
rows (epoch seconds, no aggressor side). Without side they cannot feed
heartbeat's flow features, but they are authoritative deep price/volume
history — streamed here into 1h candles and upserted as source
'kraken_archive' (the same provenance as the existing archive rows;
tier policy keeps them from clobbering nothing and protects them from
lower tiers later). The still-forming final hour of the file is dropped.

Usage:
    python heartbeat/tools/import_kraken_csv.py \
        --csv ".../TimeAndSales_Combined/ZECUSD.csv" --pair ZEC/USD
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

HYDRA_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HYDRA_ROOT))

from hydra_history_store import CandleRow, HistoryStore  # noqa: E402

GRAIN = 3600


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--pair", required=True)
    ap.add_argument("--db", default=str(HYDRA_ROOT / "hydra_history.sqlite"))
    args = ap.parse_args()

    t0 = time.time()
    candles: dict[int, list] = {}
    rows_read = 0
    with open(args.csv, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                ts, price, qty = float(row[0]), float(row[1]), float(row[2])
            except ValueError:
                continue  # header or malformed line
            rows_read += 1
            h = int(ts // GRAIN) * GRAIN
            c = candles.get(h)
            if c is None:
                candles[h] = [price, price, price, price, qty]
            else:
                c[1] = max(c[1], price)
                c[2] = min(c[2], price)
                c[3] = price
                c[4] += qty
    if not candles:
        print("no rows parsed"); return 1
    last_hour = max(candles)
    candles.pop(last_hour, None)  # possibly-partial final hour of the dump

    store = HistoryStore(args.db)
    out = [CandleRow(pair=args.pair, grain_sec=GRAIN, ts=h, open=o, high=hi,
                     low=lo, close=c, volume=v, source="kraken_archive")
           for h, (o, hi, lo, c, v) in sorted(candles.items())]
    written = 0
    for i in range(0, len(out), 5000):
        written += store.upsert_candles(out[i:i + 5000])
    span_d = (max(candles) - min(candles)) / 86400
    print(f"{args.pair}: {rows_read:,} csv trades -> {len(out):,} hourly "
          f"candles ({span_d:.0f} days), {written:,} upserted "
          f"({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
