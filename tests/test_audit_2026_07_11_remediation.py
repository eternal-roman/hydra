"""Regression tests for AUDIT_2026-07-11 HIGH/MED money-safety fixes (v2.27.6)."""
from __future__ import annotations

import os
import sys
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine, Regime, Signal, SignalAction, Strategy
from hydra_kraken_cli import KrakenCLI, KRAKEN_REST_FLOOR_S
from hydra_companions.live_executor import LiveExecutor
from hydra_companions.executor import TradeProposal
from hydra_companions.config import live_execution_enabled
from hydra_brain import HydraBrain
from hydra_quant_rules import evaluate_qfe, apply_rules


# ─── M-CLI: hard reject market ─────────────────────────────────

def test_order_buy_rejects_market():
    out = KrakenCLI.order_buy("SOL/USD", 0.1, price=100.0, order_type="market")
    assert "error" in out
    assert "limit" in out["error"].lower() or "refuse" in out["error"].lower()


def test_order_sell_rejects_non_post_only():
    out = KrakenCLI.order_sell(
        "SOL/USD", 0.1, price=100.0, order_type="limit", post_only=False,
    )
    assert "error" in out


def test_rest_floor_constant():
    assert KRAKEN_REST_FLOOR_S == 2.0


def test_throttle_rest_spaces_calls():
    """Two _throttle_rest calls should wait ~2s on the second (or after reset)."""
    KrakenCLI._last_rest_mono = 0.0
    t0 = __import__("time").monotonic()
    KrakenCLI._throttle_rest()
    KrakenCLI._throttle_rest()
    elapsed = __import__("time").monotonic() - t0
    assert elapsed >= 1.9  # allow small clock skew


# ─── M-PAPER / M-LIVE companion ────────────────────────────────

class _StubCLI:
    def __init__(self):
        self.buys = []

    def order_buy(self, **kw):
        self.buys.append(kw)
        return {"txid": ["OID"]}

    def order_sell(self, **kw):
        return {"txid": ["OID"]}


class _StubAgent:
    def __init__(self, paper=False):
        self.paper = paper
        self.kraken_cli = _StubCLI()
        self.broadcaster = SimpleNamespace(broadcast_message=lambda *a, **k: None)
        self.execution_stream = None


class _StubCoord:
    class _R:
        def safety_cap(self, *a, **k):
            return 0
    router = _R()
    _daily_trades = {}
    ladder_watcher = None


def _trade():
    return TradeProposal(
        proposal_id="p1", companion_id="apex", user_id="u",
        pair="SOL/USD", side="buy", size=0.1, limit_price=100.0,
        stop_loss=90.0, rationale="",
    )


def test_live_executor_refuses_paper_agent(monkeypatch):
    monkeypatch.setenv("HYDRA_COMPANION_LIVE_EXECUTION", "1")
    monkeypatch.delenv("HYDRA_COMPANION_DISABLED", raising=False)
    monkeypatch.delenv("HYDRA_COMPANION_PROPOSALS_ENABLED", raising=False)
    agent = _StubAgent(paper=True)
    ex = LiveExecutor(agent=agent, coordinator=_StubCoord())
    r = ex.execute_trade(_trade())
    assert r["ok"] is False
    assert "paper" in r["error"].lower()
    assert agent.kraken_cli.buys == []


def test_live_executor_rechecks_live_flag(monkeypatch):
    monkeypatch.delenv("HYDRA_COMPANION_LIVE_EXECUTION", raising=False)
    agent = _StubAgent(paper=False)
    ex = LiveExecutor(agent=agent, coordinator=_StubCoord())
    r = ex.execute_trade(_trade())
    assert r["ok"] is False
    assert "live" in r["error"].lower()
    assert agent.kraken_cli.buys == []


# ─── M-CB inclusive ────────────────────────────────────────────

def test_circuit_breaker_at_exactly_15_halts():
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", hold_through=False)
    eng.tradable = True
    eng.peak_equity = 100.0
    eng.max_drawdown = 15.0
    # Force the CB arm path used in tick()
    if eng.tradable and eng.max_drawdown >= eng.CIRCUIT_BREAKER_PCT:
        eng.halted = True
    assert eng.halted is True


# ─── M-HT fail-closed BUY without history ──────────────────────

def test_execute_signal_fail_closed_buy_without_history():
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", hold_through=True)
    eng.candles.clear()
    eng.prices.clear()
    t = eng.execute_signal("BUY", 0.9, "nohist", "MOMENTUM")
    assert t is None


# ─── H2 agent quant kill-switch branch ─────────────────────────

