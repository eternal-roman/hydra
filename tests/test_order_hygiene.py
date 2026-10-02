"""Working-order hygiene: ambiguous placements, stuck rows, stale orders.

Regressions covered:
  * A CLI timeout after Kraken accepted an order was journaled as
    PLACEMENT_FAILED and rolled back, so the next tick placed a duplicate.
  * A PLACED row whose terminal event was missed (boot query error, stream
    sequence gap, cancel answered "Unknown order") was never re-queried, and
    `_resting_entry` held every later signal on the pair — exits included.
  * A resting post-only order the market left behind was never re-priced:
    a parked SELL kept the engine flat on paper while the coins fell.
  * A second terminal event for the same row re-ran the rollback/true-up.
"""
import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import (
    HydraAgent, UNCONFIRMED_PLACEMENT_GRACE_S, order_reprice_settings,
    placement_outcome_unknown,
)
from hydra_engine import HydraEngine, CrossPairCoordinator, SIZING_COMPETITION
from hydra_kraken_cli import KrakenCLI
from hydra_streams import FakeExecutionStream, FakeTickerStream

PAIR = "BTC/USD"


def _agent():
    agent = HydraAgent.__new__(HydraAgent)
    agent.pairs = [PAIR]
    agent.paper = False
    agent.demo = False
    agent.mode = "competition"
    agent.brain = None
    agent.engines = {PAIR: HydraEngine(initial_balance=1000.0, asset=PAIR,
                                       sizing=SIZING_COMPETITION)}
    agent.order_journal = []
    agent._cancel_sent_ids = set()
    agent._books_dirty = False
    agent._userref_counter = 1000
    agent._portfolio_buy_halted = False
    agent._portfolio_max_drawdown_pct = 0.0
    agent._unconfirmed_since = {}
    agent._requery_ids = set()
    agent.execution_stream = FakeExecutionStream()
    agent.ticker_stream = FakeTickerStream([PAIR])
    agent.ticker_stream.inject(PAIR, {"bid": 179.9, "ask": 180.1, "last": 180.0})
    agent._cached_balance = {"USD": 1000.0}
    agent._cached_free_balance = {"USD": 1000.0}
    agent.balance_stream = None
    agent.triangle = None
    agent.coordinator = CrossPairCoordinator([PAIR])
    agent.trackers = {}
    agent._completed_trades_since_update = 0
    eng = agent.engines[PAIR]
    for i in range(80):
        px = 100.0 + i
        eng.ingest_candle({"open": px - 0.5, "high": px + 1.5, "low": px - 1.5,
                           "close": px, "volume": 10.0,
                           "timestamp": 1_700_000_000 + i * 3600})
    return agent


def _place_buy(agent):
    eng = agent.engines[PAIR]
    snap = eng.snapshot_position()
    trade = eng.execute_signal("BUY", 0.9, reason="test entry", strategy="MOMENTUM")
    assert trade is not None
    state = {"regime": "TREND_UP", "strategy": "MOMENTUM", "_pre_trade_snapshot": snap}
    last = {"action": trade.action, "price": trade.price,
            "amount": round(trade.amount, 8), "reason": trade.reason,
            "confidence": trade.confidence}
    ok = agent._place_order(PAIR, last, state)
    if not ok:
        eng.restore_position(snap)
    return ok


def _timeout_run(placed):
    def fake_run(args, timeout=20, retries=None):
        if args[:2] == ["order", "buy"]:
            if "--validate" in args:
                return {"descr": {"order": "validated"}}
            placed.append(int(args[args.index("--userref") + 1]))
            return {"error": "Command timed out",
                    "error_category": "transport_timeout", "retryable": True}
        raise AssertionError(f"unexpected CLI call {args}")
    return fake_run


def test_placement_outcome_classification():
    assert placement_outcome_unknown({"error": "x", "error_category": "transport_timeout"})
    assert placement_outcome_unknown({"error": "x", "error_category": "network"})
    assert not placement_outcome_unknown({"error": "EOrder:Post only order",
                                          "error_category": "api"})
    assert not placement_outcome_unknown({"error": "x", "error_category": "transport_spawn"})
    assert not placement_outcome_unknown({"txid": ["O1"]})


