"""Tuner outcomes follow the confirmed sell fill, net of the exchange fee.

The optimistic SELL used to record a gross win/loss while the order was
still PLACED. A later cancel restored the engine and left that
observation in the window. Recording happens in _apply_execution_event
after _deduct_fill_fee, and only when the fill actually flattens the book.
"""
import os
import sys
import pathlib
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent  # noqa: E402
from hydra_engine import HydraEngine  # noqa: E402
from hydra_tuner import DEFAULT_PARAMS, ParameterTracker  # noqa: E402


PAIR = "BTC/USD"


def setup_function(_fn=None):
    os.environ.pop("HYDRA_FEE_DEDUCTION_DISABLED", None)


def _engine(mark=110.0, size=0.01, entry=100.0):
    eng = HydraEngine(initial_balance=10_000.0, asset=PAIR, hold_through=False)
    eng.prices.append(mark)
    params = dict(DEFAULT_PARAMS)
    params["volatile_atr_mult"] = 2.2
    eng.position.size = size
    eng.position.avg_entry = entry
    eng.position.params_at_entry = params
    return eng


def _agent(eng):
    agent = HydraAgent.__new__(HydraAgent)
    agent.order_journal = []
    agent._books_dirty = False
    agent.pairs = [PAIR]
    agent.engines = {PAIR: eng}
    agent._completed_trades_since_update = 0
    agent.trackers = {
        PAIR: ParameterTracker(pair=PAIR, save_dir=tempfile.mkdtemp()),
    }
    return agent


def _journal(trade, snap, oid):
    return {
        "pair": PAIR,
        "side": trade.action,
        "order_ref": {"order_id": oid, "order_userref": 1},
        "intent": {"amount": trade.amount, "limit_price": trade.price},
        "decision": {
            "params_at_entry": trade.params_at_entry,
            "strategy": "MOMENTUM",
            "regime": None,
            "reason": trade.reason,
            "confidence": trade.confidence,
            "cross_pair_override": None,
            "book_confidence_modifier": None,
            "brain_verdict": None,
            "swap_id": None,
        },
        "pre_trade_snapshot": snap,
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


def _event(entry, eng, snap, *, state, vol, price, fee, side=None, idx=0):
    ev = {
        "journal_index": idx,
        "order_id": entry["order_ref"]["order_id"],
        "state": state,
        "vol_exec": vol,
        "avg_fill_price": price,
        "engine_ref": eng,
        "pre_trade_snapshot": snap,
        "pair": PAIR,
        "side": side or entry["side"],
        "placed_amount": entry["intent"]["amount"],
        "terminal_reason": "test",
        "exec_ids": ["exec-1"],
        "timestamp": "2026-09-26T00:00:00+00:00",
    }
    if fee is not None:
        ev["fee_quote"] = fee
    return ev


def _sell(eng, oid="OID-SELL"):
    snap = eng.snapshot_position()
    trade = eng.execute_signal("SELL", 0.9, "exit", "MOMENTUM")
    assert trade is not None and trade.action == "SELL"
    return snap, trade, _journal(trade, snap, oid)


def test_optimistic_close_does_not_record_until_fill_and_nets_the_fee():
    eng = _engine(mark=110.0)
    agent = _agent(eng)
    snap, trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    # Flat on the optimistic book, gross profit in hand, nothing learned yet.
    assert eng.position.size == 0.0
    assert trade.profit == pytest.approx(0.1)
    assert agent.trackers[PAIR].observations == []

    # Confirmed at a worse price than the candle close. Gross is 0.05, not 0.10.
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=0.01, price=105.0, fee=0.02,
    ))
    obs = agent.trackers[PAIR].observations
    assert len(obs) == 1
    assert obs[0]["outcome"] == "win"
    assert obs[0]["signal"] == "SELL"
    assert obs[0]["profit"] == pytest.approx(0.03)  # 0.05 gross - 0.02 fee
    assert obs[0]["params"]["volatile_atr_mult"] == 2.2
    assert entry["tuner_recorded"] is True
    assert entry["lifecycle"]["tuner_recorded"] is True
    assert entry["lifecycle"]["fee_applied"] is True
    assert eng.balance == pytest.approx(10_001.03)
    assert agent._completed_trades_since_update == 1

    # Replay rewrites lifecycle and re-true-ups the book. One observation,
    # and the fee is still debited exactly once against the restored book.
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=0.01, price=105.0, fee=0.02,
    ))
    assert len(agent.trackers[PAIR].observations) == 1
    assert agent._completed_trades_since_update == 1
    assert eng.balance == pytest.approx(10_001.03)


