"""Geometry port parity-by-construction tests (synthetic sequences)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.candles import DailyBar  # noqa: E402
from s3bounce.setups import (  # noqa: E402
    BOUNCE_ATR, ma, swing_lows, robust_atr, causal_setups, entry_index)

DAY = 86400


def bar(i, o, h, low, c, v=1.0):
    return DailyBar(open_ts=float(i * DAY), open=o, high=h, low=low,
                    close=c, volume=v)


def flat(n, px=100.0, rng=1.0, start=0):
    return [bar(start + i, px, px + rng / 2, px - rng / 2, px) for i in range(n)]


def test_ma_none_below_period():
    assert ma([1.0] * 8, 9, 7) is None
    assert ma([2.0] * 9, 9, 8) == 2.0


def test_swing_low_semantics():
    lows = [5, 4, 3, 4, 5, 3, 3, 3, 5, 4]
    bars = [bar(i, low + 1, low + 2, low, low + 1) for i, low in enumerate(lows)]
    sw = swing_lows(bars, 2)
    assert 2 in sw                       # strict-below neighbors
    # plateau 5..7 all equal 3: index 5 has a strictly-lower... none; but
    # window contains equal lows only -> needs at least one strictly greater? no:
    # rule = lo <= all AND lo < at least one in window. Index 5 window lows
    # [3,4,3,3,3]: 3 <= all and 3 < 4 -> qualifies.
    assert 5 in sw


def test_robust_atr_drops_crash_spike():
    bars = flat(15)
    a_clean = robust_atr(bars, 14, 3.0)
    assert a_clean is not None and abs(a_clean - 1.0) < 1e-9
    spiked = bars[:-1] + [bar(14, 100, 150, 50, 100)]      # 100-range spike
    a_spiked = robust_atr(spiked, 14, 3.0)
    assert abs(a_spiked - 1.0) < 1e-9                       # outlier dropped


def down_leg_bars():
    """Down-leg with four distinct descending swing lows (4, 9, 14, 19)
    below MA9, then a bounce candle whose high clears L0 + 1.0*ATR but
    stays under the 3.3*ATR target (so the setup is entryable at b1)."""
    lows = [100, 99, 98, 97, 96, 97, 98, 95, 93, 92, 93, 94, 91, 89, 88,
            89, 90, 86, 85, 84, 85, 86]
    bars = [bar(i, lo + 0.5, lo + 1.2, lo, lo + 0.3) for i, lo in enumerate(lows)]
    bars.append(bar(len(lows), 85.0, 86.6, 84.5, 86.4))       # bounce (idx 22)
    bars += [bar(len(lows) + 1 + i, 86.0, 86.8, 85.5, 86.0) for i in range(5)]
    return bars


def test_causal_setup_found_with_bounce():
    bars = down_leg_bars()
    setups = [s for s in causal_setups(bars) if s.low_px == 84]
    assert setups, "expected the down-leg setup at low 84"
    s = setups[0]
    assert s.bounce_idx is not None
    assert bars[s.bounce_idx].high >= s.low_px + BOUNCE_ATR * s.atr
    assert s.low_px == min(b.low for b in bars[: s.bounce_idx])


def test_crash_exclusion():
    bars = down_leg_bars()
    i = 18                                     # near the final swing low
    crashed = list(bars)
    crashed[i] = bar(i, 87.5, 88.0, 60.0, 87.3)   # range >> 3*ATR
    assert all(s.low_idx != i for s in causal_setups(crashed))


def test_entry_index_none_on_resolved():
    bars = down_leg_bars()
    s = [x for x in causal_setups(bars) if x.low_px == 84][0]
    # undercut the low right after bounce -> resolved (fake) before entry
    undercut = list(bars)
    j = s.bounce_idx + 1
    undercut[j] = bar(j, 92, 92.5, s.low_px - 1.0, 92)
    s2 = [x for x in causal_setups(undercut) if x.low_idx == s.low_idx]
    if s2:
        assert entry_index(undercut, s2[0], 1) is None
    assert entry_index(bars, s, 1) == s.bounce_idx + 1