def test_timeout_keeps_a_blocking_row_and_never_double_places():
    agent = _agent()
    eng = agent.engines[PAIR]
    placed = []
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=_timeout_run(placed)):
        assert _place_buy(agent) is True
        size_after = eng.position.size
        row = agent.order_journal[-1]
        assert row["lifecycle"]["state"] == "PLACED"
        assert row["lifecycle"]["unconfirmed"] is True
        assert row["order_ref"]["order_userref"] == placed[0]
        assert size_after > 0  # optimistic book kept, not rolled back
        assert agent._resting_entry(PAIR) is row
        # A second attempt on the same pair is refused before any CLI call.
        before = len(placed)
        trade = {"action": "BUY", "price": 180.0, "amount": 0.1,
                 "reason": "again", "confidence": 0.9}
        assert agent._place_order(PAIR, trade, {}) is False
        assert len(placed) == before
    assert agent.execution_stream.is_tracking(userref=placed[0])


def test_stream_names_the_order_and_its_fill_is_applied():
    agent = _agent()
    eng = agent.engines[PAIR]
    placed = []
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=_timeout_run(placed)):
        _place_buy(agent)
    uref = placed[0]
    row = agent.order_journal[-1]
    amount = row["intent"]["amount"]
    stream = agent.execution_stream
    stream.inject_event({"order_id": "OREAL", "order_userref": uref,
                         "order_status": "new"})
    assert stream.drain_events() == []
    assert stream.learned_order_id(uref) == "OREAL"
    agent._resolve_unconfirmed_placements()
    assert row["order_ref"]["order_id"] == "OREAL"
    assert "unconfirmed" not in row["lifecycle"]
    stream.inject_event({"order_id": "OREAL", "order_userref": uref,
                         "order_status": "filled", "exec_id": "E1",
                         "last_qty": amount, "last_price": 179.5,
                         "fees": [{"qty": 0.0}]})
    for ev in stream.drain_events():
        agent._apply_execution_event(ev)
    assert row["lifecycle"]["state"] == "FILLED"
    assert eng.position.avg_entry == pytest.approx(179.5)


def test_terminal_event_finds_a_row_that_still_has_no_txid():
    agent = _agent()
    placed = []
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=_timeout_run(placed)):
        _place_buy(agent)
    row = agent.order_journal[-1]
    stream = agent.execution_stream
    # Kraken cancels it (post-only would cross) before any "new" was read.
    stream.inject_event({"order_id": "OREJ", "order_userref": placed[0],
                         "order_status": "canceled", "reason": "Post only order"})
    events = stream.drain_events()
    assert len(events) == 1 and events[0]["userref"] == placed[0]
    agent._apply_execution_event(events[0])
    assert row["lifecycle"]["state"] == "CANCELLED_UNFILLED"
    assert row["order_ref"]["order_id"] == "OREJ"
    assert agent.engines[PAIR].position.size == 0.0


def test_silence_through_the_grace_window_means_not_accepted():
    agent = _agent()
    stream = agent.execution_stream
    stream._started_at = time.monotonic() - 10.0  # connected before the send
    placed = []
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=_timeout_run(placed)):
        _place_buy(agent)
    uref = placed[0]
    row = agent.order_journal[-1]
    agent._resolve_unconfirmed_placements()
    assert row["lifecycle"]["state"] == "PLACED"  # still inside the window
    agent._unconfirmed_since[uref] -= UNCONFIRMED_PLACEMENT_GRACE_S + 1.0
    stream._started_at = agent._unconfirmed_since[uref] - 1.0  # still before the send
    agent._resolve_unconfirmed_placements()
    assert row["lifecycle"]["state"] == "REJECTED"
    assert agent.engines[PAIR].position.size == 0.0
    assert agent._resting_entry(PAIR) is None
    assert not stream.is_tracking(userref=uref)


