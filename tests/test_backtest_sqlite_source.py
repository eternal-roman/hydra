"""Tests for SqliteSource and the 'sqlite' branch of make_candle_source."""
from hydra_backtest import SqliteSource, BacktestConfig, make_candle_source
from hydra_history_store import HistoryStore, CandleRow


def test_sqlite_source_yields_in_window(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow("BTC/USD", 3600, 1_700_000_000 + i * 3600,
                  10, 11, 9, 10, 1, "kraken_archive")
        for i in range(5)
    ]
    store.upsert_candles(rows)
    src = SqliteSource(
        db_path=str(tmp_path / "h.sqlite"),
        grain_sec=3600,
        start_ts=1_700_000_000 + 1 * 3600,
        end_ts=1_700_000_000 + 3 * 3600,
    )
    candles = list(src.iter_candles("BTC/USD"))
    assert len(candles) == 3


def test_factory_default_is_sqlite(tmp_path):
    HistoryStore(str(tmp_path / "h.sqlite"))
    cfg = BacktestConfig(
        name="t",
        pairs=("BTC/USD",),
        data_source="sqlite",
        data_source_params_json='{"db_path": "' + str(tmp_path / "h.sqlite").replace("\\", "\\\\") + '", "grain_sec": 3600, "start_ts": 0, "end_ts": 9999999999}',
    )
    src = make_candle_source(cfg)
    assert isinstance(src, SqliteSource)
