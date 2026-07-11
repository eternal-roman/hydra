"""Regime-selective rails (AI-control re-regulation study).

Default OFF. When on: TREND_UP-only BUY, conf floor 0.55, force-flatten
TREND_DOWN. Does not deregulate friction or min_conf defaults.
"""
from __future__ import annotations

import os

import pytest

from hydra_engine import (
    HydraEngine,
    Regime,
    Signal,
    SignalAction,
    Strategy,
)


def _engine(selective: bool = True, balance: float = 1000.0) -> HydraEngine:
    return HydraEngine(
        initial_balance=balance,
        asset="SOL/USD",
        regime_selective=selective,
        candle_interval=60,
    )


def test_default_off_without_env(monkeypatch):
    monkeypatch.delenv("HYDRA_REGIME_SELECTIVE", raising=False)
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.regime_selective is False


def test_env_enables(monkeypatch):
    monkeypatch.setenv("HYDRA_REGIME_SELECTIVE", "1")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.regime_selective is True


def test_constructor_overrides_env(monkeypatch):
    monkeypatch.setenv("HYDRA_REGIME_SELECTIVE", "1")
    eng = HydraEngine(
        initial_balance=100.0, asset="SOL/USD", regime_selective=False
    )
    assert eng.regime_selective is False


def test_block_buy_ranging():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.90, "MR", Strategy.MEAN_REVERSION)
    out = eng._apply_regime_selective(Regime.RANGING, sig)
    assert out.action == SignalAction.HOLD
    assert "REGIME_SELECTIVE:block_buy" in out.reason


def test_block_buy_volatile():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.90, "GRID", Strategy.GRID)
    out = eng._apply_regime_selective(Regime.VOLATILE, sig)
    assert out.action == SignalAction.HOLD


def test_allow_trend_up_buy_above_floor():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.70, "MOM", Strategy.MOMENTUM)
    out = eng._apply_regime_selective(Regime.TREND_UP, sig)
    assert out.action == SignalAction.BUY


def test_block_trend_up_buy_below_floor():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.54, "MOM", Strategy.MOMENTUM)
    out = eng._apply_regime_selective(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "low_conf" in out.reason


def test_force_flatten_trend_down_when_long():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "idle", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL
    assert out.confidence >= 0.70
    assert "force_flatten" in out.reason


def test_force_flatten_overrides_buy_nibble():
    eng = _engine(True)
    eng.position.size = 0.5
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.BUY, 0.56, "DEF nibble", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL


def test_selective_off_passthrough():
    eng = _engine(False)
    eng.position.size = 1.0
    sig = Signal(SignalAction.BUY, 0.90, "MR", Strategy.MEAN_REVERSION)
    out = eng._apply_regime_selective(Regime.RANGING, sig)
    assert out.action == SignalAction.BUY


def test_selective_does_not_lower_min_conf():
    from hydra_engine import SIZING_COMPETITION
    eng = HydraEngine(
        initial_balance=1000.0,
        asset="SOL/USD",
        sizing=dict(SIZING_COMPETITION),
        regime_selective=True,
    )
    # Product path keeps competition floor; must not silently drop to 0.50/0.55
    assert eng.sizer.min_confidence == 0.65


def test_friction_constants_intact():
    eng = _engine(True)
    assert eng.FRICTION_HURDLE_MULT >= 2.0
    assert eng.ROUND_TRIP_FRICTION_PCT > 0


def test_selective_does_not_set_friction_kill(monkeypatch):
    monkeypatch.delenv("HYDRA_FRICTION_GATE_DISABLED", raising=False)
    _ = _engine(True)
    assert os.environ.get("HYDRA_FRICTION_GATE_DISABLED") != "1"


def test_block_buy_trend_down_when_flat():
    eng = _engine(True)
    eng.position.size = 0.0
    sig = Signal(SignalAction.BUY, 0.90, "DEF nibble", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.HOLD
    assert "block_buy_TREND_DOWN" in out.reason


def test_force_flatten_rewrites_brain_hold():
    """Caller HOLD must become SELL when long under TREND_DOWN (no bypass)."""
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "brain hold", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL
    assert "force_flatten" in out.reason
