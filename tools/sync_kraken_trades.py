"""Bootstrap + incremental sync of Kraken personal trade history into
hydra_kraken_trades.sqlite.

Two modes:

    python -m tools.sync_kraken_trades --bootstrap-from kraken_trades_dump.json
        Populate from a pre-pulled Kraken JSON dump (the file
        `kraken trades-history --type all -o json` produces). Idempotent —
        existing txids are skipped.

    python -m tools.sync_kraken_trades --incremental
        Pull only trades since the most recent time_unix already in the
        store. Calls `kraken trades-history --start <ts>` via WSL, then
        upserts. Idempotent.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

from hydra_kraken_trades import KrakenTradesStore


def _kraken_trades_history_page(start_ts: Optional[float] = None,
                                offset: int = 0) -> dict:
    """One page of Kraken's private trades-history (default 50 per page).

    Uses bash -c with `source ~/.cargo/env` per CLAUDE.md WSL-Kraken
    convention."""
    inner = "source ~/.cargo/env && kraken trades-history --type all -o json"
    if start_ts is not None and start_ts > 0:
        inner += f" --start {start_ts:.6f}"
    if offset > 0:
        inner += f" --offset {offset}"
    cmd = ["wsl", "-d", os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu"), "--", "bash", "-c", inner]
    out = subprocess.check_output(cmd, encoding="utf-8")
    return json.loads(out)


def _iter_kraken_trades_pages(start_ts: Optional[float] = None,
                              start_offset: int = 0,
                              sleep_sec: float = 5.0,
                              max_pages: int = 500,
                              max_retries: int = 5):
    """Yield (offset, target_count, trades_dict) per page from Kraken's
    trades-history. Caller persists incrementally so a mid-stream failure
    doesn't lose progress.

    On Kraken-CLI subprocess failure, exponential backoff up to max_retries
    before raising. Sleep base 5s — Kraken's private endpoints have a
    tighter rate budget than the public OHLC endpoint; the 2s floor in
    CLAUDE.md is for OHLC. trades-history is more expensive per call.

    Stops when (a) page returns empty, (b) cumulative >= target_count from
    page 0, or (c) max_pages hit."""
    offset = start_offset
    target_count: Optional[int] = None
    cumulative = 0
    for page in range(max_pages):
        # Per-page retry loop with exponential backoff.
        last_exc: Optional[Exception] = None
        data: Optional[dict] = None
        for attempt in range(max_retries):
            try:
                data = _kraken_trades_history_page(start_ts=start_ts, offset=offset)
                last_exc = None
                break
            except subprocess.CalledProcessError as e:
                last_exc = e
                wait = sleep_sec * (2 ** attempt)
                print(f"  [KRAKEN-TRADES] offset={offset} attempt {attempt + 1}/{max_retries} "
                      f"failed (rc={e.returncode}); retrying in {wait:.1f}s")
                time.sleep(wait)
        if data is None:
            raise RuntimeError(
                f"Kraken trades-history exhausted retries at offset={offset}: {last_exc}"
            )
        if not isinstance(data, dict):
            return
        if target_count is None:
            target_count = int(data.get("count", 0) or 0)
        page_trades = data.get("trades") or {}
        if not page_trades:
            return
        cumulative += len(page_trades)
        yield offset, target_count, page_trades
        if target_count and cumulative >= target_count:
            return
        offset += len(page_trades)
        time.sleep(sleep_sec)


def bootstrap_from_dump(store: KrakenTradesStore, dump_path: str) -> int:
    """Roll a pre-pulled Kraken JSON dump into the store. Returns inserted count."""
    with open(dump_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    trades = data.get("trades") or {}
    print(f"  [KRAKEN-TRADES] dump: {len(trades)} trades; upserting...")
    n = store.upsert_kraken_trades(trades)
    print(f"  [KRAKEN-TRADES] inserted {n} new rows; total in store: {store.count()}")
    return n


def _stream_pages_into_store(store: KrakenTradesStore,
                             start_ts: Optional[float],
                             start_offset: int = 0,
                             sleep_sec: float = 5.0) -> int:
    """Pull pages and persist each one before continuing. Restartable —
    if anything raises, what's been written stays written."""
    total_inserted = 0
    pages_seen = 0
    for offset, target_count, page_trades in _iter_kraken_trades_pages(
        start_ts=start_ts, start_offset=start_offset, sleep_sec=sleep_sec
    ):
        n = store.upsert_kraken_trades(page_trades)
        total_inserted += n
        pages_seen += 1
        print(f"  [KRAKEN-TRADES] offset={offset} page+{len(page_trades)} "
              f"inserted={n} (cumulative new={total_inserted}); "
              f"store.count()={store.count()}/{target_count}")
    print(f"  [KRAKEN-TRADES] stream done: {pages_seen} pages, "
          f"{total_inserted} new rows inserted; total in store: {store.count()}")
    return total_inserted


def incremental(store: KrakenTradesStore) -> int:
    """Pull-and-merge only trades since the most recent time_unix in store.
    Persists per-page so a mid-stream failure doesn't lose progress."""
    cursor = store.latest_time() or 0.0
    print(f"  [KRAKEN-TRADES] incremental sync since time_unix={cursor:.6f}...")
    return _stream_pages_into_store(store, start_ts=cursor)


def full_pull(store: KrakenTradesStore, start_offset: int = 0) -> int:
    """Exhaustive pull — walks all offsets from `start_offset`. Run once
    at bootstrap against an empty store; thereafter use --incremental.

    Pass start_offset=N to resume from a specific page count after a
    previous failure (e.g., --full-pull --resume-offset 1150)."""
    print(f"  [KRAKEN-TRADES] full historical pull (paginated, "
          f"resume_offset={start_offset})...")
    return _stream_pages_into_store(store, start_ts=None,
                                    start_offset=start_offset)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hydra_kraken_trades.sqlite")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bootstrap-from", metavar="PATH",
                   help="Roll a Kraken trades-history JSON dump into the store")
    g.add_argument("--full-pull", action="store_true",
                   help="Exhaustive paginated pull from offset=0 (one-time bootstrap)")
    g.add_argument("--incremental", action="store_true",
                   help="Pull only trades since the latest time_unix in store")
    ap.add_argument("--resume-offset", type=int, default=0,
                    help="With --full-pull, start from this offset instead of 0")
    args = ap.parse_args()

    store = KrakenTradesStore(args.db)
    if args.bootstrap_from:
        bootstrap_from_dump(store, args.bootstrap_from)
    elif args.full_pull:
        full_pull(store, start_offset=args.resume_offset)
    elif args.incremental:
        incremental(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
