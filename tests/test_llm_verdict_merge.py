"""One rule for applying the LLM's verdict, on fresh and cached ticks.

Regressions:
  * An OVERRIDE of an engine SELL to BUY opened a long sized on the bearish
    signal's confidence (the LLM opened a position the engine never signalled).
  * The fresh tick placed a BUY on CONFIRM/ADJUST with final_signal HOLD and
    the same-candle cache then vetoed it — place, cancel, churn.
  * The same-candle cache was keyed on the candle only, so a BUY that
    appeared after a SELL deliberation inherited the SELL's verdict unseen.
  * HYDRA_QUANT_INDICATORS_DISABLED returned before the cached veto, letting
    a BUY the LLM had vetoed through on the next tick.
  * A SELL sized 0 by the LLM was silently refused and QFE never saw it.
  * ADJUST replaced the signal reason, erasing the "extreme overbought"
    marker the ride-trend rail needs, so a confirmed exit became a HOLD.
  * An LLM call on a SELL with no inventory could only cost money (or flip).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import HydraAgent, merge_llm_verdict
from hydra_brain import BrainDecision
from hydra_engine import HydraEngine

PAIR = "BTC/USD"
FRESH_QI = {
    "funding_bps_8h": 5.0, "oi_price_regime": "neutral",
    "cvd_divergence_sigma": 0.1, "basis_apr_pct": 5.0,
    "oi_delta_1h_pct": 0.0, "staleness_s": 10.0,
}


def test_merge_rule_table():
    assert merge_llm_verdict("SELL", "BUY") == "HOLD"     # no unsignalled entry
    assert merge_llm_verdict("BUY", "HOLD") == "HOLD"     # veto
    assert merge_llm_verdict("SELL", "HOLD") == "HOLD"    # veto an exit
    assert merge_llm_verdict("BUY", "SELL") == "SELL"     # entry -> exit
    assert merge_llm_verdict("BUY", "BUY") == "BUY"
    assert merge_llm_verdict("HOLD", "BUY") == "HOLD"     # never from HOLD
    assert merge_llm_verdict("BUY", "maybe") == "BUY"     # unparseable: no change
    assert merge_llm_verdict("BUY", None) == "BUY"


def _decision(action, final, size=1.0, summary="llm"):
    return BrainDecision(
        action=action, final_signal=final, confidence_adj=0.5,
        size_multiplier=size, analyst_reasoning="", risk_reasoning="",
        combined_summary=summary, fallback=False,
    )


def _agent(decision, calls=None):
    agent = HydraAgent.__new__(HydraAgent)
    agent.engines = {PAIR: HydraEngine(initial_balance=1000, asset=PAIR)}

    class _Brain:
        api_available = True

        def deliberate(self, state):
            if calls is not None:
                calls.append(state["signal"]["action"])
            return decision

    agent.brain = _Brain()
    agent._last_ai_decision = {}
    agent._last_brain_candle_ts = {}
    agent._current_portfolio_summary = {}
    agent._portfolio_guidance = None
    agent.ticker_stream = type("T", (), {"latest_ticker": lambda self, pair: {}})()
    agent._build_quant_indicators = lambda pair, state: None
    agent._build_triangle_context = lambda *a, **k: {}
    return agent


def _state(action, reason="engine", size=0.5, avg=100.0, price=100.0, ts=1000.0):
    return {
        "signal": {"action": action, "confidence": 0.8, "reason": reason},
        "price": price,
        "position": {"size": size, "avg_entry": avg},
        "quant_indicators": dict(FRESH_QI),
        "candles": [{"t": ts}],
    }


def test_override_cannot_flip_a_sell_into_a_buy(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _agent(_decision("OVERRIDE", "BUY"))
    state = _state("SELL")
    agent._apply_brain(PAIR, state, {})
    assert state["signal"]["action"] == "HOLD"


def test_confirm_with_hold_verdict_vetoes_the_fresh_buy(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _agent(_decision("CONFIRM", "HOLD"))
    state = _state("BUY", size=0.0, avg=0.0)
    agent._apply_brain(PAIR, state, {})
    assert state["signal"]["action"] == "HOLD"


def test_cache_is_keyed_by_engine_action(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    calls = []
    agent = _agent(_decision("CONFIRM", "SELL"), calls)
    agent._apply_brain(PAIR, _state("SELL", ts=5000.0), {})
    assert calls == ["SELL"]
    buy = _state("BUY", size=0.5, ts=5000.0)
    agent._apply_brain(PAIR, buy, {})
    assert calls == ["SELL", "BUY"]  # deliberated, not replayed


def test_same_question_on_the_same_candle_replays(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    calls = []
    agent = _agent(_decision("OVERRIDE", "HOLD"), calls)
    first = _state("BUY", size=0.0, avg=0.0, ts=7000.0)
    agent._apply_brain(PAIR, first, {})
    second = _state("BUY", size=0.0, avg=0.0, ts=7000.0)
    agent._apply_brain(PAIR, second, {})
    assert calls == ["BUY"]
    assert second["signal"]["action"] == "HOLD"


def test_kill_switch_keeps_the_cached_llm_veto(monkeypatch):
    monkeypatch.setenv("HYDRA_QUANT_INDICATORS_DISABLED", "1")
    agent = _agent(_decision("OVERRIDE", "HOLD"))
    agent._apply_brain(PAIR, _state("BUY", size=0.0, avg=0.0, ts=9000.0), {})
    again = _state("BUY", size=0.0, avg=0.0, ts=9000.0)
    agent._apply_brain(PAIR, again, {})
    assert again["signal"]["action"] == "HOLD"


def test_zero_sized_profitable_exit_is_rescued_by_qfe(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _agent(_decision("CONFIRM", "SELL", size=0.0))
    state = _state("SELL", size=1.0, avg=100.0, price=103.0)  # +3% in profit
    agent._apply_brain(PAIR, state, {})
    assert state["signal"]["action"] == "SELL"
    assert state["signal"]["reason"].startswith("[QFE PROFIT EXIT]")
    assert state["ai_decision"]["size_multiplier"] == 1.0


def test_zero_sized_losing_exit_is_an_explicit_hold(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _agent(_decision("CONFIRM", "SELL", size=0.0))
    state = _state("SELL", size=1.0, avg=100.0, price=97.0)
    agent._apply_brain(PAIR, state, {})
    assert state["signal"]["action"] == "HOLD"
    assert "exit sized 0" in state["signal"]["reason"]


def test_adjust_keeps_the_engine_reason_for_the_rail(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    agent = _agent(_decision("ADJUST", "SELL", size=0.6))
    reason = "Momentum fading: RSI 88.0 > 85 extreme overbought"
    state = _state("SELL", reason=reason, size=1.0)
    agent._apply_brain(PAIR, state, {})
    assert state["signal"]["action"] == "SELL"
    assert "extreme overbought" in state["signal"]["reason"]


def test_sell_with_no_inventory_skips_the_llm(monkeypatch):
    monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
    calls = []
    agent = _agent(_decision("OVERRIDE", "BUY"), calls)
    state = _state("SELL", size=0.0, avg=0.0)
    agent._apply_brain(PAIR, state, {})
    assert calls == []
    assert state["signal"]["action"] == "SELL"  # inert: engine cannot sell nothing


def test_coordinator_does_not_relabel_a_protected_flatten():
    states = {
        "SOL/USD": {"signal": {"action": "SELL", "confidence": 0.7,
                               "reason": "HOLD_THROUGH:force_flatten|Defensive"}},
        "BTC/USD": {"signal": {"action": "BUY", "confidence": 0.7, "reason": "entry"}},
    }
    overrides = {
        "SOL/USD": {"action": "ADJUST", "signal": "BUY", "confidence_adj": 0.8,
                    "reason": "Cross-pair: BTC recovering"},
        "BTC/USD": {"action": "ADJUST", "signal": "BUY", "confidence_adj": 0.9,
                    "reason": "Cross-pair: confluence", "swap": None},
    }
    swaps = HydraAgent._apply_cross_pair_overrides(states, overrides)
    assert states["SOL/USD"]["signal"]["reason"].startswith("HOLD_THROUGH:force_flatten")
    assert states["SOL/USD"]["signal"]["action"] == "SELL"
    assert states["BTC/USD"]["signal"]["reason"].startswith("[CROSS-PAIR]")
    assert swaps == []
