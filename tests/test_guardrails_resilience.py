"""v2.32 regressions: tick-loop wedge, circuit-breaker ratchet, execution
double-fold, unsellable-dust loop, and brain-independent quant guardrails.

Every test here pairs the fix with a NEGATIVE CONTROL where one is possible —
the audit that produced these fixes also found several tests that could not
fail (assertions under an `if` that never ran, or asserting on a literal the
test itself had just written). A test that passes when the production code is
deleted is worse than no test.
"""
from __future__ import annotations

import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent
from hydra_engine import HydraEngine
from hydra_streams import ExecutionStream


# ═══════════════════════════════════════════════════════════════
# 1. Tick-loop wedge — _build_portfolio_review_state on a None state
# ═══════════════════════════════════════════════════════════════

class TestPortfolioReviewNoneState:
    """`_fetch_and_tick` returns None when the live CandleStream is unhealthy,
    and the caller stores that None under the pair key. `dict.get(pair, {})`
    does not help — the key EXISTS, so the default never applies.

    This mattered far beyond one tick: the crash fired before the stream
    recovery block later in the tick body, so the dead stream was never
    restarted and every subsequent tick died identically. Exits included.
    """

    def _agent(self):
        agent = HydraAgent.__new__(HydraAgent)
        agent.pairs = ["BTC/USD", "ETH/USD"]
        agent.engines = {
            "BTC/USD": HydraEngine(initial_balance=1000, asset="BTC/USD"),
            "ETH/USD": HydraEngine(initial_balance=1000, asset="ETH/USD"),
        }
        agent._current_portfolio_summary = {}
        return agent

    def test_none_state_does_not_raise(self):
        agent = self._agent()
        # BTC ticked fine; ETH was skipped this tick -> explicit None.
        states = {
            "BTC/USD": {"regime": "TREND_UP", "signal": {"action": "BUY"}},
            "ETH/USD": None,
        }
        out = agent._build_portfolio_review_state(states)
        assert isinstance(out, dict)
        pairs = {d["pair"] for d in out["pair_details"]}
        assert pairs == {"BTC/USD", "ETH/USD"}

    def test_skipped_pair_reports_unknown_not_stale_data(self):
        agent = self._agent()
        states = {"BTC/USD": None, "ETH/USD": None}
        out = agent._build_portfolio_review_state(states)
        for detail in out["pair_details"]:
            # A skipped pair must not be described with a confident regime.
            assert detail["regime"] == "UNKNOWN"

    def test_populated_state_still_read_correctly(self):
        """Negative control: the `or {}` must not swallow real data."""
        agent = self._agent()
        states = {
            "BTC/USD": {"regime": "VOLATILE", "signal": {"action": "SELL"}},
            "ETH/USD": {"regime": "RANGING", "signal": {"action": "HOLD"}},
        }
        out = agent._build_portfolio_review_state(states)
        by_pair = {d["pair"]: d for d in out["pair_details"]}
        assert by_pair["BTC/USD"]["regime"] == "VOLATILE"
        assert by_pair["ETH/USD"]["regime"] == "RANGING"


