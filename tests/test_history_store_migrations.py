import sqlite3
from hydra_history_store import HistoryStore, SCHEMA_VERSION


def test_existing_db_with_lower_schema_version_raises(tmp_path):
    """Until v2 ships, opening a DB tagged < SCHEMA_VERSION must explicit-fail
    rather than silently corrupt."""
    db = tmp_path / "h.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version', '0')")
        conn.commit()
    try:
        HistoryStore(str(db))
    except RuntimeError as e:
        assert "schema_version=0" in str(e)
        return
    raise AssertionError("expected RuntimeError")


def test_v1_db_upgrades_to_v2_preserving_rows(tmp_path):
    """A pre-v2 (schema_version=1) DB on disk must silently upgrade to v2
    without losing existing rows."""
    db = tmp_path / "h.sqlite"
    # Hand-build a v1 DB the way T1's HistoryStore would have created it.
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version', '1')")
        conn.execute("""CREATE TABLE ohlc(
            pair TEXT NOT NULL, grain_sec INTEGER NOT NULL, ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT NOT NULL, ingested_at INTEGER NOT NULL,
            PRIMARY KEY(pair, grain_sec, ts))""")
        conn.execute("""INSERT INTO ohlc VALUES(
            'BTC/USD', 3600, 1700000000, 1, 1, 1, 1, 1, 'kraken_archive', 0)""")
        conn.commit()
    # Open with current code — must upgrade silently.
    HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert int(v) == SCHEMA_VERSION
        # Pre-existing ohlc row preserved.
        n = conn.execute("SELECT COUNT(*) FROM ohlc").fetchone()[0]
        assert n == 1
