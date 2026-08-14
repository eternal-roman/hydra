"""Money-safety gate regressions: CLI market-order hard reject, global REST
throttle, companion paper/live refusal, inclusive circuit-breaker boundary,
hold-through fail-closed, quant kill-switch, RM prompt features, pool enqueue,
and tape non-blocking stop."""
from __future__ import annotations

import os
import sys
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine
from hydra_kraken_cli import KrakenCLI, KRAKEN_REST_FLOOR_S
from hydra_companions.live_executor import LiveExecutor
from hydra_companions.executor import TradeProposal
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

def _flat_candles(eng, px: float, n: int = 55, start: int = 0):
    for i in range(start, start + n):
        eng.ingest_candle({
            "open": px, "high": px * 1.001, "low": px * 0.999,
            "close": px, "volume": 10.0,
            "timestamp": 1700000000 + i * 900,
        })


def test_circuit_breaker_at_exactly_15_halts():
    """Drive the REAL tick() path at exactly 15.0% drawdown and assert the
    inclusive (>=) comparison halts (an earlier version re-implemented the
    halt branch inline and could never fail)."""
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/USD", hold_through=False)
    _flat_candles(eng, 100.0)
    eng.peak_equity = 10000.0
    eng.balance = 8500.0  # flat position → equity 8500 → exactly 15.0% DD
    eng.tick(generate_only=True)
    assert eng.max_drawdown == pytest.approx(15.0)
    assert eng.halted is True
    assert "CIRCUIT BREAKER" in eng.halt_reason


def test_circuit_breaker_below_15_does_not_halt():
    """Negative boundary: 14.99% drawdown must NOT halt (regression guard
    against >= being tightened to > or the threshold drifting)."""
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/USD", hold_through=False)
    _flat_candles(eng, 100.0)
    eng.peak_equity = 10000.0
    eng.balance = 8501.0  # 14.99% DD
    eng.tick(generate_only=True)
    assert eng.max_drawdown < 15.0
    assert eng.halted is False


# ─── M-HT fail-closed BUY without history ──────────────────────

def test_execute_signal_fail_closed_buy_without_history():
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", hold_through=True)
    eng.candles.clear()
    eng.prices.clear()
    t = eng.execute_signal("BUY", 0.9, "nohist", "MOMENTUM")
    assert t is None


# ─── H2 agent quant kill-switch branch ─────────────────────────

def _guardrail_agent():
    """Minimal real HydraAgent for driving _apply_quant_guardrails."""
    from hydra_agent import HydraAgent
    from hydra_engine import HydraEngine
    agent = HydraAgent.__new__(HydraAgent)
    agent.brain = None
    agent.engines = {"BTC/USD": HydraEngine(initial_balance=1000, asset="BTC/USD")}
    return agent


def _guardrail_state():
    return {
        "signal": {"action": "BUY", "confidence": 0.8, "reason": "momentum"},
        "price": 100.0,
        "position": {"size": 0.0, "avg_entry": 0.0},
        "quant_indicators": {
            "funding_bps_8h": 140.0, "oi_price_regime": "neutral",
            "cvd_divergence_sigma": 0.1, "basis_apr_pct": 5.0,
            "staleness_s": 10.0,
        },
    }


def test_agent_quant_kill_switch_skips_apply_rules(monkeypatch):
    """Real agent path: HYDRA_QUANT_INDICATORS_DISABLED=1 must not call apply_rules.

    Rewritten in v2.32.1. The previous version re-implemented the gate inside
    the test and asserted on its own simulation — `called == []` was
    unfalsifiable and hydra_agent was never imported, so deleting the entire
    kill-switch branch from production left this green. It now spies on the
    real `hydra_quant_rules.apply_rules` and drives the real
    `HydraAgent._apply_quant_guardrails`, with a POSITIVE CONTROL below so the
    assertion can actually fail.
    """
    import hydra_quant_rules
    monkeypatch.setenv("HYDRA_QUANT_INDICATORS_DISABLED", "1")

    called = []
    real = hydra_quant_rules.apply_rules

    def _spy(*a, **k):
        called.append(1)
        return real(*a, **k)

    monkeypatch.setattr(hydra_quant_rules, "apply_rules", _spy)

    state = _guardrail_state()
    _guardrail_agent()._apply_quant_guardrails("BTC/USD", state)

    assert called == [], "kill switch set but apply_rules was still called"
    # And the signal must be left untouched — no force_hold applied.
    assert state["signal"]["action"] == "BUY"


