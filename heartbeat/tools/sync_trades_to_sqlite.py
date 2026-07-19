"""Durably mirror the heartbeat parquet trade tape into hydra_history.sqlite.

The parquet store under heartbeat/data/ is a local working cache; the
canonical SQL store is where history must not be lost. This tool upserts
every stored trade into a `trades` table (WAL, INSERT OR IGNORE on
(pair, trade_id) — reruns and overlapping backfills are no-ops), so a
wiped parquet cache costs nothing but a re-export.

The table is additive alongside `ohlc` and never touched by HistoryStore;
owner is this tool. Kraken trade_ids are per-pair monotone, which makes
(pair, trade_id) a natural idempotency key; the rare trade_id=0 rows
(unknown id) fall back to a (pair, ts, price, qty, side) uniqueness guard.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/sync_trades_to_sqlite.py \
        --pairs SOL/USD,BTC/USD,ETH/USD [--db ../hydra_history.sqlite]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))

import pyarrow.parquet as pq  # noqa: E402

from heartbeat.config import load_config  # noqa: E402
from heartbeat.store import Store         # noqa: E402

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  pair        TEXT    NOT NULL,
  trade_id    INTEGER NOT NULL,
  ts          REAL    NOT NULL,
  price       REAL    NOT NULL,
  qty         REAL    NOT NULL,
  side        TEXT    NOT NULL,
  ord_type    TEXT    NOT NULL,
  ingested_at INTEGER NOT NULL,
  PRIMARY KEY (pair, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_pair_ts ON trades (pair, ts);
"""


def sync_pair(con: sqlite3.Connection, store: Store, pair: str,
              tf: str = "1h") -> tuple[int, int]:
    """Stream part files -> INSERT OR IGNORE batches. Returns (seen, new)."""
    d = store.dir_for(pair, tf, "tape")
    now = int(time.time())
    seen = new = 0
    for part in sorted(d.glob("part-*.parquet")):
        t = pq.read_table(part)
        cols = {n: t.column(n).to_pylist() for n in t.schema.names}
        rows = []
        for j in range(t.num_rows):
            tid = int(cols["trade_id"][j])
            if tid == 0:
                # No exchange id: derive a stable surrogate from the tuple
                # so reruns stay idempotent (collision-free in practice for
                # same-pair duplicates, which is all the guard must catch).
                tid = -abs(hash((cols["ts"][j], cols["price"][j],
                                 cols["qty"][j], cols["side"][j]))) or -1
            rows.append((pair, tid, cols["ts"][j], cols["price"][j],
                         cols["qty"][j], cols["side"][j],
                         cols["ord_type"][j], now))
        seen += len(rows)
        cur = con.executemany(
            "INSERT OR IGNORE INTO trades "
            "(pair, trade_id, ts, price, qty, side, ord_type, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        new += cur.rowcount
    con.commit()
    return seen, new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="SOL/USD,BTC/USD,ETH/USD")
    ap.add_argument("--db", default=str(HEARTBEAT_ROOT.parent /
                                        "hydra_history.sqlite"))
    ap.add_argument("--tf", default="1h")
    args = ap.parse_args()

    cfg = load_config(None)
    store = Store(str(HEARTBEAT_ROOT / cfg["store"]["root"]))
    con = sqlite3.connect(args.db, timeout=60.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)

    rc = 0
    for pair in [p.strip() for p in args.pairs.split(",")]:
        t0 = time.time()
        seen, new = sync_pair(con, store, pair, args.tf)
        total, lo, hi = con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM trades WHERE pair=?",
            (pair,)).fetchone()
        span = (hi - lo) / 86400 if lo else 0
        print(f"{pair}: {seen} tape rows -> {new} new, {total} total in db "
              f"({span:.1f} days, {time.time() - t0:.0f}s)", flush=True)
        if seen == 0:
            rc = max(rc, 1)
    con.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
