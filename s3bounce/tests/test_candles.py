"""DailyBarSeries: 1h → UTC-day aggregation, seeding, forming-day exclusion."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.candles import DailyBar, DailyBarSeries  # noqa: E402

DAY = 86400


def h(ts, o, hi, lo, c, v=1.0):
    return dict(ts=ts, open=o, high=hi, low=lo, close=c, volume=v)


def test_1h_rows_aggregate_to_utc_day():
    s = DailyBarSeries()
    base = 100 * DAY
    s.update_1h(base + 0 * 3600, 10, 12, 9, 11, 1.0)
    s.update_1h(base + 5 * 3600, 11, 15, 10, 14, 2.0)
    s.update_1h(base + 23 * 3600, 14, 14.5, 8, 9, 0.5)
    bars = s.completed_bars(now_ts=base + 2 * DAY)
    assert len(bars) == 1
    b = bars[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (10, 15, 8, 9, 3.5)
    assert b.open_ts == base and b.day == 100
    assert b.range == 7 and b.close_ts == base + DAY


def test_forming_day_excluded_until_complete():
    s = DailyBarSeries()
    base = 100 * DAY
    s.update_1h(base, 10, 12, 9, 11, 1.0)
    assert s.completed_bars(now_ts=base + 3600) == []          # still forming
    assert len(s.completed_bars(now_ts=base + DAY)) == 1        # boundary = complete


def test_seed_never_overwrites_1h_built_day():
    s = DailyBarSeries()
    base = 100 * DAY
    s.update_1h(base, 10, 12, 9, 11, 1.0)
    s.seed([h(base, 99, 99, 99, 99, 99.0), h(base - DAY, 5, 6, 4, 5, 1.0)])
    bars = s.completed_bars(now_ts=base + 2 * DAY)
    assert [b.day for b in bars] == [99, 100]
    assert bars[1].close == 11                                  # 1h data won
    assert bars[0].close == 5                                   # seed filled the gap


def test_seed_idempotent_and_sorted():
    s = DailyBarSeries()
    rows = [h(101 * DAY, 2, 3, 1, 2), h(100 * DAY, 1, 2, 0.5, 1.5)]
    s.seed(rows)
    s.seed(rows)
    bars = s.completed_bars(now_ts=103 * DAY)
    assert [b.day for b in bars] == [100, 101]
    assert isinstance(bars[0], DailyBar)
