"""HYDRA Canonical Historical Store — SQLite-backed OHLC only.

Stdlib-only. Single source of truth for backtest / research candle history
(`meta` + `ohlc`). Not a store for trading decisions or release snapshots.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 2

# Source tier ranking — higher rank wins on conflict.
# kraken_archive is immutable; rest > tape for trailing-edge refresh.
_SOURCE_RANK = {"tape": 1, "kraken_rest": 2, "kraken_archive": 3}


@dataclass(frozen=True)
class CandleRow:
    pair: str
    grain_sec: int
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def __post_init__(self):
        if self.source not in _SOURCE_RANK:
            raise ValueError(f"unknown source tier: {self.source}")


@dataclass(frozen=True)
class Coverage:
    pair: str
    grain_sec: int
    candle_count: int
    first_ts: Optional[int]
    last_ts: Optional[int]
    gap_count: int
    max_gap_sec: int


@dataclass(frozen=True)
class CandleOut:
    pair: str
    grain_sec: int
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlc (
  pair         TEXT    NOT NULL,
  grain_sec    INTEGER NOT NULL,
  ts           INTEGER NOT NULL,
  open         REAL    NOT NULL,
  high         REAL    NOT NULL,
  low          REAL    NOT NULL,
  close        REAL    NOT NULL,
  volume       REAL    NOT NULL,
  source       TEXT    NOT NULL,
  ingested_at  INTEGER NOT NULL,
  PRIMARY KEY (pair, grain_sec, ts)
);
"""


class HistoryStore:
    def __init__(self, path: str = "hydra_history.sqlite"):
        self.path = path
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            # Detect existing DB before applying schema script.
            existing = None
            try:
                cur = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                )
                row = cur.fetchone()
                if row is not None:
                    existing = int(row[0])
            except sqlite3.OperationalError:
                existing = None  # fresh DB, meta table not created yet
            if existing is not None and existing != SCHEMA_VERSION:
                if existing == 1 and SCHEMA_VERSION == 2:
                    # 1 -> 2 was additive; no row backfill required.
                    pass
                else:
                    raise RuntimeError(
                        f"hydra_history_store: schema_version={existing} on disk, "
                        f"code expects {SCHEMA_VERSION}. Run a migration or delete "
                        f"the DB to rebuild from archive."
                    )
            conn.executescript(_SCHEMA)
            # Drop legacy release-gate tables if an old DB still has them.
            # That feature was removed (self-comparison / never-actionable);
            # this DB is raw OHLC only.
            for _orphan in (
                "regression_trade",
                "regression_equity_curve",
                "regression_metrics",
                "regression_run",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {_orphan}")
            # Bump recorded version idempotently.
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    def upsert_candles(self, rows: Iterable[CandleRow]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        now = int(time.time())
        n = 0
        with self._lock, self._conn() as conn:
            for r in rows:
                # Tier-aware insert: only overwrite if incoming source rank
                # >= existing source rank.
                cur = conn.execute(
                    "SELECT source FROM ohlc WHERE pair=? AND grain_sec=? AND ts=?",
                    (r.pair, r.grain_sec, r.ts),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if _SOURCE_RANK[r.source] < _SOURCE_RANK[existing[0]]:
                        continue  # incoming is lower tier — skip
                conn.execute(
                    """INSERT OR REPLACE INTO ohlc
                       (pair, grain_sec, ts, open, high, low, close, volume,
                        source, ingested_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r.pair, r.grain_sec, r.ts, r.open, r.high, r.low,
                     r.close, r.volume, r.source, now),
                )
                n += 1
            conn.commit()
        return n

    def fetch(self, pair: str, grain_sec: int,
              start_ts: int, end_ts: int) -> Iterator[CandleOut]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT pair, grain_sec, ts, open, high, low, close, volume, source
                   FROM ohlc
                   WHERE pair=? AND grain_sec=? AND ts>=? AND ts<=?
                   ORDER BY ts ASC""",
                (pair, grain_sec, start_ts, end_ts),
            ).fetchall()
        yield from (CandleOut(*row) for row in rows)

    def coverage(self, pair: str, grain_sec: int) -> Coverage:
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT COUNT(*), MIN(ts), MAX(ts) FROM ohlc
                   WHERE pair=? AND grain_sec=?""",
                (pair, grain_sec),
            )
            count, first, last = cur.fetchone()
            if count == 0:
                return Coverage(pair, grain_sec, 0, None, None, 0, 0)
            cur = conn.execute(
                """SELECT ts FROM ohlc WHERE pair=? AND grain_sec=?
                   ORDER BY ts ASC""",
                (pair, grain_sec),
            )
            prev = None
            gap_count = 0
            max_gap = 0
            for (ts,) in cur:
                if prev is not None:
                    delta = ts - prev
                    if delta > grain_sec:
                        gap_count += 1
                        if delta > max_gap:
                            max_gap = delta
                prev = ts
        return Coverage(pair, grain_sec, count, first, last, gap_count, max_gap)

    def list_pairs(self) -> List[Tuple[str, int]]:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT DISTINCT pair, grain_sec FROM ohlc ORDER BY pair, grain_sec"
            )
            return [(r[0], r[1]) for r in cur]
