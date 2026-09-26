"""One working order per pair, and a fill must dirty the snapshot.

A second execute_signal while a PLACED row is still open books a fill
the exchange has not made. The first order's true-up then restores an
older snapshot and the second order is left live against the wrong book.
"""
from __future__ import annotations

import sys
import pathlib
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent, resting_order_decision  # noqa: E402


def _agent(port: int) -> HydraAgent:
    agent = HydraAgent(
        pairs=["BTC/USD"],
        initial_balance=1000.0,
        interval_seconds=1,
        duration_seconds=3,
        ws_port=port,
        demo=True,
    )
    agent.brain = None
    return agent


def _placed(agent: HydraAgent, side: str = "BUY", oid: str = "OID-1") -> dict:
    eng = agent.engines["BTC/USD"]
    flat = eng.snapshot_position()
    return {
        "pair": "BTC/USD",
        "side": side,
        "order_ref": {"order_id": oid, "order_userref": 7},
        "intent": {"amount": 0.01, "limit_price": 100.0},
        "pre_trade_snapshot": flat,
        "lifecycle": {
            "state": "PLACED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": 0.0,
            "final_at": None,
            "terminal_reason": None,
            "exec_ids": [],
        },
    }


def test_resting_decision_table():
    assert resting_order_decision(None, "BUY") == "place"
    assert resting_order_decision("BUY", "BUY") == "skip"
    assert resting_order_decision("BUY", "HOLD") == "skip"
    assert resting_order_decision("SELL", "SELL") == "skip"
    assert resting_order_decision("BUY", "SELL") == "cancel"
    assert resting_order_decision("SELL", "BUY") == "cancel"
    assert resting_order_decision("SELL", "BUY", buy_blocked=True) == "skip"


def test_same_side_does_not_execute_or_cancel():
    agent = _agent(18791)
    eng = agent.engines["BTC/USD"]
    eng.position.size = 0.25
    entry = _placed(agent, "BUY")
    # Snapshot inside the row is the flat book taken before this phantom.
    agent.order_journal.append(entry)
    assert agent._hold_for_resting_order("BTC/USD", "BUY") is True
    assert entry["lifecycle"]["state"] == "PLACED"
    assert eng.position.size == 0.25


def test_opposite_on_paper_restores_snapshot_and_does_not_place():
    agent = _agent(18792)
    eng = agent.engines["BTC/USD"]
    entry = _placed(agent, "BUY")
    agent.order_journal.append(entry)
    eng.position.size = 0.25
    eng.position.avg_entry = 100.0
    assert agent._hold_for_resting_order("BTC/USD", "SELL") is True
    assert eng.position.size == 0.0
    assert entry["lifecycle"]["state"] == "CANCELLED_UNFILLED"
    assert agent._books_dirty is True
    # The row is terminal, so the next tick may trade.
    assert agent._hold_for_resting_order("BTC/USD", "SELL") is False


def test_live_cancel_is_sent_once_and_does_not_roll_back_early():
    agent = _agent(18793)
    agent.paper = False
    eng = agent.engines["BTC/USD"]
    entry = _placed(agent, "BUY", oid="OID-LIVE")
    agent.order_journal.append(entry)
    eng.position.size = 0.25
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch("hydra_agent.KrakenCLI.cancel_order", return_value={"count": 1}) as cancel:
        assert agent._hold_for_resting_order("BTC/USD", "SELL") is True
        assert agent._hold_for_resting_order("BTC/USD", "SELL") is True
    assert cancel.call_count == 1
    assert cancel.call_args[0][0] == "OID-LIVE"
    # Live books wait for the execution stream. The phantom stays until then.
    assert eng.position.size == 0.25
    assert entry["lifecycle"]["state"] == "PLACED"


def test_place_order_refuses_stack_without_a_failure_row():
    agent = _agent(18794)
    agent.order_journal.append(_placed(agent, "BUY"))
    before = len(agent.order_journal)
    ok = agent._place_order(
        "BTC/USD",
        {"action": "BUY", "amount": 0.01, "price": 100.0, "confidence": 0.9},
        {},
    )
    assert ok is False
    assert len(agent.order_journal) == before
    assert agent.order_journal[-1]["lifecycle"]["state"] == "PLACED"


def test_dashboard_journal_keeps_a_working_order_behind_failures():
    agent = _agent(18795)
    agent.order_journal.append(_placed(agent, "BUY", oid="WORKING"))
    for i in range(25):
        agent.order_journal.append({
            "pair": "BTC/USD",
            "side": "BUY",
            "lifecycle": {"state": "PLACEMENT_FAILED"},
            "order_ref": {"order_id": f"F{i}"},
        })
    rows = agent._journal_for_dashboard(20)
    ids = [(e.get("order_ref") or {}).get("order_id") for e in rows]
    assert "WORKING" in ids
    assert len(rows) == 21


def test_book_change_checkpoints_snapshot_without_a_new_row():
    agent = _agent(18796)
    saved = []
    agent._save_snapshot = lambda: saved.append(True)
    agent._books_dirty = True
    start = len(agent.order_journal)
    agent._checkpoint_tick(start, tick=1)
    assert saved == [True]
    assert agent._books_dirty is False
    agent._checkpoint_tick(start, tick=1)
    assert saved == [True]


def test_fill_event_dirties_the_book():
    agent = _agent(18797)
    eng = agent.engines["BTC/USD"]
    entry = _placed(agent, "BUY", oid="OID-FILL")
    agent.order_journal.append(entry)
    agent._books_dirty = False
    agent._apply_execution_event({
        "journal_index": 0,
        "order_id": "OID-FILL",
        "state": "CANCELLED_UNFILLED",
        "vol_exec": 0.0,
        "avg_fill_price": None,
        "fee_quote": 0.0,
        "engine_ref": eng,
        "pre_trade_snapshot": entry["pre_trade_snapshot"],
        "pair": "BTC/USD",
        "side": "BUY",
        "placed_amount": 0.01,
        "terminal_reason": "test",
        "exec_ids": [],
    })
    assert agent._books_dirty is True
    assert entry["lifecycle"]["state"] == "CANCELLED_UNFILLED"


def _force_signal(agent: HydraAgent, action: str) -> None:
    for pair, engine in agent.engines.items():
        real = engine.tick

        def patched(*a, _real=real, **kw):
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


def test_run_loop_does_not_execute_while_an_order_is_working():
    agent = _agent(18798)
    agent.order_journal.append(_placed(agent, "BUY", oid="OID-RUN"))
    _force_signal(agent, "BUY")
    calls = []
    engine = agent.engines["BTC/USD"]
    real_execute = engine.execute_signal

    def spy(*a, **kw):
        calls.append(kw.get("action") if "action" in kw else (a[0] if a else None))
        return real_execute(*a, **kw)

    engine.execute_signal = spy
    with mock.patch.object(agent, "_save_snapshot"):
        agent.run()
    assert calls == []
    assert agent.order_journal[0]["lifecycle"]["state"] == "PLACED"


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_run_loop_still_executes_when_nothing_is_resting(action):
    agent = _agent(18799 if action == "BUY" else 18800)
    _force_signal(agent, action)
    calls = []
    engine = agent.engines["BTC/USD"]
    real_execute = engine.execute_signal

    def spy(*a, **kw):
        calls.append(kw.get("action") if "action" in kw else (a[0] if a else None))
        return real_execute(*a, **kw)

    engine.execute_signal = spy
    with mock.patch.object(agent, "_save_snapshot"):
        agent.run()
    assert action in calls