def test_agent_quant_rules_DO_run_when_switch_unset(monkeypatch):
    """POSITIVE CONTROL for the test above — this is what makes it falsifiable.

    With the switch unset the same path MUST call apply_rules and MUST act on
    the result (funding at +140 bps trips R1 and force_holds the BUY).
    """
    import hydra_quant_rules
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)

    called = []
    real = hydra_quant_rules.apply_rules

    def _spy(*a, **k):
        called.append(1)
        return real(*a, **k)

    monkeypatch.setattr(hydra_quant_rules, "apply_rules", _spy)

    state = _guardrail_state()
    _guardrail_agent()._apply_quant_guardrails("BTC/USD", state)

    assert called, "guardrails did not run with the kill switch unset"
    assert state["signal"]["action"] == "HOLD"
    assert state["ai_decision"]["rules_force_hold"] is True


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
            # Must be the *_pct names the agent writes (this test previously
            # fed the un-suffixed name the buggy formatter read, encoding the
            # key mismatch instead of catching it).
            "realized_vol_1h_pct": 0.12,
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

def test_run_backtest_queues_when_pool_attached(tmp_path):
    """With a pool attached, run_backtest must ENQUEUE via
    pool.submit_experiment (never run inline) and surface the experiment id.
    The prior version of this test never touched the pool."""
    from hydra_backtest_tool import BacktestToolDispatcher

    class FakePool:
        def __init__(self):
            self.submitted = []

        def submit_experiment(self, exp):
            self.submitted.append(exp)
            return exp.id

    pool = FakePool()
    d = BacktestToolDispatcher(store_root=tmp_path / "exp", pool=pool)
    with patch("hydra_backtest_tool.run_experiment") as run_inline:
        out = d.execute(
            "run_backtest",
            {"preset": "default",
             "hypothesis": "pool enqueue regression pin"},
            caller="test",
        )
    assert out.get("success") is True, out
    data = out.get("data") or {}
    assert data.get("status") == "queued"
    assert len(pool.submitted) == 1
    assert data.get("experiment_id") == pool.submitted[0].id
    run_inline.assert_not_called()  # inline path must not fire in pool mode


# ─── tape non-blocking stop ────────────────────────────────────

def test_tape_stop_does_not_block_on_full_queue(tmp_path):
    from hydra_tape_capture import TapeCapture
    from hydra_history_store import HistoryStore

    store = HistoryStore(tmp_path / "t.sqlite")
    t = TapeCapture(store, queue_max=1)
    t._q.put_nowait(object())  # fill
    t.stop()  # must return without hang


def test_keep_brain_does_not_wipe_cached_override(monkeypatch):
    """Same-candle path: rules re-run, cached OVERRIDE/size survive."""
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _guardrail_agent()
    state = _guardrail_state()
    state["ai_decision"] = {
        "action": "OVERRIDE",
        "final_signal": "HOLD",
        "size_multiplier": 0.3,
        "combined_summary": "RM de-risk",
    }
    agent._apply_quant_guardrails("BTC/USD", state, keep_brain=True)
    assert state["signal"]["action"] == "HOLD"
    assert state["ai_decision"]["action"] == "OVERRIDE"
    assert state["ai_decision"]["size_multiplier_brain"] == 0.3
    assert state["ai_decision"].get("brain_available") is not False


def test_coerce_bool_does_not_treat_false_string_as_true():
    from hydra_brain import _coerce_bool
    assert _coerce_bool("false") is False
    assert _coerce_bool("true") is True
    assert _coerce_bool(False) is False
    assert _coerce_bool(True) is True


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
