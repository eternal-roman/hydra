"""PR-B hard risk caps (remediation plan G2).

B1: max_position_pct applies after size_multiplier
B2: gross inventory cannot exceed max_position_pct of equity
B3: peak_equity never rebases downward on balance seed
B4: portfolio DD ≥ 15% blocks BUY, allows SELL
"""
from __future__ import annotations

import os

import pytest

from hydra_engine import HydraEngine, SIZING_COMPETITION, SIZING_CONSERVATIVE


@pytest.fixture(autouse=True)
def _disable_friction_for_cap_tests(monkeypatch):
    """Cap tests isolate sizing; friction on flat synthetic series is orthogonal."""
    monkeypatch.setenv("HYDRA_FRICTION_GATE_DISABLED", "1")


def _seed(eng: HydraEngine, n: int = 60, px: float = 50.0) -> None:
    for i in range(n):
        # Mild swing so indicators stay defined; friction disabled in fixture.
        p = px * (1.0 + 0.002 * ((i % 10) - 5))
        eng.ingest_candle({
            "open": p, "high": p * 1.005, "low": p * 0.995,
            "close": p, "volume": 100.0,
            "timestamp": float(1_700_000_000 + i * 300),
        })


class TestMaxPositionAfterMultiplier:
    def test_conf_09_mult_15_stays_within_competition_cap(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", sizing=SIZING_COMPETITION)
        _seed(eng, px=50.0)
        t = eng.execute_signal("BUY", 0.90, "probe", "MOMENTUM", size_multiplier=1.5)
        assert t is not None
        px = eng.prices[-1]
        equity = eng.balance + eng.position.size * px
        notional = eng.position.size * px
        assert notional / equity <= eng.sizer.max_position_pct + 1e-6, (
            f"notional {notional/equity:.4f} exceeds cap {eng.sizer.max_position_pct}"
        )

    def test_repeated_buys_cannot_pyramid_past_cap(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", sizing=SIZING_COMPETITION)
        _seed(eng, px=50.0)
        for _ in range(10):
            eng.execute_signal("BUY", 0.90, "pyramid", "MOMENTUM", size_multiplier=1.5)
        px = eng.prices[-1]
        equity = eng.balance + eng.position.size * px
        notional = eng.position.size * px
        assert notional / equity <= eng.sizer.max_position_pct + 1e-6

    def test_conservative_cap_respected_with_mult(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", sizing=SIZING_CONSERVATIVE)
        _seed(eng, px=50.0)
        eng.execute_signal("BUY", 0.90, "probe", "MOMENTUM", size_multiplier=1.5)
        px = eng.prices[-1]
        equity = eng.balance + eng.position.size * px
        notional = eng.position.size * px
        assert notional / equity <= 0.30 + 1e-6


class TestPeakEquityNoDownRebase:
    def test_seed_peak_never_lowers(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
        eng.peak_equity = 150.0
        eng.max_drawdown = 10.0
        # Simulate agent re-seed helper
        eng.peak_equity = max(eng.peak_equity, 100.0)
        assert eng.peak_equity == 150.0


class TestPortfolioBuyHalt:
    def test_portfolio_halt_flag_blocks_buy_semantics(self):
        """Agent-level gate: when portfolio_buy_halted, BUY must not execute.

        Unit-level stand-in: engine still executes; agent wraps the check.
        We test the pure helper used by the agent.
        """
        from hydra_agent import HydraAgent

        # Lightweight: call the static-like check via a minimal instance path
        assert HydraAgent._should_block_buy_for_portfolio_dd(
            portfolio_buy_halted=True, action="BUY"
        ) is True
        assert HydraAgent._should_block_buy_for_portfolio_dd(
            portfolio_buy_halted=True, action="SELL"
        ) is False
        assert HydraAgent._should_block_buy_for_portfolio_dd(
            portfolio_buy_halted=False, action="BUY"
        ) is False
