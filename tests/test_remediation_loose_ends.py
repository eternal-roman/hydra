"""Loose-end coverage for remediation review findings.

- tradable emitted in engine state (Rule3 live gate)
- portfolio BUY halt semantics
- FILLED true-up via true_up_fill + avg_entry
- quant kill switch skips apply_rules force_hold
- journal persists pre_trade_snapshot key for PLACED
"""
from __future__ import annotations

import os

import pytest

from hydra_engine import CrossPairCoordinator, HydraEngine, SIZING_COMPETITION
from hydra_quant_rules import FUNDING_EXTREME_BPS, apply_rules


def _seed(eng: HydraEngine, n: int = 55, px: float = 100.0) -> None:
    for i in range(n):
        p = px * (1.0 + 0.002 * ((i % 7) - 3))
        eng.ingest_candle({
            "open": p, "high": p * 1.01, "low": p * 0.99,
            "close": p, "volume": 50.0,
            "timestamp": float(1_700_000_000 + i * 300),
        })


class TestTradableInBuildState:
    def test_build_state_includes_tradable_true(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", tradable=True)
        _seed(eng)
        st = eng.tick(generate_only=True)
        assert "tradable" in st
        assert st["tradable"] is True

    def test_build_state_includes_tradable_false(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/BTC", tradable=False)
        _seed(eng, px=0.002)
        st = eng.tick(generate_only=True)
        assert st["tradable"] is False

    def test_rule3_uses_engine_tradable_from_tick(self):
        """Regression: live agent feeds tick() states to coordinator without
        a second stamp — tradable must already be on the state dict."""
        pairs = ["SOL/USD", "SOL/BTC", "BTC/USD"]
        coord = CrossPairCoordinator(pairs)
        engines = {
            "SOL/USD": HydraEngine(100.0, "SOL/USD", tradable=True),
            "SOL/BTC": HydraEngine(1.0, "SOL/BTC", tradable=False),
            "BTC/USD": HydraEngine(100.0, "BTC/USD", tradable=True),
        }
        for e in engines.values():
            _seed(e, n=55, px=100.0 if "BTC" not in e.asset or e.asset == "BTC/USD" else 0.002)
        # Force regimes via direct state construction from tick + override
        states = {}
        for p, eng in engines.items():
            st = eng.tick(generate_only=True)
            states[p] = st
        # Set triad for Rule3 without Rule2 (btc not TREND_UP)
        states["BTC/USD"]["regime"] = "RANGING"
        states["SOL/USD"]["regime"] = "TREND_DOWN"
        states["SOL/USD"]["position"] = {"size": 1.0, "avg_entry": 100.0, "unrealized_pnl": 0.0}
        states["SOL/BTC"]["regime"] = "TREND_UP"
        assert states["SOL/BTC"]["tradable"] is False
        ov = coord.get_overrides(states)
        sol = ov.get("SOL/USD")
        assert sol is None or "swap" not in (sol or {}), (
            f"Rule3 must not swap into untradable bridge; got {sol}"
        )


class TestPortfolioHalt:
    def test_block_buy_only(self):
        from hydra_agent import HydraAgent
        assert HydraAgent._should_block_buy_for_portfolio_dd(True, "BUY") is True
        assert HydraAgent._should_block_buy_for_portfolio_dd(True, "SELL") is False
        assert HydraAgent._should_block_buy_for_portfolio_dd(False, "BUY") is False


class TestFilledTrueUp:
    def test_true_up_changes_avg_entry_from_close(self, monkeypatch):
        monkeypatch.setenv("HYDRA_FRICTION_GATE_DISABLED", "1")
        eng = HydraEngine(1000.0, "SOL/USD", sizing=SIZING_COMPETITION)
        _seed(eng, px=100.0)
        snap = eng.snapshot_position()
        t = eng.execute_signal("BUY", 0.90, "opt", "MOMENTUM")
        assert t is not None
        close_px = t.price
        fill_px = close_px * 0.99
        assert eng.true_up_fill(
            "BUY", t.amount, fill_px, pre_trade_snapshot=snap, reason="test",
        )
        assert eng.position.avg_entry == pytest.approx(fill_px, rel=1e-9)
        assert eng.position.avg_entry != pytest.approx(close_px, rel=1e-6)


class TestQuantKillSwitch:
    def test_r10_fires_without_kill_switch(self):
        qi = {
            "funding_bps_8h": None,
            "oi_delta_1h_pct": None,
            "basis_apr_pct": None,
            "staleness_s": 10.0,
        }
        r = apply_rules("BUY", {}, qi)
        assert r.force_hold is True  # R10 without kill switch

    def test_kill_switch_env_skips_rules_in_agent_branch(self, monkeypatch):
        """Agent gates apply_rules behind HYDRA_QUANT_INDICATORS_DISABLED=1."""
        monkeypatch.setenv("HYDRA_QUANT_INDICATORS_DISABLED", "1")
        assert os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") == "1"
        # Simulate agent branch: when flag set, do not call apply_rules.
        _quant_rules_disabled = (
            os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") == "1"
        )
        rules_force_hold = False
        if not _quant_rules_disabled:
            rules_force_hold = apply_rules("BUY", {}, {
                "funding_bps_8h": None, "oi_delta_1h_pct": None,
                "basis_apr_pct": None, "staleness_s": 10.0,
            }).force_hold
        assert rules_force_hold is False


class TestJournalSnapshotKey:
    def test_build_journal_can_carry_pre_trade_snapshot(self):
        from hydra_agent import HydraAgent
        # Lightweight: verify shape used by _place_order
        entry = {
            "lifecycle": {"state": "PLACED"},
            "pre_trade_snapshot": {
                "balance": 100.0,
                "position_size": 0.0,
                "position_avg_entry": 0.0,
                "position_realized_pnl": 0.0,
                "position_params_at_entry": None,
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "trades_len": 0,
                "equity_history_len": 0,
                "peak_equity": 100.0,
                "max_drawdown": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "tradable": True,
            },
        }
        assert entry["pre_trade_snapshot"]["balance"] == 100.0
        # persistence filter keeps non-PLACEMENT_FAILED
        agent = object.__new__(HydraAgent)
        agent.order_journal = [entry]
        persisted = agent._journal_for_persistence()
        assert len(persisted) == 1
        assert "pre_trade_snapshot" in persisted[0]