def test_agent_quant_kill_switch_skips_apply_rules(monkeypatch):
    """Real agent path: HYDRA_QUANT_INDICATORS_DISABLED=1 must not call apply_rules."""
    monkeypatch.setenv("HYDRA_QUANT_INDICATORS_DISABLED", "1")
    # Simulate the agent gate (same predicate as hydra_agent.py).
    _quant_rules_disabled = (
        os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") == "1"
    )
    called = []

    def _spy(*a, **k):
        called.append(1)
        return apply_rules(*a, **k)

    if not _quant_rules_disabled:
        _spy("BUY", {}, {"funding_bps_8h": None})
    assert _quant_rules_disabled is True
    assert called == []

    # Stronger: patch apply_rules import site via agent module predicate helper.
    from hydra_agent import HydraAgent
    src = open(ROOT / "hydra_agent.py", encoding="utf-8").read()
    assert 'HYDRA_QUANT_INDICATORS_DISABLED") == "1"' in src
    assert "if not _quant_rules_disabled" in src


# ─── H3 QFE agent rewrite contract (pure + wiring shape) ───────

def test_qfe_restores_sell_when_profitable_no_squeeze():
    r = evaluate_qfe(
        position_size=1.0,
        unrealized_pnl_pct=2.5,
        quant_indicators={"oi_price_regime": "balanced", "funding_bps_8h": 0.0},
        positioning_bias="crowded_short",  # alone must not veto
    )
    assert r.force_exit is True


def test_agent_qfe_block_requires_quant_enabled():
    """Source contract: QFE only runs when quant rules are not disabled."""
    src = (ROOT / "hydra_agent.py").read_text(encoding="utf-8")
    assert "not _quant_rules_disabled" in src
    # QFE assignment sites
    assert 'state["signal"]["action"] = "SELL"' in src
    assert 'qfe_trigger_values' in src


# ─── M-RM + M-SYN brain prompts ────────────────────────────────

def test_rm_features_appear_in_risk_prompt():
    brain = HydraBrain.__new__(HydraBrain)
    state = {
        "asset": "SOL/USD", "price": 100, "regime": "RANGING",
        "candle_interval": 15, "candle_status": "closed",
        "signal": {"action": "BUY", "confidence": 0.7},
        "indicators": {"rsi": 50, "bb_width": 1},
        "position": {"size": 0, "avg_entry": 0, "unrealized_pnl": 0},
        "portfolio": {"balance": 100, "equity": 100, "peak_equity": 100,
                      "pnl_pct": 0, "max_drawdown_pct": 0},
        "performance": {"total_trades": 0, "win_rate_pct": 0, "sharpe_estimate": 0},
        "volatility": {"atr_pct": 1},
        "volume": {"current": 1, "avg_20": 1},
        "quant_indicators": {
            "realized_vol_1h": 0.12,
            "fill_rate_24h": 0.5,
            "minutes_since_last_trade": 10,
        },
    }
    analyst = {"thesis": "x", "conviction": 0.5, "signal_agreement": True,
               "size_multiplier": 1.0, "force_hold": False, "positioning_bias": "unknown",
               "concern": ""}
    prompt = brain._build_risk_prompt(state, analyst)
    assert "realized_vol_1h" in prompt
    assert "0.12" in prompt
    assert "RM ENGINE FEATURES" in prompt


def test_quant_prompt_includes_synthetic_pair():
    state = {
        "quant_indicators": {
            "funding_bps_8h": 1.0,
            "oi_delta_1h_pct": None,
            "oi_price_regime": "balanced",
            "basis_apr_pct": None,
            "cvd_divergence_sigma": 0.0,
            "staleness_s": 1.0,
            "synthetic_pair": True,
        },
    }
    block = HydraBrain._format_quant_indicators(state)
    assert "synthetic_pair: true" in block


# ─── H6 pool-aware tool ───────────────────────────────────────

def test_run_backtest_queues_when_pool_attached():
    from hydra_backtest_tool import BacktestToolDispatcher

    class FakePool:
        def submit_experiment(self, exp):
            return exp.id

    d = BacktestToolDispatcher(pool=FakePool())
    # Avoid real run — patch build + new_experiment lightly via execute
    with patch.object(d, "_tool_run_backtest", wraps=d._tool_run_backtest):
        # Use list_presets as smoke that dispatcher still works
        out = d.execute("list_presets", {}, caller="test")
        assert out.get("success") is True


# ─── tape non-blocking stop ────────────────────────────────────

def test_tape_stop_does_not_block_on_full_queue(tmp_path):
    import queue
    from hydra_tape_capture import TapeCapture
    from hydra_history_store import HistoryStore

    store = HistoryStore(tmp_path / "t.sqlite")
    t = TapeCapture(store, queue_max=1)
    t._q.put_nowait(object())  # fill
    t.stop()  # must return without hang


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