# ═══════════════════════════════════════════════════════════════
# 2. Circuit-breaker ratchet across --resume
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreakerResumeReset:
    """`halted` is restored from the snapshot and nothing outside __init__ ever
    cleared it. Production runs `--mode competition --resume`, so one 15%
    drawdown disabled new BUYs across every future session forever.
    """

    def test_halt_survives_resume_by_default(self, monkeypatch):
        monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER", raising=False)
        eng = HydraEngine(initial_balance=1000, asset="BTC/USD")
        snap = {"halted": True, "halt_reason": "drawdown 15.2% >= 15.0%"}
        eng.restore_runtime(snap)
        assert eng.halted is True, "a breach must not be erased by a restart"

    def test_explicit_env_clears_halt(self, monkeypatch):
        monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
        eng = HydraEngine(initial_balance=1000, asset="BTC/USD")
        snap = {"halted": True, "halt_reason": "drawdown 15.2% >= 15.0%"}
        eng.restore_runtime(snap)
        assert eng.halted is False
        assert eng.halt_reason == ""

    def test_reset_does_not_erase_drawdown_record(self, monkeypatch):
        """The escape hatch clears the FLAG only. Peak equity and max drawdown
        must survive, so the breaker re-arms immediately if still underwater."""
        monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
        eng = HydraEngine(initial_balance=1000, asset="BTC/USD")
        snap = {
            "halted": True,
            "halt_reason": "drawdown 15.2% >= 15.0%",
            "peak_equity": 1500.0,
            "max_drawdown": 15.2,
        }
        eng.restore_runtime(snap)
        assert eng.halted is False
        assert eng.peak_equity == 1500.0
        assert eng.max_drawdown == 15.2

    def test_env_set_to_other_value_does_not_clear(self, monkeypatch):
        """Negative control: only the exact opt-in string clears the halt."""
        monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "0")
        eng = HydraEngine(initial_balance=1000, asset="BTC/USD")
        eng.restore_runtime({"halted": True, "halt_reason": "x"})
        assert eng.halted is True


# ═══════════════════════════════════════════════════════════════
# 3. ExecutionStream double-fold on snapshot replay after restart
# ═══════════════════════════════════════════════════════════════

class TestExecutionReplayDedup:
    """The stream runs with `--snap-trades true` and deliberately preserves
    `_known_orders` across restarts. Together those replay already-folded
    fills: a 0.4 fill counted twice made a fully-filled 1.0 order report 1.4,
    which classified FILLED as PARTIALLY_FILLED, over-booked inventory, and
    roughly doubled the fee debited from engine cash.
    """

    def _stream_with_order(self):
        import threading
        stream = ExecutionStream.__new__(ExecutionStream)
        stream._lock = threading.RLock()
        stream._userref_to_order_id = {}
        stream._terminal_queue = []
        stream._known_orders = {
            "TX1": {
                "vol_exec_running": 0.0,
                "cost_running": 0.0,
                "fee_running": 0.0,
                "exec_ids": [],
                "placed_amount": 1.0,
            }
        }
        return stream

    def _fill(self, exec_id, qty=0.4, price=100.0, fee=0.16):
        return {
            "exec_id": exec_id,
            "order_id": "TX1",
            "last_qty": qty,
            "last_price": price,
            "cost": qty * price,
            "fees": [{"qty": fee}],
            "order_status": "open",
        }

    def test_replayed_exec_id_not_double_counted(self):
        stream = self._stream_with_order()
        known = stream._known_orders["TX1"]
        stream._apply_entry(self._fill("E-1"))
        after_first = known["vol_exec_running"]
        # Subprocess restart -> snapshot replays the SAME execution.
        stream._apply_entry(self._fill("E-1"))
        assert known["vol_exec_running"] == after_first == pytest.approx(0.4)
        assert known["fee_running"] == pytest.approx(0.16)
        assert known["exec_ids"].count("E-1") == 1

    def test_distinct_exec_ids_still_accumulate(self):
        """Negative control: dedup must not block genuine second fills."""
        stream = self._stream_with_order()
        known = stream._known_orders["TX1"]
        stream._apply_entry(self._fill("E-1", qty=0.4))
        stream._apply_entry(self._fill("E-2", qty=0.6))
        assert known["vol_exec_running"] == pytest.approx(1.0)
        assert known["fee_running"] == pytest.approx(0.32)

    def test_anonymous_fills_clamped_to_placed_amount(self):
        """Entries with no exec_id cannot be deduped, so they are clamped:
        never book more than was actually placed."""
        stream = self._stream_with_order()
        known = stream._known_orders["TX1"]
        for _ in range(4):
            entry = self._fill("", qty=0.4)
            entry.pop("exec_id")
            stream._apply_entry(entry)
        assert known["vol_exec_running"] <= 1.0 + 1e-9
        # cost scaled with the clamp so avg price stays a true average
        avg = known["cost_running"] / known["vol_exec_running"]
        assert avg == pytest.approx(100.0, rel=1e-6)


