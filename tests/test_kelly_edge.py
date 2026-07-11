"""PR-D: excess-over-threshold Kelly + timeframe friction."""
from __future__ import annotations

import os

import pytest

from hydra_engine import HydraEngine, PositionSizer, SIZING_COMPETITION, Signal, SignalAction, Strategy


def test_conf_at_min_sizes_less_than_old_formula():
    s = PositionSizer(**SIZING_COMPETITION)
    # Old edge at 0.65 was 0.30 → half-Kelly 0.15 of bankroll
    # New edge at 0.65 is 0.10 → half-Kelly 0.05 of bankroll
    size = s.calculate(0.65, 100.0, 50.0, "SOL/USD")
    notional = size * 50.0
    assert notional == pytest.approx(5.0, rel=1e-6)  # 5% of balance
    assert notional < 15.0  # old was 15%


def test_conf_one_reaches_half_kelly_cap():
    s = PositionSizer(**SIZING_COMPETITION)
    size = s.calculate(1.0, 100.0, 50.0, "SOL/USD")
    notional = size * 50.0
    # edge 1.0 * 0.50 = 0.50, but max_position 0.40 clamps
    assert notional == pytest.approx(40.0, rel=1e-6)


def test_friction_hurdle_higher_on_1h(monkeypatch):
    monkeypatch.delenv("HYDRA_FRICTION_GATE_DISABLED", raising=False)
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", candle_interval=60)
    # Seed with tiny expected move MR signal via direct _maybe_execute path
    for i in range(40):
        p = 100.0 + 0.01 * i
        eng.ingest_candle({
            "open": p, "high": p + 0.05, "low": p - 0.05,
            "close": p, "volume": 10, "timestamp": float(i),
        })
    # Force a BUY with tiny BB distance by using MOMENTUM with low atr
    sig = Signal(
        action=SignalAction.BUY, confidence=0.90, reason="t",
        strategy=Strategy.MOMENTUM,
        indicators={"atr_pct": 0.3, "price": eng.prices[-1]},
    )
    # 2*0.3 = 0.6% < 2.0% 1h hurdle
    before = eng.friction_skips
    t = eng._maybe_execute(sig)
    assert t is None
    assert eng.friction_skips == before + 1