def test_fee_larger_than_gross_is_a_loss():
    eng = _engine(mark=110.0)
    agent = _agent(eng)
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=0.01, price=110.0, fee=0.15,
    ))
    obs = agent.trackers[PAIR].observations
    assert len(obs) == 1
    # Price P&L is +0.10; the fee turns the round trip into a loss.
    assert eng.trades[-1].profit == pytest.approx(0.10)
    assert obs[0]["profit"] == pytest.approx(-0.05)
    assert obs[0]["outcome"] == "loss"


def test_zero_missing_or_bad_fee_stays_gross():
    def _close(fee):
        eng = _engine(mark=110.0)
        agent = _agent(eng)
        snap, _trade, entry = _sell(eng)
        agent.order_journal.append(entry)
        agent._apply_execution_event(_event(
            entry, eng, snap, state="FILLED", vol=0.01, price=110.0, fee=fee,
        ))
        return agent.trackers[PAIR].observations

    for fee in (0.0, None, "not-a-fee"):
        obs = _close(fee)
        assert len(obs) == 1
        assert obs[0]["profit"] == pytest.approx(0.10)
        assert obs[0]["outcome"] == "win"


def test_break_even_after_fee_is_a_loss():
    eng = _engine(mark=102.0)
    agent = _agent(eng)
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=0.01, price=102.0, fee=0.02,
    ))
    obs = agent.trackers[PAIR].observations
    assert len(obs) == 1
    assert obs[0]["profit"] == pytest.approx(0.0)
    assert obs[0]["outcome"] == "loss"


@pytest.mark.parametrize("state", ["CANCELLED_UNFILLED", "REJECTED", "PLACED"])
def test_non_fill_does_not_record(state):
    eng = _engine()
    agent = _agent(eng)
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    assert eng.win_count == 1
    agent._apply_execution_event(_event(
        entry, eng, snap, state=state, vol=0.0, price=None, fee=0.0,
    ))
    assert agent.trackers[PAIR].observations == []
    assert entry.get("tuner_recorded") is not True
    if state in ("CANCELLED_UNFILLED", "REJECTED"):
        assert eng.position.size == pytest.approx(0.01)
        assert eng.win_count == 0
    else:
        # PLACED is not a terminal fill and does not roll the book back.
        assert eng.position.size == 0.0


def test_buy_fill_does_not_record():
    eng = HydraEngine(initial_balance=10_000.0, asset=PAIR, hold_through=False)
    eng.prices.append(100.0)
    snap = eng.snapshot_position()
    trade = eng.execute_signal("BUY", 0.9, "enter", "MOMENTUM")
    assert trade is not None and trade.action == "BUY"
    agent = _agent(eng)
    entry = _journal(trade, snap, "OID-BUY")
    agent.order_journal.append(entry)
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=trade.amount, price=100.0,
        fee=0.05, side="BUY",
    ))
    assert agent.trackers[PAIR].observations == []
    assert entry.get("tuner_recorded") is not True
    assert entry["lifecycle"]["fee_applied"] is True


