"""Feature port semantics on constructed sequences."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.candles import DailyBar  # noqa: E402
from s3bounce.setups import Setup  # noqa: E402
from s3bounce.features import (  # noqa: E402
    FEATURES, compute_features, fresh_low_days, recency_at, shock_flags)

DAY = 86400


def bar(i, o, h, low, c, v=1.0):
    return DailyBar(open_ts=float(i * DAY), open=o, high=h, low=low,
                    close=c, volume=v)


def make_setup(low_idx, bounce_idx, low_px, atr):
    return Setup(low_idx=low_idx, low_px=low_px, atr=atr,
                 bounce_idx=bounce_idx, label=None)


def base_bars(n=40):
    return [bar(i, 100, 101, 99, 100, v=10.0) for i in range(n)]


def test_clv_close_at_high_and_features_keys():
    bars = base_bars()
    bars[30] = bar(30, 99, 102, 99, 102, v=10.0)     # close == high -> clv +1
    s = make_setup(28, 30, 99.0, 1.0)
    compute_features(bars, [s], {"A": set()})
    assert set(s.x) == set(FEATURES)
    assert s.x["clv"] == 1.0
    assert s.x["range_atr"] == 3.0                    # (102-99)/1.0


def test_vol_z_zero_sd_guard():
    bars = base_bars()                                # constant volume
    s = make_setup(28, 30, 99.0, 1.0)
    compute_features(bars, [s], {})
    assert s.x["vol_z"] == 0.0 and s.x["breadth"] == 0.0


def test_shock_recency_cap_and_flag():
    # small alternating drift so the causal sigma window is nonzero
    # (the sd>0 guard means a zero-variance tape can never flag a shock)
    bars = [bar(i, 100, 101, 99, 100 + 0.2 * (i % 2)) for i in range(25)]
    bars.append(bar(25, 100, 130, 100, 130))          # +30% shock day (idx 25)
    # stay at the new level afterwards: no reverse shock at 26
    bars += [bar(i, 130, 131, 129, 130 + 0.2 * (i % 2)) for i in range(26, 40)]
    flags = shock_flags(bars)
    assert flags[25]
    assert recency_at(flags, 27) == 2
    assert recency_at(flags, 24) == 10                # nothing at/before


def test_breadth_counts_fresh_low_window():
    bars = base_bars()
    setup_day = 30
    s = make_setup(setup_day, 32, 99.0, 1.0)
    others = {"BTC": {setup_day - 1}, "ETH": {setup_day - 5}, "ZEC": set()}
    compute_features(bars, [s], others)
    assert s.x["breadth"] == 1.0                      # only BTC within 3 days


def test_retest_within_quarter_atr():
    bars = base_bars()
    bars[10] = bar(10, 100, 101, 95.2, 100)           # prior low near setup low
    bars[30] = bar(30, 100, 101, 95.0, 100)           # setup low bar
    s = make_setup(30, 32, 95.0, 1.0)
    compute_features(bars, [s], {})
    assert s.x["retest"] == 1.0
    bars[10] = bar(10, 100, 101, 96.0, 100)           # 1.0 away > 0.25*ATR
    compute_features(bars, [s], {})
    assert s.x["retest"] == 0.0


def test_fresh_low_days():
    bars = base_bars()
    bars[35] = bar(35, 100, 101, 90.0, 100)
    days = fresh_low_days(bars)
    assert 35 in days and len(days) == 1