def test_a_stream_restart_inside_the_window_keeps_the_row_blocking():
    agent = _agent()
    stream = agent.execution_stream
    placed = []
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=_timeout_run(placed)):
        _place_buy(agent)
    uref = placed[0]
    agent._unconfirmed_since[uref] -= UNCONFIRMED_PLACEMENT_GRACE_S + 1.0
    stream._started_at = time.monotonic()  # (re)connected after the send
    agent._resolve_unconfirmed_placements()
    assert agent.order_journal[-1]["lifecycle"]["state"] == "PLACED"


def test_restored_unconfirmed_row_is_held_and_warned_once(capsys):
    agent = _agent()
    agent.order_journal.append({
        "pair": PAIR, "side": "BUY", "placed_at": "2026-10-01T00:00:00+00:00",
        "intent": {"amount": 0.1, "paper": False},
        "order_ref": {"order_userref": 4242, "order_id": None},
        "lifecycle": {"state": "PLACED", "unconfirmed": True},
    })
    agent._resolve_unconfirmed_placements()
    agent._resolve_unconfirmed_placements()
    out = capsys.readouterr().out
    assert out.count("carried over from an earlier session") == 1
    assert agent.order_journal[-1]["lifecycle"]["state"] == "PLACED"


def test_duplicate_terminal_event_is_ignored():
    agent = _agent()
    eng = agent.engines[PAIR]
    eng.position.size = 0.5
    eng.position.avg_entry = 150.0
    row = {"pair": PAIR, "side": "BUY", "intent": {"amount": 0.5},
           "order_ref": {"order_userref": 7, "order_id": "OX"},
           "lifecycle": {"state": "FILLED"},
           "pre_trade_snapshot": {"balance": 1000.0, "position_size": 0.0,
                                  "position_avg_entry": 0.0}}
    agent.order_journal.append(row)
    agent._apply_execution_event({
        "order_id": "OX", "journal_index": 0, "engine_ref": eng,
        "pre_trade_snapshot": row["pre_trade_snapshot"], "placed_amount": 0.5,
        "pair": PAIR, "side": "BUY", "state": "CANCELLED_UNFILLED",
        "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
        "terminal_reason": "late duplicate", "exec_ids": [], "timestamp": None,
    })
    assert row["lifecycle"]["state"] == "FILLED"
    assert eng.position.size == 0.5  # not rolled back


def _placed_row(oid, placed_at, side="SELL", limit=200.0, amount=0.5):
    return {"pair": PAIR, "side": side, "placed_at": placed_at,
            "intent": {"amount": amount, "limit_price": limit, "paper": False},
            "order_ref": {"order_userref": 9, "order_id": oid},
            "lifecycle": {"state": "PLACED"},
            "pre_trade_snapshot": {"balance": 900.0, "position_size": amount,
                                   "position_avg_entry": 150.0}}


def test_untracked_placed_row_is_requeried_and_finalized():
    agent = _agent()
    eng = agent.engines[PAIR]
    row = _placed_row("OSTUCK", "2026-10-01T00:00:00+00:00")
    agent.order_journal.append(row)
    eng.balance = 1000.0  # optimistic SELL already booked
    resp = {"OSTUCK": {"status": "closed", "vol_exec": 0.5, "price": 199.0,
                       "fee": 0.1, "closetm": 1.0}}
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "query_orders", return_value=resp) as q:
        agent._requery_stale_placed(tick=1)
    q.assert_called_once()
    assert row["lifecycle"]["state"] == "FILLED"
    assert agent._resting_entry(PAIR) is None


def test_tracked_young_row_is_not_requeried_off_cycle():
    agent = _agent()
    row = _placed_row("OLIVE", "2099-01-01T00:00:00+00:00")
    agent.order_journal.append(row)
    agent.execution_stream.register(
        order_id="OLIVE", userref=9, journal_index=0, pair=PAIR, side="SELL",
        placed_amount=0.5, engine_ref=agent.engines[PAIR], pre_trade_snapshot=None)
    with mock.patch.object(KrakenCLI, "query_orders") as q:
        agent._requery_stale_placed(tick=6)
    q.assert_not_called()


