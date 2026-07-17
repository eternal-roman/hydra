"""Labeler unit tests on constructed candle series with known outcomes."""

from heartbeat.eval.labeler import extract_events
from helpers import base_config, mk_candle


def _series(specs):
    """specs: list of (o, h, l, c); 1h candles, constant volume."""
    return [mk_candle(open_ts=i * 3600, o=o, h=h, l=l, c=c, vol=10.0)
            for i, (o, h, l, c) in enumerate(specs)]


def _downleg_prefix(start=200.0, cycles=10):
    """Down-leg with GENUINE swing lows (local minima), each successively
    lower and printed below the MA9. Cycle of 4: down, deep dip (the swing
    low — lower than both neighbors), partial recovery, drift down."""
    out = []
    px = start
    for c in range(cycles):
        out.append((px, px + 0.3, px - 1.2, px - 1.0))          # down
        out.append((px - 1.0, px - 0.7, px - 4.0, px - 2.0))    # deep dip
        out.append((px - 2.0, px - 0.8, px - 2.3, px - 1.0))    # recovery
        out.append((px - 1.0, px - 0.7, px - 2.2, px - 2.0))    # drift down
        px -= 2.0
    return out


def test_reversal_labeled():
    cfg = base_config()
    specs = _downleg_prefix(cycles=10)           # ends around price 160, ATR ~2.5
    low = specs[-1][3] - 3.0                 # event low
    specs.append((specs[-1][3], specs[-1][3] + 0.2, low, low + 0.5))  # low candle
    px = low + 0.5
    for k in range(12):                      # strong advance, no new low
        specs.append((px, px + 1.8, px - 0.2, px + 1.6))
        px += 1.6
    candles = _series(specs)
    p_up = [0.5] * len(candles)
    events = extract_events("BTC/USD", "1h", candles, p_up, cfg)
    assert len(events) >= 1
    ev = events[-1]
    assert ev.label == "reversal"
    assert ev.p_at["bounce+3"] == 0.5


def test_fake_labeled():
    cfg = base_config()
    specs = _downleg_prefix(cycles=10)
    low = specs[-1][3] - 3.0
    specs.append((specs[-1][3], specs[-1][3] + 0.2, low, low + 0.5))
    px = low + 0.5
    for k in range(3):                       # pop ~1.5 ATR
        specs.append((px, px + 1.4, px - 0.1, px + 1.2))
        px += 1.2
    for k in range(4):                       # rollover to a NEW LOW
        specs.append((px, px + 0.2, px - 2.5, px - 2.2))
        px -= 2.2
    candles = _series(specs)
    events = extract_events("BTC/USD", "1h", candles, [0.5] * len(candles), cfg)
    assert len(events) >= 1
    assert events[-1].label == "fake"


def test_crash_regime_excluded():
    cfg = base_config()
    specs = _downleg_prefix(cycles=10)
    last = specs[-1][3]
    # crash candle: range 30 >> 3x ATR right at the low
    specs.append((last, last + 1, last - 30, last - 25))
    px = last - 25
    for k in range(12):
        specs.append((px, px + 2.0, px - 0.2, px + 1.8))
        px += 1.8
    candles = _series(specs)
    events = extract_events("BTC/USD", "1h", candles, [0.5] * len(candles), cfg)
    lows = [e.low_idx for e in events]
    assert 40 not in lows  # the crash low must not become an event


def test_chop_no_downleg_excluded():
    cfg = base_config()
    # flat chop: no established down-leg -> zero events
    specs = []
    for i in range(60):
        base = 100 + (1 if i % 2 else -1) * 0.5
        specs.append((base, base + 1, base - 1, base))
    candles = _series(specs)
    events = extract_events("BTC/USD", "1h", candles, [0.5] * len(candles), cfg)
    assert events == []


def test_misaligned_series_raises():
    import pytest
    cfg = base_config()
    candles = _series(_downleg_prefix(cycles=5))
    with pytest.raises(ValueError, match="misaligned"):
        extract_events("BTC/USD", "1h", candles, [0.5] * 3, cfg)
