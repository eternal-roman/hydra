"""Deterministic-only deployments must still place orders.

With no LLM key configured `HydraAgent.brain` is None. The deterministic
R1-R11 + QFE guardrails are the layer that is supposed to hold when the LLMs
are unavailable — and the engine must still execute the signals it generates.

v2.32.0 added the brain-free guardrail branch to the Phase 2 loop as an
`elif` placed above the `else` that owned `all_states[pair] = state`. An
actionable BUY/SELL with `brain is None` therefore ran the guardrails and was
then dropped from `all_states`, and Phase 2.5 (`state = all_states.get(pair);
if not state: continue`) skipped the pair entirely. The result was an agent
that generated signals, logged them, and placed NO orders at all — not
entries and not exits, so a losing position rode through every flatten.

These tests drive the real `run()` loop in offline demo mode.
"""
from __future__ import annotations

import sys
import pathlib
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent  # noqa: E402


def _agent(port: int) -> HydraAgent:
    agent = HydraAgent(
        pairs=["BTC/USD"],
        initial_balance=1000.0,
        interval_seconds=1,
        duration_seconds=1,
        ws_port=port,
        demo=True,
    )
    # Explicit: a deployment with no API keys configured.
    agent.brain = None
    return agent


def _force_signal(agent: HydraAgent, action: str) -> None:
    """Make every engine tick emit `action` with executable confidence."""
    real_tick = {}
    for pair, engine in agent.engines.items():
        real_tick[pair] = engine.tick

        def patched(*a, _pair=pair, _real=real_tick[pair], **kw):
            state = _real(*a, **kw)
            if state:
                state["signal"] = {
                    "action": action,
                    "confidence": 0.95,
                    "reason": "forced by test",
                    "strategy": "MOMENTUM",
                }
            return state

        engine.tick = patched


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_actionable_signal_executes_without_brain(action):
    """An actionable signal must reach execute_signal when brain is None."""
    agent = _agent(port=18781 if action == "BUY" else 18782)
    _force_signal(agent, action)

    calls = []
    engine = agent.engines["BTC/USD"]
    real_execute = engine.execute_signal

    # The agent calls this with keyword arguments, so stay signature-agnostic.
    def spy(*a, **kw):
        calls.append(kw.get("action") if "action" in kw else (a[0] if a else None))
        return real_execute(*a, **kw)

    engine.execute_signal = spy

    with mock.patch.object(agent, "_save_snapshot"):
        agent.run()

    assert calls, (
        f"brain-free agent never called execute_signal for a {action} signal "
        f"— Phase 2.5 skipped the pair, so no order would be placed"
    )
    assert action in calls


def test_guardrails_still_run_without_brain():
    """The brain-free path must still evaluate the deterministic rules."""
    agent = _agent(port=18783)
    _force_signal(agent, "BUY")

    seen = []
    real_guardrails = agent._apply_quant_guardrails

    def spy(pair, state):
        seen.append(pair)
        return real_guardrails(pair, state)

    agent._apply_quant_guardrails = spy

    with mock.patch.object(agent, "_save_snapshot"):
        agent.run()

    assert seen, "deterministic guardrails did not run on the brain-free path"


def _print_state(ai=None, action="HOLD"):
    state = {
        "signal": {"action": action, "confidence": 0.72, "reason": "test"},
        "portfolio": {"equity": 1000.0, "pnl_pct": 0.1, "max_drawdown_pct": 1.2},
        "position": {"size": 0.0, "avg_entry": 0.0, "unrealized_pnl": 0.0},
        "price": 68000.0,
        "regime": "RANGING",
        "strategy": "MEAN_REVERSION",
    }
    if ai is not None:
        state["ai_decision"] = ai
    return state


def test_rules_only_payload_includes_final_signal(monkeypatch):
    """Brain-free guardrails must stamp final_signal (dashboard pill + print)."""
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    from hydra_engine import HydraEngine

    agent = HydraAgent.__new__(HydraAgent)
    agent.brain = None
    agent.engines = {"BTC/USD": HydraEngine(initial_balance=1000, asset="BTC/USD")}
    state = {
        "signal": {"action": "BUY", "confidence": 0.8, "reason": "momentum"},
        "price": 100.0,
        "position": {"size": 0.0, "avg_entry": 0.0},
        "quant_indicators": {
            "funding_bps_8h": 5.0, "oi_price_regime": "neutral",
            "cvd_divergence_sigma": 0.1, "basis_apr_pct": 5.0,
            "staleness_s": 10.0,
        },
    }
    agent._apply_quant_guardrails("BTC/USD", state)
    ai = state["ai_decision"]
    assert ai["action"] == "RULES_ONLY"
    assert ai["final_signal"] == state["signal"]["action"]
    assert ai.get("summary") or ai.get("combined_summary")


def test_print_tick_status_rules_only_does_not_raise(capsys):
    """RULES_ONLY (and legacy missing keys) must not KeyError in tick print."""
    agent = HydraAgent.__new__(HydraAgent)

    agent._print_tick_status("BTC/USD", _print_state({
        "action": "RULES_ONLY",
        "final_signal": "HOLD",
        "combined_summary": "Deterministic guardrails only — no LLM configured.",
        "brain_available": False,
    }))
    out = capsys.readouterr().out
    assert "RULES_ONLY" in out
    assert "HOLD" in out
    assert "Deterministic guardrails" in out

    # Pre-fix payload: no final_signal / summary / fallback.
    agent._print_tick_status("BTC/USD", _print_state({
        "action": "RULES_ONLY",
        "combined_summary": "legacy rules-only payload",
    }))
    out2 = capsys.readouterr().out
    assert "RULES_ONLY" in out2
    assert "legacy rules-only" in out2

    # Completely sparse cached decision.
    agent._print_tick_status("ETH/USD", _print_state({}))
    capsys.readouterr()  # must not raise