def test_unknown_order_cancel_flags_a_requery():
    agent = _agent()
    row = _placed_row("OGONE", "2026-10-01T00:00:00+00:00")
    agent.order_journal.append(row)
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "cancel_order",
                              return_value={"error": "EOrder:Unknown order"}):
        agent._cancel_resting_for_opposite(row, "BUY")
    assert "OGONE" in agent._requery_ids


def test_parked_sell_is_cancelled_once_the_market_moves_away(monkeypatch):
    monkeypatch.delenv("HYDRA_ORDER_REPRICE_S", raising=False)
    monkeypatch.delenv("HYDRA_ORDER_REPRICE_BPS", raising=False)
    agent = _agent()
    agent.ticker_stream.inject(PAIR, {"bid": 189.0, "ask": 190.0, "last": 189.5})
    old = "2026-01-01T00:00:00+00:00"
    agent.order_journal.append(_placed_row("OPARK", old, limit=200.0))
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "cancel_order", return_value={"count": 1}) as c:
        agent._reprice_stale_resting()
    c.assert_called_once_with("OPARK")


def test_reprice_respects_age_distance_and_kill_switch(monkeypatch):
    agent = _agent()
    agent.ticker_stream.inject(PAIR, {"bid": 199.0, "ask": 199.9, "last": 199.5})
    old = "2026-01-01T00:00:00+00:00"
    agent.order_journal.append(_placed_row("ONEAR", old, limit=200.0))
    with mock.patch.object(KrakenCLI, "cancel_order") as c:
        agent._reprice_stale_resting()  # only 5 bps away
    c.assert_not_called()
    agent.order_journal[-1]["placed_at"] = "2099-01-01T00:00:00+00:00"
    agent.ticker_stream.inject(PAIR, {"bid": 150.0, "ask": 151.0, "last": 150.5})
    with mock.patch.object(KrakenCLI, "cancel_order") as c:
        agent._reprice_stale_resting()  # far away but too young
    c.assert_not_called()
    agent.order_journal[-1]["placed_at"] = old
    monkeypatch.setenv("HYDRA_ORDER_REPRICE_S", "0")
    with mock.patch.object(KrakenCLI, "cancel_order") as c:
        agent._reprice_stale_resting()
    c.assert_not_called()


def test_reprice_settings_parse_defensively(monkeypatch):
    monkeypatch.setenv("HYDRA_ORDER_REPRICE_S", "nan")
    monkeypatch.setenv("HYDRA_ORDER_REPRICE_BPS", "-3")
    assert order_reprice_settings() == (900.0, 15.0)


def test_txid_less_success_is_adopted_from_the_stream_never_rejected():
    agent = _agent()
    stream = agent.execution_stream
    stream._started_at = time.monotonic() - 1000.0

    def fake_run(args, timeout=20, retries=None):
        if args[:2] == ["order", "buy"]:
            if "--validate" in args:
                return {"descr": {"order": "validated"}}
            return {"descr": {"order": "buy"}}  # success, but no txid
        raise AssertionError(f"unexpected CLI call {args}")

    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "_run", side_effect=fake_run):
        assert _place_buy(agent) is True
    row = agent.order_journal[-1]
    uref = row["order_ref"]["order_userref"]
    assert row["order_ref"]["order_id"] is None
    assert row["lifecycle"]["accepted_without_txid"] is True
    agent._resolve_unconfirmed_placements()  # silent stream: hold, never reject
    assert row["lifecycle"]["state"] == "PLACED"
    stream.inject_event({"order_id": "OLATE", "order_userref": uref,
                         "order_status": "new"})
    stream.drain_events()
    agent._resolve_unconfirmed_placements()
    assert row["order_ref"]["order_id"] == "OLATE"
    assert "accepted_without_txid" not in row["lifecycle"]
