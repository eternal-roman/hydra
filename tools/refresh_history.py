"""Daily REST refresh of the trailing window for the canonical OHLC store.

Walks each (pair, grain_sec) currently present, fills any multi-page gap
between coverage().last_ts and now via KrakenCLI.ohlc_paged(), then calls
KrakenCLI.ohlc() for the trailing window. Tier policy in HistoryStore
prevents overwrites of kraken_archive rows.

Usage:
    python -m tools.refresh_history [--db hydra_history.sqlite]
"""
from __future__ import annotations

import argparse
import os
import time as _time
from typing import Any, List, Optional

from hydra_history_store import CandleRow, HistoryStore


def refresh_pair(store: HistoryStore, pair: str, grain_sec: int,
                 cli: Optional[Any] = None) -> int:
    """Refresh one (pair, grain_sec) combination. cli is injectable for tests."""
    if cli is None:
        from hydra_kraken_cli import KrakenCLI
        cli = KrakenCLI
    rows = cli.ohlc(pair, interval=grain_sec // 60) or []
    now = int(_time.time())
    out: List[CandleRow] = []
    for r in rows:
        ts = int(float(r.get("timestamp", 0)))
        if ts <= 0:
            continue
        if ts + grain_sec > now:
            # Kraken's last OHLC row is the still-forming candle. Freezing
            # it poisons the store: the row is final under the tier policy
            # and never revisited (trade-tape audit found frozen rows with
            # volume ~10x low). Skip; the completed candle lands next run.
            continue
        out.append(CandleRow(
            pair=pair, grain_sec=grain_sec, ts=ts,
            open=float(r.get("open", 0)), high=float(r.get("high", 0)),
            low=float(r.get("low", 0)), close=float(r.get("close", 0)),
            volume=float(r.get("volume", 0)),
            source="kraken_rest",
        ))
    return store.upsert_candles(out)


def fill_gaps_for_pair(store: HistoryStore, pair: str, grain_sec: int,
                       cli: Optional[Any] = None, max_pages: int = 200,
                       sleep_sec: float = 2.5) -> int:
    """Walk Kraken REST `since` cursor forward until coverage reaches now.

    Idempotent across reruns: tier-policy in HistoryStore prevents archive
    rows from being overwritten. Sleeps sleep_sec between page calls to
    respect Kraken's 2s REST rate-limit floor (default 2.5s).

    Returns the total number of rows upserted.
    """
    if cli is None:
        from hydra_kraken_cli import KrakenCLI
        cli = KrakenCLI
    cov = store.coverage(pair, grain_sec)
    if cov.last_ts is None:
        return 0
    now = int(_time.time())
    if (now - cov.last_ts) < grain_sec * 2:
        return 0
    interval_min = grain_sec // 60
    cursor = int(cov.last_ts)
    total_written = 0
    unfillable_warned = False
    for _ in range(max_pages):
        candles, last_cursor = cli.ohlc_paged(pair, interval=interval_min, since=cursor)
        if not candles:
            break
        # Detect Kraken's "trailing window only" REST behavior: if the FIRST
        # returned candle is more than grain_sec*2 ahead of `since`, Kraken
        # didn't honor the cursor — it just gave us the trailing 720 candles.
        # Deep gaps (older than ~30 days at 1h) cannot be filled via REST OHLC.
        first_ts = int(float(candles[0].get("timestamp", 0)))
        if (not unfillable_warned and first_ts > cursor + grain_sec * 2):
            print(f"  [REFRESH] {pair} {grain_sec}s: unfillable gap from "
                  f"{cursor} to {first_ts} ({(first_ts - cursor) // 3600}h). "
                  f"Kraken REST OHLC doesn't paginate deep history; "
                  f"gap will persist until next Kraken trade-archive "
                  f"bootstrap or live tape capture covers it.")
            unfillable_warned = True
        rows: List[CandleRow] = []
        for r in candles:
            ts = int(float(r.get("timestamp", 0)))
            if ts <= 0:
                continue
            if ts + grain_sec > now:
                continue  # forming candle — same poisoning as refresh_pair
            rows.append(CandleRow(
                pair=pair, grain_sec=grain_sec, ts=ts,
                open=float(r.get("open", 0)), high=float(r.get("high", 0)),
                low=float(r.get("low", 0)), close=float(r.get("close", 0)),
                volume=float(r.get("volume", 0)),
                source="kraken_rest",
            ))
        if rows:
            total_written += store.upsert_candles(rows)
        if last_cursor <= cursor:
            # No forward progress — stop to avoid infinite loop.
            break
        cursor = last_cursor
        if cursor >= now:
            break
        _time.sleep(sleep_sec)
    return total_written


def refresh_all(db_path: str = "hydra_history.sqlite") -> int:
    store = HistoryStore(db_path)
    total = 0
    for pair, grain_sec in store.list_pairs():
        gaps = fill_gaps_for_pair(store, pair, grain_sec)
        if gaps:
            print(f"  [REFRESH] {pair} {grain_sec}s gap-fill: {gaps} rows")
            total += gaps
        n = refresh_pair(store, pair, grain_sec)
        print(f"  [REFRESH] {pair} {grain_sec}s trailing: {n} rows")
        total += n
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite"),
                    help="SQLite path (env: HYDRA_HISTORY_DB)")
    args = ap.parse_args()
    refresh_all(args.db)


if __name__ == "__main__":
    main()