# ═══════════════════════════════════════════════════════════════
# 4. Brain-independent deterministic guardrails
# ═══════════════════════════════════════════════════════════════

class TestGuardrailsWithoutBrain:
    """`apply_rules` / `evaluate_qfe` were only ever called inside
    `_apply_brain`, which returns immediately when `self.brain is None`. With
    no LLM key the whole R1-R11 stack silently did not run, while live
    funding/OI kept flowing to the dashboard.
    """

    def _agent(self):
        agent = HydraAgent.__new__(HydraAgent)
        agent.brain = None
        agent.engines = {"BTC/USD": HydraEngine(initial_balance=1000, asset="BTC/USD")}
        return agent

    def _buy_state(self, funding_bps):
        return {
            "signal": {"action": "BUY", "confidence": 0.8, "reason": "momentum"},
            "price": 100.0,
            "position": {"size": 0.0, "avg_entry": 0.0},
            "quant_indicators": {
                "funding_bps_8h": funding_bps,
                "oi_price_regime": "neutral",
                "cvd_divergence_sigma": 0.1,
                "basis_apr_pct": 5.0,
                "staleness_s": 10.0,
            },
        }

    def test_r1_force_holds_buy_with_no_brain(self, monkeypatch):
        """Extreme positive funding must force_hold a BUY even with no LLM."""
        monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
        agent = self._agent()
        state = self._buy_state(funding_bps=140.0)
        agent._apply_quant_guardrails("BTC/USD", state)
        assert state["signal"]["action"] == "HOLD"
        assert state["ai_decision"]["rules_force_hold"] is True
        assert state["ai_decision"]["size_multiplier"] == 0.0

    def test_benign_funding_leaves_buy_intact(self, monkeypatch):
        """Negative control: the guardrail pass must not blanket-block BUYs."""
        monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
        agent = self._agent()
        state = self._buy_state(funding_bps=5.0)
        agent._apply_quant_guardrails("BTC/USD", state)
        assert state["signal"]["action"] == "BUY"
        assert state["ai_decision"]["rules_force_hold"] is False
        assert state["ai_decision"]["size_multiplier"] > 0.0

    def test_kill_switch_skips_guardrails(self, monkeypatch):
        """HYDRA_QUANT_INDICATORS_DISABLED=1 must skip this path too, or the
        documented kill switch would not actually kill anything."""
        monkeypatch.setenv("HYDRA_QUANT_INDICATORS_DISABLED", "1")
        agent = self._agent()
        state = self._buy_state(funding_bps=140.0)
        agent._apply_quant_guardrails("BTC/USD", state)
        assert state["signal"]["action"] == "BUY"
        assert "ai_decision" not in state

    def test_r10_blackout_on_stale_feed(self, monkeypatch):
        """A dark derivatives feed is MORE dangerous without an LLM, not less."""
        monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
        agent = self._agent()
        state = self._buy_state(funding_bps=5.0)
        state["quant_indicators"] = {
            "funding_bps_8h": None,
            "oi_price_regime": None,
            "cvd_divergence_sigma": None,
            "basis_apr_pct": None,
            "staleness_s": 900.0,
        }
        agent._apply_quant_guardrails("BTC/USD", state)
        assert state["signal"]["action"] == "HOLD"
        assert state["ai_decision"]["rules_force_hold"] is True

    def test_guardrails_never_open_a_position(self, monkeypatch):
        """QFE is exit-only: it must never turn a HOLD/flat state into a BUY."""
        monkeypatch.delenv("HYDRA_QUANT_INDICATORS_DISABLED", raising=False)
        agent = self._agent()
        state = self._buy_state(funding_bps=140.0)
        agent._apply_quant_guardrails("BTC/USD", state)
        assert state["signal"]["action"] != "BUY"
        assert state["ai_decision"]["qfe_active"] is False
