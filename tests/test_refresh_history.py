import time
from hydra_history_store import HistoryStore, CandleRow
from tools.refresh_history import refresh_pair, fill_gaps_for_pair


class _StubCli:
    def __init__(self, rows):
        self._rows = rows

    def ohlc(self, pair, interval=60):
        return self._rows


def test_refresh_inserts_rest_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 1.0},
    ])
    n = refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    assert n == 1
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.source == "kraken_rest"
    assert got.close == 10.5


def test_refresh_does_not_overwrite_archive(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    store.upsert_candles([CandleRow("BTC/USD", 3600, 1_700_000_000,
                                    1, 1, 1, 1, 1, "kraken_archive")])
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 99, "high": 99, "low": 99,
         "close": 99, "volume": 99},
    ])
    refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.close == 1  # archive preserved


class _PagingStubCli:
    """Stub that emits two pages of fake candles, then empty."""
    def __init__(self):
        self.calls = []

    def ohlc(self, pair, interval=60):
        return []  # not used by gap-fill

    def ohlc_paged(self, pair, interval=60, since=0):
        self.calls.append((pair, interval, since))
        if len(self.calls) == 1:
            # First page: 3 candles, last_cursor = ts of last
            base = since + 3600
            candles = [
                {"timestamp": base + i * 3600, "open": 1, "high": 1, "low": 1,
                 "close": 1.0 + i, "volume": 1.0}
                for i in range(3)
            ]
            return candles, base + 2 * 3600
        if len(self.calls) == 2:
            # Second page: 1 more candle, last_cursor advances by 1h
            base = since + 3600
            candles = [{"timestamp": base, "open": 1, "high": 1, "low": 1,
                        "close": 99.0, "volume": 1.0}]
            return candles, base
        return [], 0  # no more


def test_fill_gaps_for_pair_pages_until_empty(tmp_path):
    """Multi-page gap fill walks the cursor forward until the source returns
    empty. Tier-policy keeps existing archive rows untouched."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    # Seed an archive row 100 hours in the past so there's a gap to fill.
    now = int(time.time())
    archive_ts = (now // 3600) * 3600 - 100 * 3600
    store.upsert_candles([CandleRow("BTC/USD", 3600, archive_ts,
                                    1, 1, 1, 1, 1, "kraken_archive")])
    cli = _PagingStubCli()
    n = fill_gaps_for_pair(store, "BTC/USD", grain_sec=3600, cli=cli,
                           max_pages=10, sleep_sec=0)
    # 3 + 1 = 4 candles written across 2 pages.
    assert n == 4
    assert len(cli.calls) == 3   # 2 productive + 1 empty terminator
    # Archive row preserved.
    rows = list(store.fetch("BTC/USD", 3600, archive_ts, archive_ts))
    assert len(rows) == 1
    assert rows[0].source == "kraken_archive"


def test_fill_gaps_no_op_when_caught_up(tmp_path):
    """If last_ts is within grain_sec of now, gap-fill is a no-op (no calls)."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    fresh_ts = (int(time.time()) // 3600) * 3600
    store.upsert_candles([CandleRow("BTC/USD", 3600, fresh_ts,
                                    1, 1, 1, 1, 1, "kraken_rest")])
    cli = _PagingStubCli()
    n = fill_gaps_for_pair(store, "BTC/USD", grain_sec=3600, cli=cli,
                           max_pages=10, sleep_sec=0)
    assert n == 0
    assert len(cli.calls) == 0


def test_refresh_skips_forming_candle(tmp_path):
    """Kraken's last OHLC row is the still-forming candle; upserting it
    freezes a partial row under the tier policy (trade-tape audit found
    frozen rows with volume ~10x low). Only completed candles may land."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    now = int(time.time())
    complete_ts = (now // 3600 - 1) * 3600   # last fully closed hour
    forming_ts = (now // 3600) * 3600        # current, still-forming hour
    cli = _StubCli([
        {"timestamp": complete_ts, "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 5.0},
        {"timestamp": forming_ts, "open": 10.5, "high": 10.6, "low": 10.4,
         "close": 10.55, "volume": 0.3},
    ])
    n = refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    assert n == 1
    rows = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert [r.ts for r in rows] == [complete_ts]


def test_fill_gaps_skips_forming_candle(tmp_path):
    """Same forming-candle rule on the gap-fill paging path."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    now = int(time.time())
    seed_ts = (now // 3600 - 3) * 3600
    store.upsert_candles([CandleRow("BTC/USD", 3600, seed_ts,
                                    1, 1, 1, 1, 1, "kraken_rest")])

    class _FormingStubCli:
        calls = 0

        def ohlc_paged(self, pair, interval=60, since=0):
            self.calls += 1
            if self.calls > 1:
                return [], 0
            forming = (int(time.time()) // 3600) * 3600
            return ([{"timestamp": forming - 3600, "open": 1, "high": 1,
                      "low": 1, "close": 2.0, "volume": 1.0},
                     {"timestamp": forming, "open": 1, "high": 1, "low": 1,
                      "close": 3.0, "volume": 0.1}], forming)

    n = fill_gaps_for_pair(store, "BTC/USD", 3600, cli=_FormingStubCli(),
                           sleep_sec=0)
    assert n == 1
    stored_ts = [r.ts for r in store.fetch("BTC/USD", 3600, 0, 9_999_999_999)]
    assert (int(time.time()) // 3600) * 3600 not in stored_ts