def test_partial_that_leaves_inventory_does_not_record():
    eng = _engine()
    agent = _agent(eng)
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    agent._apply_execution_event(_event(
        entry, eng, snap, state="PARTIALLY_FILLED", vol=0.004, price=110.0, fee=0.01,
    ))
    assert eng.position.size == pytest.approx(0.006)
    assert agent.trackers[PAIR].observations == []
    assert entry.get("tuner_recorded") is not True
    assert agent._completed_trades_since_update == 0
    # The partial still paid the fee.
    assert entry["lifecycle"]["fee_applied"] is True


def test_closing_fill_after_partial_records_accumulated_net():
    eng = _engine()
    agent = _agent(eng)
    snap1, _trade, entry1 = _sell(eng, oid="OID-PARTIAL")
    agent.order_journal.append(entry1)
    agent._apply_execution_event(_event(
        entry1, eng, snap1, state="PARTIALLY_FILLED", vol=0.004, price=110.0,
        fee=0.01, idx=0,
    ))
    assert agent.trackers[PAIR].observations == []

    snap2 = eng.snapshot_position()
    trade2 = eng.execute_signal("SELL", 0.9, "exit rest", "MOMENTUM")
    assert trade2 is not None and eng.position.size == 0.0
    entry2 = _journal(trade2, snap2, "OID-CLOSE")
    agent.order_journal.append(entry2)
    agent._apply_execution_event(_event(
        entry2, eng, snap2, state="FILLED", vol=trade2.amount, price=110.0,
        fee=0.02, idx=1,
    ))
    obs = agent.trackers[PAIR].observations
    assert len(obs) == 1
    # Accumulated price P&L (0.04 + 0.06) minus this fill's fee only.
    assert obs[0]["profit"] == pytest.approx(0.08)
    assert obs[0]["outcome"] == "win"
    assert obs[0]["params"]["volatile_atr_mult"] == 2.2


def test_dust_close_partial_records_net():
    eng = _engine()
    agent = _agent(eng)
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    vol = 0.009996  # remainder 0.000004 < BTC dust threshold 0.000005
    agent._apply_execution_event(_event(
        entry, eng, snap, state="PARTIALLY_FILLED", vol=vol, price=110.0, fee=0.02,
    ))
    assert eng.position.size == 0.0
    obs = agent.trackers[PAIR].observations
    assert len(obs) == 1
    assert obs[0]["profit"] == pytest.approx(10.0 * vol - 0.02)
    assert obs[0]["outcome"] == "win"


def test_fee_kill_switch_still_classifies_net():
    os.environ["HYDRA_FEE_DEDUCTION_DISABLED"] = "1"
    try:
        eng = _engine(mark=110.0)
        agent = _agent(eng)
        snap, _trade, entry = _sell(eng)
        balance_before = eng.balance
        agent.order_journal.append(entry)
        agent._apply_execution_event(_event(
            entry, eng, snap, state="FILLED", vol=0.01, price=110.0, fee=0.02,
        ))
        obs = agent.trackers[PAIR].observations
        assert len(obs) == 1
        assert obs[0]["profit"] == pytest.approx(0.08)
        assert obs[0]["outcome"] == "win"
        # Kill switch skips the debit; true-up at the same price is a wash.
        assert eng.balance == pytest.approx(balance_before)
    finally:
        os.environ.pop("HYDRA_FEE_DEDUCTION_DISABLED", None)


def test_fiftieth_confirmed_close_runs_tuner_update():
    eng = _engine()
    agent = _agent(eng)
    agent._completed_trades_since_update = 49
    snap, _trade, entry = _sell(eng)
    agent.order_journal.append(entry)
    agent._apply_execution_event(_event(
        entry, eng, snap, state="FILLED", vol=0.01, price=110.0, fee=0.0,
    ))
    # One observation is under the tuner's own minimum, so the window stays,
    # but the every-50 cadence still fires and resets the counter.
    assert len(agent.trackers[PAIR].observations) == 1
    assert agent._completed_trades_since_update == 0
