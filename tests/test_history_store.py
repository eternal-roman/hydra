import os
import sqlite3
import tempfile
import pytest
from hydra_history_store import HistoryStore, SCHEMA_VERSION, CandleRow, Coverage


def test_init_creates_schema(tmp_path):
    db = tmp_path / "h.sqlite"
    store = HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = [r[0] for r in rows]
    assert "ohlc" in names
    assert "meta" in names
    assert not any(n.startswith("regression_") for n in names)


def test_init_drops_orphan_regression_tables(tmp_path):
    """Legacy release-gate tables must not survive open — raw OHLC + meta only."""
    db = tmp_path / "h.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema_version', '2');
            CREATE TABLE ohlc(
              pair TEXT NOT NULL, grain_sec INTEGER NOT NULL, ts INTEGER NOT NULL,
              open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
              close REAL NOT NULL, volume REAL NOT NULL,
              source TEXT NOT NULL, ingested_at INTEGER NOT NULL,
              PRIMARY KEY(pair, grain_sec, ts));
            INSERT INTO ohlc VALUES(
              'BTC/USD', 3600, 1700000000, 1,1,1,1,1, 'kraken_archive', 0);
            CREATE TABLE regression_run(
              run_id TEXT PRIMARY KEY, hydra_version TEXT NOT NULL,
              git_sha TEXT NOT NULL, param_hash TEXT NOT NULL,
              pair TEXT NOT NULL, grain_sec INTEGER NOT NULL,
              spec_json TEXT NOT NULL, override_reason TEXT,
              created_at INTEGER NOT NULL);
            INSERT INTO regression_run VALUES(
              'dead', '9.9.9-timing-probe', 'abc', '', 'BTC/USD', 3600,
              '{}', NULL, 1);
            CREATE TABLE regression_metrics(
              run_id TEXT NOT NULL, fold_idx INTEGER NOT NULL,
              metric TEXT NOT NULL, value REAL NOT NULL,
              PRIMARY KEY(run_id, fold_idx, metric));
            CREATE TABLE regression_trade(
              run_id TEXT NOT NULL, trade_idx INTEGER NOT NULL,
              ts INTEGER NOT NULL, side TEXT NOT NULL, price REAL NOT NULL,
              size REAL NOT NULL, fee REAL NOT NULL, regime TEXT, reason TEXT,
              PRIMARY KEY(run_id, trade_idx));
            CREATE TABLE regression_equity_curve(
              run_id TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL,
              PRIMARY KEY(run_id, ts));
            """
        )
        conn.commit()
    HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "ohlc" in names
        assert "meta" in names
        assert not any(n.startswith("regression_") for n in names)
        assert conn.execute("SELECT COUNT(*) FROM ohlc").fetchone()[0] == 1


def test_schema_version_recorded(tmp_path):
    db = tmp_path / "h.sqlite"
    HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert v is not None
    assert int(v[0]) == SCHEMA_VERSION


def test_upsert_and_fetch(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                  open=10.0, high=11.0, low=9.0, close=10.5, volume=100.0,
                  source="kraken_archive"),
        CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_003_600,
                  open=10.5, high=12.0, low=10.0, close=11.5, volume=200.0,
                  source="kraken_archive"),
    ]
    n = store.upsert_candles(rows)
    assert n == 2
    fetched = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_003_600))
    assert len(fetched) == 2
    assert fetched[0].close == 10.5
    assert fetched[1].close == 11.5


def test_archive_tier_immutable(tmp_path):
    """tape/rest writes must NOT overwrite kraken_archive rows."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    archive_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                            open=10.0, high=11.0, low=9.0, close=10.5,
                            volume=100.0, source="kraken_archive")
    store.upsert_candles([archive_row])
    tape_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=99.0, high=99.0, low=99.0, close=99.0,
                         volume=99.0, source="tape")
    store.upsert_candles([tape_row])
    [got] = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_000_000))
    assert got.close == 10.5  # archive preserved


def test_rest_overwrites_tape(tmp_path):
    """kraken_rest is more authoritative than tape for trailing window."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    tape_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
                         source="tape")
    store.upsert_candles([tape_row])
    rest_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=2.0, high=2.0, low=2.0, close=2.0, volume=2.0,
                         source="kraken_rest")
    store.upsert_candles([rest_row])
    [got] = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_000_000))
    assert got.close == 2.0



def test_coverage_empty(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cov = store.coverage("BTC/USD", 3600)
    assert cov.candle_count == 0
    assert cov.first_ts is None and cov.last_ts is None


def test_coverage_with_gap(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow("BTC/USD", 3600, 1_700_000_000 + i*3600,
                  10, 11, 9, 10, 1, "kraken_archive")
        for i in range(3)  # ts +0, +3600, +7200
    ]
    # Skip a candle at +10800, write +14400
    rows.append(CandleRow("BTC/USD", 3600, 1_700_000_000 + 4*3600,
                          10, 11, 9, 10, 1, "kraken_archive"))
    store.upsert_candles(rows)
    cov = store.coverage("BTC/USD", 3600)
    assert cov.candle_count == 4
    assert cov.gap_count == 1
    assert cov.max_gap_sec == 7200


def test_list_pairs_returns_distinct_sorted(tmp_path):
    """list_pairs powers DATASET pane coverage rows; must be distinct + sorted."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    store.upsert_candles([
        CandleRow("BTC/USD", 3600, 1_700_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
        CandleRow("BTC/USD", 3600, 1_700_003_600, 1, 1, 1, 1, 1, "kraken_archive"),
        CandleRow("BTC/USD",  900, 1_700_000_000, 1, 1, 1, 1, 1, "tape"),
        CandleRow("SOL/USD", 3600, 1_700_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
    ])
    pairs = store.list_pairs()
    # Distinct (pair, grain_sec); sorted by pair then grain_sec.
    assert pairs == [("BTC/USD", 900), ("BTC/USD", 3600), ("SOL/USD", 3600)]


def test_list_pairs_empty(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    assert store.list_pairs() == []
