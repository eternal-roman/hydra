"""Exit-policy semantics (mirrors the exit-gate unified simulator)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.candles import DailyBar  # noqa: E402
from s3bounce.exits import (  # noqa: E402
    HOLD_K, ExitDecision, OpenPosition, evaluate)
from s3bounce.setups import HORIZON, TARGET_ATR  # noqa: E402

DAY = 86400


def bar(i, o, h, low, c):
    return DailyBar(open_ts=float(i * DAY), open=o, high=h, low=low,
                    close=c, volume=1.0)


def pos(arm, low_px=100.0, atr=2.0, low_idx=10, entry_idx=12):
    return OpenPosition(asset="BTC/USD", arm=arm, entry_ts=entry_idx * DAY,
                        entry_px=103.0, low_px=low_px, atr=atr,
                        low_idx=low_idx, entry_idx=entry_idx)


def test_x0_touch_stop_fills_min_close_l0():
    p = pos("x0_registered")
    d = evaluate("x0_registered", p, bar(13, 101, 102, 99, 101.5), 13)
    assert d == ExitDecision(100.0, "stop")            # close above L0 -> L0
    d = evaluate("x0_registered", p, bar(13, 101, 102, 98, 98.5), 13)
    assert d == ExitDecision(98.5, "stop")             # close below -> close


def test_x1_close_stop_ignores_wick():
    p = pos("x1_close_stop")
    assert evaluate("x1_close_stop", p, bar(13, 101, 102, 99, 101.5), 13) is None
    d = evaluate("x1_close_stop", p, bar(13, 101, 102, 98, 99.5), 13)
    assert d == ExitDecision(99.5, "stop_close")


def test_stop_priority_over_target_same_bar():
    tgt = 100.0 + TARGET_ATR * 2.0
    p = pos("x0_registered")
    d = evaluate("x0_registered", p, bar(13, 101, tgt + 1, 99, 105), 13)
    assert d.reason == "stop"                          # conservative ordering
    d = evaluate("x0_registered", p, bar(13, 101, tgt + 1, 100.5, 105), 13)
    assert d == ExitDecision(tgt, "target")


def test_horizon_anchored_at_low_idx():
    p = pos("x1_close_stop", low_idx=10)
    quiet = bar(0, 101, 102, 100.5, 101)
    assert evaluate("x1_close_stop", p, quiet, 10 + HORIZON) is None
    d = evaluate("x1_close_stop", p, quiet, 10 + HORIZON + 1)
    assert d == ExitDecision(101, "time")


def test_hold_k60_anchored_at_entry():
    p = pos("hold_k60_stop", entry_idx=12)
    quiet = bar(0, 101, 200, 100.5, 150)               # no target in this arm
    assert evaluate("hold_k60_stop", p, quiet, 12 + HOLD_K - 1) is None
    d = evaluate("hold_k60_stop", p, quiet, 12 + HOLD_K)
    assert d == ExitDecision(150, "time")
    d = evaluate("hold_k60_stop", p, bar(0, 101, 102, 98, 99), 20)
    assert d == ExitDecision(99, "stop_close")


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        evaluate("nope", pos("x1_close_stop"), bar(13, 1, 2, 0.5, 1), 13)

# ---- trail arms (shadow-only; trail gate 2026-07-19) -----------------------
# arming line = L0 + 3.3*ATR = 106.6 with the default pos() fixture

ARM_LINE = 100.0 + TARGET_ATR * 2.0


def test_x4a_unarmed_behaves_as_close_stop():
    p = pos("x4a_trail_ma9")
    assert evaluate("x4a_trail_ma9", p, bar(13, 101, 102, 99, 101.5), 13,
                    ma9=200.0) is None                 # wick + close>L0: open
    d = evaluate("x4a_trail_ma9", p, bar(13, 101, 102, 98, 99.5), 13,
                 ma9=200.0)
    assert d == ExitDecision(99.5, "stop_close")       # ma9 ignored unarmed


def test_x4a_arms_at_target_line_and_trails_ma9():
    p = pos("x4a_trail_ma9")
    # close at the arming line: arms, does NOT exit (target is not an exit)
    assert evaluate("x4a_trail_ma9", p, bar(13, 101, ARM_LINE + 1, 101,
                                            ARM_LINE), 13, ma9=90.0) is None
    assert p.armed
    # armed: wick below L0 no longer stops; only close<ma9 exits
    assert evaluate("x4a_trail_ma9", p, bar(14, 108, 110, 98, 108.0), 14,
                    ma9=107.0) is None
    d = evaluate("x4a_trail_ma9", p, bar(15, 108, 109, 105, 106.0), 15,
                 ma9=107.0)
    assert d == ExitDecision(106.0, "trail")


def test_x4a_same_bar_arm_then_trail_exit():
    # close >= arming line while still under MA9: arm and exit same bar
    p = pos("x4a_trail_ma9")
    d = evaluate("x4a_trail_ma9", p, bar(13, 101, ARM_LINE + 2, 101,
                                         ARM_LINE + 0.5), 13, ma9=ARM_LINE + 1)
    assert d == ExitDecision(ARM_LINE + 0.5, "trail")


def test_x4a_missing_ma9_keeps_position_open():
    p = pos("x4a_trail_ma9")
    p.armed = True
    assert evaluate("x4a_trail_ma9", p, bar(14, 108, 110, 90, 95.0), 14,
                    ma9=None) is None                  # fails open, no trail
    quiet = bar(0, 101, 102, 100.5, 101)
    d = evaluate("x4a_trail_ma9", p, quiet, 10 + HORIZON + 1, ma9=None)
    assert d == ExitDecision(101, "time")              # horizon backstop


def test_x5_routes_by_premium_against_cut():
    # premium_atr = (103-100)/2 = 1.5
    weak = pos("x5_vigor_routed")
    weak.premium_cut = 1.6                             # 1.5 <= 1.6 -> x1
    tgt_bar = bar(13, 101, ARM_LINE + 1, 101, ARM_LINE - 1)
    assert evaluate("x5_vigor_routed", weak, tgt_bar, 13,
                    ma9=200.0).reason == "target"
    strong = pos("x5_vigor_routed")
    strong.premium_cut = 1.4                           # 1.5 > 1.4 -> x4a
    assert evaluate("x5_vigor_routed", strong, tgt_bar, 13,
                    ma9=90.0) is None                  # high>=tgt is no exit
    assert not strong.armed                            # close < arm line


def test_x5_without_cut_is_conservative_x1():
    p = pos("x5_vigor_routed")                         # premium_cut=None
    d = evaluate("x5_vigor_routed", p,
                 bar(13, 101, ARM_LINE + 1, 101, ARM_LINE - 1), 13, ma9=90.0)
    assert d.reason == "target"
