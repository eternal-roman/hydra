"""Hold-through rails (product default ON).

TREND_UP-only BUY conf≥0.65, force-flatten TREND_DOWN, ride mid-TREND_UP
except extreme overbought. Kill: HYDRA_HOLD_THROUGH=0.
"""
from __future__ import annotations

import os

from hydra_engine import (
    HydraEngine,
    Regime,
    Signal,
    SignalAction,
    Strategy,
    SIZING_COMPETITION,
)


def _engine(hold: bool = True, balance: float = 1000.0) -> HydraEngine:
    return HydraEngine(
        initial_balance=balance,
        asset="SOL/USD",
        hold_through=hold,
        candle_interval=60,
    )


def test_default_on_without_env(monkeypatch):
    """Flag-combo bakeoff: hold_through is default ON (best active returns)."""
    monkeypatch.delenv("HYDRA_HOLD_THROUGH", raising=False)
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.hold_through is True


def test_env_disables(monkeypatch):
    monkeypatch.setenv("HYDRA_HOLD_THROUGH", "0")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.hold_through is False


def test_env_enables_explicit(monkeypatch):
    monkeypatch.setenv("HYDRA_HOLD_THROUGH", "1")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.hold_through is True


def test_constructor_overrides_env(monkeypatch):
    monkeypatch.setenv("HYDRA_HOLD_THROUGH", "0")
    eng = HydraEngine(
        initial_balance=100.0, asset="SOL/USD", hold_through=True
    )
    assert eng.hold_through is True


def test_skip_buy_ranging():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.90, "MR", Strategy.MEAN_REVERSION)
    out = eng._apply_hold_through(Regime.RANGING, sig)
    assert out.action == SignalAction.HOLD
    assert "HOLD_THROUGH:skip_buy" in out.reason


def test_skip_buy_below_065():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.60, "MOM", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "low_conf" in out.reason


def test_allow_trend_up_buy_at_065():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.65, "MOM", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.BUY


def test_ride_mid_trend_sell():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(
        SignalAction.SELL,
        0.70,
        "Momentum fading: MACD hist -1.0 < 0, price 99 < BB mid 100, RSI 55",
        Strategy.MOMENTUM,
    )
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "ride_trend" in out.reason


def test_allow_extreme_overbought_sell():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(
        SignalAction.SELL,
        0.80,
        "Momentum fading: RSI 86.0 > 85 extreme overbought",
        Strategy.MOMENTUM,
    )
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.SELL


def test_high_conf_alone_does_not_exit_mid_trend():
    """Calibration: conf is not a useful mid-trend exit gate — ride instead."""
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.SELL, 0.90, "strong fade", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "ride_trend" in out.reason


def test_force_flatten_trend_down():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "idle", Strategy.DEFENSIVE)
    out = eng._apply_hold_through(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL
    assert out.confidence >= eng.HOLD_THROUGH_FLATTEN_CONF
    assert "force_flatten" in out.reason


def test_allow_sell_in_ranging_when_long():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.SELL, 0.70, "MR exit", Strategy.MEAN_REVERSION)
    out = eng._apply_hold_through(Regime.RANGING, sig)
    assert out.action == SignalAction.SELL


def test_off_passthrough():
    eng = _engine(False)
    eng.position.size = 1.0
    sig = Signal(SignalAction.SELL, 0.70, "fade", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.SELL


def test_does_not_lower_min_conf():
    eng = HydraEngine(
        initial_balance=1000.0,
        asset="SOL/USD",
        sizing=dict(SIZING_COMPETITION),
        hold_through=True,
    )
    assert eng.sizer.min_confidence == 0.65


def test_does_not_set_friction_kill(monkeypatch):
    monkeypatch.delenv("HYDRA_FRICTION_GATE_DISABLED", raising=False)
    _ = _engine(True)
    assert os.environ.get("HYDRA_FRICTION_GATE_DISABLED") != "1"


def test_entry_floor_is_065():
    """Bakeoff: 0.65 entry floor (0.55 re-opened losses)."""
    assert HydraEngine.HOLD_THROUGH_BUY_MIN_CONF == 0.65
