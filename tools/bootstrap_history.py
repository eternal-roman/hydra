"""One-time bootstrap: Kraken trade archive (zip of TimeAndSales_Combined CSVs)
→ rolled 1h OHLC candles → hydra_history.sqlite (source='kraken_archive').

Usage:
    python -m tools.bootstrap_history --zip ~/Downloads/Kraken_Trading_History.zip \\
        --pairs XBTUSD,ETHUSD,ZECUSD --grain 3600 --out hydra_history.sqlite

Stdlib only. Stream-reads each CSV; never loads trades into RAM.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import time
import zipfile
from typing import Dict, Iterator, List, Optional, Tuple

from hydra_history_store import CandleRow, HistoryStore

# Kraken file name stem → canonical "BASE/QUOTE" form.
_KRAKEN_FILE_TO_CANONICAL: Dict[str, str] = {
    "XBTUSD": "BTC/USD",
    "SOLUSD": "SOL/USD",
    "SOLXBT": "SOL/BTC",
    "ETHUSD": "ETH/USD",
    "ZECUSD": "ZEC/USD",
}


def kraken_pair_to_canonical(filename_stem: str) -> str:
    """Map a Kraken archive filename stem to canonical BASE/QUOTE form."""
    if filename_stem in _KRAKEN_FILE_TO_CANONICAL:
        return _KRAKEN_FILE_TO_CANONICAL[filename_stem]
    raise ValueError(f"unknown Kraken archive pair: {filename_stem}")


def _iter_trades(zf: zipfile.ZipFile, member: str) -> Iterator[Tuple[int, float, float]]:
    """Yield (ts_seconds, price, volume) from a Kraken trade CSV. Streamed."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        for row in csv.reader(text):
            if not row or len(row) < 3:
                continue
            try:
                # Some Kraken archives use float seconds; normalize.
                ts = int(float(row[0]))
                price = float(row[1])
                vol = float(row[2])
            except ValueError:
                continue
            yield ts, price, vol


def _roll_to_candles(
    trades: Iterator[Tuple[int, float, float]],
    grain_sec: int,
    pair: str,
) -> Iterator[CandleRow]:
    """Stream trades → emit completed candles as bucket boundaries cross."""
    bucket_open_ts: Optional[int] = None
    o = h = l = c = 0.0
    v = 0.0
    for ts, price, vol in trades:
        bucket = (ts // grain_sec) * grain_sec
        if bucket_open_ts is None:
            bucket_open_ts = bucket
            o = h = l = c = price
            v = vol
            continue
        if bucket != bucket_open_ts:
            yield CandleRow(pair, grain_sec, bucket_open_ts,
                            o, h, l, c, v, "kraken_archive")
            bucket_open_ts = bucket
            o = h = l = c = price
            v = vol
        else:
            if price > h:
                h = price
            if price < l:
                l = price
            c = price
            v += vol
    # Trailing flush — emit the last bucket.
    if bucket_open_ts is not None:
        yield CandleRow(pair, grain_sec, bucket_open_ts,
                        o, h, l, c, v, "kraken_archive")


def bootstrap_zip(
    zip_path: str,
    out_db: str,
    pairs: List[str],
    grain_sec: int = 3600,
    batch_size: int = 10_000,
) -> Dict[str, int]:
    """Bootstrap one or more pairs from a Kraken trade archive zip.

    Returns dict of {canonical_pair: candles_written}.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(zip_path)
    store = HistoryStore(out_db)
    written: Dict[str, int] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        for kraken_pair in pairs:
            canonical = kraken_pair_to_canonical(kraken_pair)
            member = f"TimeAndSales_Combined/{kraken_pair}.csv"
            if member not in names:
                raise FileNotFoundError(f"{kraken_pair} not in archive")
            print(f"  [BOOTSTRAP] rolling {kraken_pair} -> {canonical} @ {grain_sec}s")
            t0 = time.time()
            buf: List[CandleRow] = []
            n = 0
            for candle in _roll_to_candles(_iter_trades(zf, member), grain_sec, canonical):
                buf.append(candle)
                if len(buf) >= batch_size:
                    n += store.upsert_candles(buf)
                    buf.clear()
            if buf:
                n += store.upsert_candles(buf)
            written[canonical] = n
            elapsed = time.time() - t0
            print(f"  [BOOTSTRAP]   {canonical}: {n} candles in {elapsed:.1f}s")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Bootstrap Hydra history from Kraken trade archive.")
    ap.add_argument("--zip", required=True, help="Path to Kraken_Trading_History.zip")
    ap.add_argument("--pairs", default="XBTUSD,ETHUSD,ZECUSD",
                    help="Comma-separated Kraken pair names (e.g. XBTUSD,ETHUSD,ZECUSD)")
    ap.add_argument("--grain", type=int, default=3600, help="Candle grain in seconds (default 3600)")
    ap.add_argument("--out", default=os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite"),
                    help="Output SQLite path (env: HYDRA_HISTORY_DB)")
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    bootstrap_zip(args.zip, args.out, pairs=pairs, grain_sec=args.grain)


if __name__ == "__main__":
    main()
