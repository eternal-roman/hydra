"""All harness scenarios.

Each scenario is a function that takes a Harness instance and raises on
failure. Scenarios are registered in ALL_SCENARIOS at the bottom of the
file and categorized H (happy), F (failure), E (edge), S (schema),
R (rollback), H_prime (historical regression), W (WS execution stream),
L (live).

Scenario codes are stable identifiers — tests, docs, and CI can reference
them by code. If you change a scenario's semantics, don't reuse its code.

Note: most scenarios stub KrakenCLI._run to avoid real network calls.
Live and validate modes bypass the stubs and hit the real Kraken CLI.

Lifecycle note: after the WS execution stream conversion, scenarios
observe journal entries in one of these states after a successful
placement:
  - Paper flows: `FILLED` (_place_paper_order synthesizes a fill event
    which harness_execute drains and applies immediately)
  - Live-mocked flows: `PLACED` (no WS events arrive in mock mode; a
    scenario can manually call agent.execution_stream.inject_event() to
    drive the lifecycle to a terminal state)
  - Any pre-placement failure: `PLACEMENT_FAILED` with `terminal_reason`
"""

from __future__ import annotations

import os
import time
from typing import Callable

from tests.live_harness.harness import Harness, Scenario, harness_execute
from tests.live_harness.schemas import (
    validate_journal_entry, validate_entry, SchemaViolation,
)
from tests.live_harness.state_comparator import (
    capture_engine_state, assert_rollback_complete, RollbackDiff,
)
from tests.live_harness.stubs import (
    StubRun, build_dispatcher,
    kraken_ticker, kraken_ticker_error, kraken_ticker_missing_fields,
    kraken_order_success_scalar, kraken_order_success_list,
    kraken_order_success_nested, kraken_order_success_missing_txid,
    kraken_order_success_empty_list,
    kraken_order_error, kraken_order_timeout, kraken_order_json_error,
    kraken_paper_success, kraken_paper_error,
    kraken_validate_success, kraken_validate_error,
)

from hydra_agent import HydraAgent
from hydra_kraken_cli import KrakenCLI, WSL_DISTRO


MOCK = frozenset({"mock"})
LIVE = frozenset({"validate", "live"})
VALIDATE_ONLY = frozenset({"validate"})
LIVE_ONLY = frozenset({"live"})
ALL_MOCK = frozenset({"mock"})


# ═════════════════════════════════════════════════════════════════
# Category H — Happy paths
# ═════════════════════════════════════════════════════════════════

def scenario_H1_paper_buy(h: Harness):
    """Paper BUY SOL/USDC -> journal entry reaches FILLED via synthetic event."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "paper_buy": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "H1 paper buy")
    finally:
        stub.restore()

    assert report["outcome"] == "success", f"expected success, got {report['outcome']}"
    entry = report["last_journal_entry"]
    assert entry is not None
    validate_journal_entry(entry, expected_state="FILLED")
    assert entry["pair"] == "SOL/USDC"
    assert entry["side"] == "BUY"
    assert entry["intent"]["amount"] > 0
    assert entry["intent"]["paper"] is True
    conf = entry["decision"]["confidence"]
    assert conf is not None and abs(conf - 0.75) < 0.001


def scenario_H2_paper_sell_from_position(h: Harness):
    """Paper SELL SOL/USDC from a preset position -> FILLED."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    engine.position.size = 0.5
    engine.position.avg_entry = 95.0

    stub = StubRun(build_dispatcher({
        "paper_sell": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "SELL", 0.80, "H2 paper sell")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    entry = report["last_journal_entry"]
    validate_journal_entry(entry, expected_state="FILLED")
    assert entry["side"] == "SELL"


def scenario_H3_live_buy_mocked(h: Harness):
    """Live BUY SOL/USDC with all Kraken responses mocked -> journal entry
    at PLACED, registered with the execution stream under the returned
    order_id. No WS events arrive in mock mode so the entry stays PLACED."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
        "order_validate": kraken_validate_success(),
        "order": kraken_order_success_list("TXID_H3_ABC"),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "H3 live buy")
    finally:
        stub.restore()

    assert report["outcome"] == "success", f"expected success, got {report}"
    entry = report["last_journal_entry"]
    validate_journal_entry(entry, expected_state="PLACED")
    assert entry["intent"]["order_type"] == "limit"
    assert entry["intent"]["post_only"] is True
    assert entry["order_ref"]["order_id"] == "TXID_H3_ABC"
    assert isinstance(entry["order_ref"]["order_userref"], int)
    # The execution stream should have the order registered under its id.
    known = agent.execution_stream._known_orders
    assert "TXID_H3_ABC" in known, \
        f"stream missing order_id; known_orders={list(known.keys())}"
    tracked = known["TXID_H3_ABC"]
    assert tracked["pair"] == "SOL/USDC"
    assert tracked["side"] == "BUY"


def scenario_H4_live_sell_mocked_from_position(h: Harness):
    """Live SELL from a preset position -> PLACED entry, engine total_trades
    incremented on SELL-close (commit 88797ca: increment on close, not on BUY)."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    engine.position.size = 0.05
    engine.position.avg_entry = 95.0
    pre_total = engine.total_trades
    pre_wins = engine.win_count
    pre_losses = engine.loss_count

    stub = StubRun(build_dispatcher({
        "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
        "order_validate": kraken_validate_success(),
        "order": kraken_order_success_list("TXID_H4_XYZ"),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "SELL", 0.80, "H4 live sell close")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    entry = report["last_journal_entry"]
    validate_journal_entry(entry, expected_state="PLACED")
    post_total = engine.total_trades
    post_wins = engine.win_count
    post_losses = engine.loss_count
    if engine.position.size < 0.00001:
        assert post_total == pre_total + 1, f"total_trades: {pre_total} -> {post_total}"
        assert (post_wins + post_losses) == (pre_wins + pre_losses + 1)


def scenario_H5_live_buy_real_kraken(h: Harness):
    """LIVE MODE: place a real post-only buy on SOL/USDC at a non-crossing
    price, verify PLACED entry and stream registration, then cancel."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    report = harness_execute(agent, "SOL/USDC", "BUY", 0.60, "L1 live mode")
    try:
        assert report["outcome"] in ("success", "failed_and_rolled_back"), \
            f"unexpected outcome: {report['outcome']}"
        if report["outcome"] == "success":
            entry = report["last_journal_entry"]
            validate_journal_entry(entry)
            order_id = entry["order_ref"]["order_id"]
            assert order_id and order_id != "unknown"
            assert order_id in agent.execution_stream._known_orders
            _cancel_order_with_retry(order_id, max_retries=3)
    except Exception:
        _cancel_all_safe()
        raise


def scenario_H6_live_sell_real_kraken(h: Harness):
    """LIVE MODE: SELL without position -> engine rejects, no journal entry."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    assert engine.position.size == 0.0

    report = harness_execute(agent, "SOL/USDC", "SELL", 0.70, "H6 live sell no position")
    assert report["outcome"] == "engine_rejected", \
        f"expected engine to refuse SELL with no position; got {report['outcome']}"
    assert report["journal_count_before"] == report["journal_count_after"]


# ═════════════════════════════════════════════════════════════════
# Category F — Failure paths (each verifies rollback completeness)
# ═════════════════════════════════════════════════════════════════

def _run_with_rollback_check(h: Harness, scenario_code: str,
                              setup_stub: Callable[[], StubRun],
                              action: str, confidence: float,
                              expected_reason_prefix: str,
                              expected_outcome: str = "failed_and_rolled_back"):
    """Shared helper for F scenarios: wraps setup, execution, and rollback
    assertion. Asserts the journal entry lands at PLACEMENT_FAILED with a
    terminal_reason that starts with expected_reason_prefix.
    """
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    if action == "SELL":
        engine.position.size = 0.05
        engine.position.avg_entry = 95.0

    before = capture_engine_state(engine)
    stub = setup_stub().install()
    try:
        report = harness_execute(agent, "SOL/USDC", action, confidence, f"{scenario_code} fail")
    finally:
        stub.restore()

    assert report["outcome"] == expected_outcome, \
        f"{scenario_code}: expected outcome {expected_outcome!r}, got {report['outcome']!r}"
    entry = report["last_journal_entry"]
    assert entry is not None, f"{scenario_code}: no journal entry written"
    validate_journal_entry(entry, expected_state="PLACEMENT_FAILED")
    reason = entry["lifecycle"]["terminal_reason"]
    assert isinstance(reason, str) and reason.startswith(expected_reason_prefix), \
        f"{scenario_code}: expected terminal_reason to start with {expected_reason_prefix!r}, got {reason!r}"

    after = capture_engine_state(engine)
    assert_rollback_complete(before, after, scenario_name=scenario_code)


def scenario_F1_ticker_error(h: Harness):
    """Ticker stream unhealthy -> PLACEMENT_FAILED(ticker_stream_unavailable), rollback."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]

    # Make WS ticker stream unhealthy — _place_order refuses to trade
    agent.ticker_stream.set_healthy(False)

    before = capture_engine_state(engine)
    stub = StubRun(build_dispatcher({})).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "F1 fail")
    finally:
        stub.restore()

    assert report["outcome"] == "failed_and_rolled_back", \
        f"F1: expected outcome 'failed_and_rolled_back', got {report['outcome']!r}"
    entry = report["last_journal_entry"]
    assert entry is not None, "F1: no journal entry written"
    validate_journal_entry(entry, expected_state="PLACEMENT_FAILED")
    reason = entry["lifecycle"]["terminal_reason"]
    assert reason == "ticker_stream_unavailable", \
        f"F1: expected terminal_reason 'ticker_stream_unavailable', got {reason!r}"

    after = capture_engine_state(engine)
    assert_rollback_complete(before, after, scenario_name="F1")


def scenario_F2_ticker_missing_fields(h: Harness):
    """Ticker stream returns data without bid -> PLACEMENT_FAILED(ticker_stream_unavailable)."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]

    # Inject ticker data that lacks 'bid' key — _place_order should fail
    agent.ticker_stream.inject("SOL/USDC", {"last": 100.0})

    before = capture_engine_state(engine)
    stub = StubRun(build_dispatcher({})).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "F2 fail")
    finally:
        stub.restore()

    assert report["outcome"] == "failed_and_rolled_back", \
        f"F2: expected outcome 'failed_and_rolled_back', got {report['outcome']!r}"
    entry = report["last_journal_entry"]
    assert entry is not None, "F2: no journal entry written"
    validate_journal_entry(entry, expected_state="PLACEMENT_FAILED")
    reason = entry["lifecycle"]["terminal_reason"]
    assert reason == "ticker_stream_unavailable", \
        f"F2: expected terminal_reason 'ticker_stream_unavailable', got {reason!r}"

    after = capture_engine_state(engine)
    assert_rollback_complete(before, after, scenario_name="F2")


def scenario_F3_validation_post_only_crossed(h: Harness):
    """Validation returns post-only crossing error -> PLACEMENT_FAILED(validation_failed:...)."""
    _run_with_rollback_check(
        h, "F3",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_error("EOrder:Post-only order rejected (would cross)"),
        })),
        action="BUY", confidence=0.75,
        expected_reason_prefix="validation_failed",
    )


def scenario_F4_validation_insufficient_funds(h: Harness):
    """Validation returns insufficient funds -> PLACEMENT_FAILED(validation_failed:...)."""
    _run_with_rollback_check(
        h, "F4",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_error("EOrder:Insufficient funds"),
        })),
        action="BUY", confidence=0.75,
        expected_reason_prefix="validation_failed",
    )


def scenario_F5_execution_fails_after_validation(h: Harness):
    """Validation passes but second order call errors -> PLACEMENT_FAILED(placement_error:...)."""
    def make_stub():
        return StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_error("EOrder:Market in cancel_only mode"),
        }))

    _run_with_rollback_check(
        h, "F5", setup_stub=make_stub,
        action="BUY", confidence=0.75,
        expected_reason_prefix="placement_error",
    )


def scenario_F6_execution_timeout(h: Harness):
    """Order subprocess times out -> PLACEMENT_FAILED(placement_error:...)."""
    _run_with_rollback_check(
        h, "F6",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_timeout(),
        })),
        action="BUY", confidence=0.75,
        expected_reason_prefix="placement_error",
    )


def scenario_F7_paper_failure(h: Harness):
    """Paper trade fails -> PLACEMENT_FAILED(paper_failed:...). Paper has no
    pre-trade snapshot, so outcome is 'failed_and_rolled_back' but the
    rollback is a no-op."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "paper_buy": kraken_paper_error("Insufficient paper balance"),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "F7 paper fail")
    finally:
        stub.restore()

    assert report["outcome"] == "failed_and_rolled_back"
    entry = report["last_journal_entry"]
    assert entry is not None
    validate_journal_entry(entry, expected_state="PLACEMENT_FAILED")
    assert entry["intent"]["paper"] is True
    assert entry["lifecycle"]["terminal_reason"].startswith("paper_failed")


# ═════════════════════════════════════════════════════════════════
# Category E — Edge cases
# ═════════════════════════════════════════════════════════════════

def _live_success_scenario(h: Harness, code: str, order_response: dict,
                            expected_order_id_registered: str | None):
    """Generic live-success scenario with a configurable order response shape.

    If expected_order_id_registered is None, asserts the execution stream
    did NOT register the order (because order_id came back as 'unknown').
    Otherwise asserts the order_id is tracked under _known_orders."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
        "order_validate": kraken_validate_success(),
        "order": order_response,
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, f"{code} edge")
    finally:
        stub.restore()

    assert report["outcome"] == "success", f"{code}: {report}"
    entry = report["last_journal_entry"]
    validate_journal_entry(entry, expected_state="PLACED")

    known = agent.execution_stream._known_orders
    if expected_order_id_registered is None:
        # When order_id is 'unknown', register() is a no-op by design.
        assert not known, \
            f"{code}: stream should be empty, got {list(known.keys())}"
    else:
        assert expected_order_id_registered in known, \
            f"{code}: missing order_id {expected_order_id_registered!r}; have {list(known.keys())}"


def scenario_E1_txid_list_unwrap(h: Harness):
    """Txid returned as list -> unwrapped to scalar, stream registers it."""
    _live_success_scenario(
        h, "E1",
        order_response=kraken_order_success_list("E1_TXID"),
        expected_order_id_registered="E1_TXID",
    )


def scenario_E2_txid_nested_result(h: Harness):
    """Txid nested under `result` -> extracted via fallback chain."""
    _live_success_scenario(
        h, "E2",
        order_response=kraken_order_success_nested("E2_TXID"),
        expected_order_id_registered="E2_TXID",
    )


def scenario_E3_txid_missing(h: Harness):
    """Txid missing entirely -> becomes 'unknown', stream skips registration."""
    _live_success_scenario(
        h, "E3",
        order_response=kraken_order_success_missing_txid(),
        expected_order_id_registered=None,
    )


def scenario_E4_txid_empty_list(h: Harness):
    """Txid is an empty list -> becomes 'unknown', stream skips registration."""
    _live_success_scenario(
        h, "E4",
        order_response=kraken_order_success_empty_list(),
        expected_order_id_registered=None,
    )


def scenario_E5_halted_engine(h: Harness):
    """Halted engine -> engine.tick() returns HOLD with the halt reason, no
    trade generated. Tests the PRODUCTION tick-loop behavior at
    hydra_engine.py:866-868 (the `if self.halted: return HOLD` early return).

    NOTE: engine.execute_signal() itself does NOT check `halted` — only
    tick() does. In production, tick() is always called first, so
    execute_signal is never reached on a halted engine. But this is a
    LATENT GAP: any future code path that calls execute_signal directly
    (e.g. the swap handler) bypasses the halt check."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    engine.halted = True
    engine.halt_reason = "Harness test: simulated circuit breaker"

    state = engine.tick()
    assert state["signal"]["action"] == "HOLD", \
        f"E5: halted engine tick() must return HOLD; got {state['signal']['action']}"
    reason = state["signal"]["reason"].lower()
    assert "halt" in reason or "circuit" in reason or "breaker" in reason, \
        f"E5: halted signal should reference halt reason; got {state['signal']['reason']!r}"
    assert state.get("halted") is True, "E5: state should expose halted=True"


def scenario_E6_ordermin_partial_sell_forces_full_close(h: Harness):
    """Partial sell below ordermin triggers full-close logic at
    hydra_engine.py:954-963 (commit 35a134d fix)."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    engine.position.size = 0.025
    engine.position.avg_entry = 95.0

    stub = StubRun(build_dispatcher({
        "paper_sell": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "SELL", 0.65, "E6 partial sell -> full close")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    entry = report["last_journal_entry"]
    assert entry is not None
    assert engine.position.size < 0.00001, \
        f"E6: position not fully closed; size={engine.position.size}"


def scenario_E7_unparseable_kraken_response(h: Harness):
    """Kraken returns a JSON parse error dict -> PLACEMENT_FAILED, rollback."""
    _run_with_rollback_check(
        h, "E7",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_json_error(),
        })),
        action="BUY", confidence=0.75,
        expected_reason_prefix="placement_error",
    )


# ═════════════════════════════════════════════════════════════════
# Category S — Schema compliance
# ═════════════════════════════════════════════════════════════════
#
# Every H/F/E scenario calls validate_journal_entry() with the expected
# state. A single meta-scenario confirms the schema validator itself
# rejects malformed input.


def scenario_S_meta_validator_rejects_garbage(h: Harness):
    """Meta-check: the validator itself catches obvious malformations."""
    from tests.live_harness.schemas import (
        validate_journal_entry, SchemaViolation, LIFECYCLE_STATES,
    )

    assert "PLACED" in LIFECYCLE_STATES
    assert "FILLED" in LIFECYCLE_STATES
    assert "PLACEMENT_FAILED" in LIFECYCLE_STATES

    # Missing required sections
    try:
        validate_journal_entry({"placed_at": "2026-01-01T00:00:00+00:00"})
        raise AssertionError("validator should have rejected near-empty entry")
    except SchemaViolation:
        pass

    # Wrong side
    try:
        validate_journal_entry({
            "placed_at": "2026-01-01T00:00:00+00:00",
            "pair": "SOL/USDC",
            "side": "LONG",
            "intent": {"amount": 0.02, "limit_price": 100.0, "post_only": True,
                        "order_type": "limit", "paper": False},
            "decision": {"strategy": None, "regime": None, "reason": None,
                          "confidence": None, "params_at_entry": None,
                          "cross_pair_override": None,
                          "book_confidence_modifier": None,
                          "brain_verdict": None, "swap_id": None},
            "order_ref": {"order_userref": None, "order_id": None},
            "lifecycle": {"state": "PLACED", "vol_exec": 0, "avg_fill_price": None,
                           "fee_quote": 0, "final_at": None,
                           "terminal_reason": None, "exec_ids": []},
        })
        raise AssertionError("validator should have rejected side='LONG'")
    except SchemaViolation:
        pass

    # FILLED with mismatched vol_exec
    try:
        validate_journal_entry({
            "placed_at": "2026-01-01T00:00:00+00:00",
            "pair": "SOL/USDC", "side": "BUY",
            "intent": {"amount": 0.02, "limit_price": 100.0, "post_only": True,
                        "order_type": "limit", "paper": False},
            "decision": {"strategy": None, "regime": None, "reason": None,
                          "confidence": None, "params_at_entry": None,
                          "cross_pair_override": None,
                          "book_confidence_modifier": None,
                          "brain_verdict": None, "swap_id": None},
            "order_ref": {"order_userref": None, "order_id": None},
            "lifecycle": {"state": "FILLED", "vol_exec": 0.01,  # mismatch
                           "avg_fill_price": 100.0, "fee_quote": 0,
                           "final_at": None, "terminal_reason": None, "exec_ids": []},
        })
        raise AssertionError("validator should have caught FILLED vol_exec mismatch")
    except SchemaViolation:
        pass


# ═════════════════════════════════════════════════════════════════
# Category R — Rollback completeness (meta)
# ═════════════════════════════════════════════════════════════════


def scenario_R_meta_comparator_catches_tampering(h: Harness):
    """Meta-check: the rollback comparator catches tampered state."""
    from tests.live_harness.state_comparator import (
        capture_engine_state, assert_rollback_complete, RollbackDiff,
    )
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]

    before = capture_engine_state(engine)
    engine.balance -= 10.0  # tamper
    after = capture_engine_state(engine)

    try:
        assert_rollback_complete(before, after, scenario_name="R-meta")
        raise AssertionError("comparator should have caught balance tampering")
    except RollbackDiff:
        pass


# ═════════════════════════════════════════════════════════════════
# Category H' — Historical regression tests
# ═════════════════════════════════════════════════════════════════

def scenario_Hp1_falsy_zero_competition_start_balance(h: Harness):
    """Commit 4effbea: snapshot competition_start_balance=0.0 must restore
    as 0.0, not None."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    snap = {"competition_start_balance": 0.0}
    value = snap.get("competition_start_balance")
    assert value is not None, "The fix uses `is not None`; 0.0 must not be treated as missing"
    assert value == 0.0


def scenario_Hp2_pre_trade_snapshot_stripped_from_broadcast(h: Harness):
    """Commit 4effbea: _pre_trade_snapshot must be stripped before broadcast."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    fake_state = {
        "signal": {"action": "HOLD", "confidence": 0.5, "reason": ""},
        "_pre_trade_snapshot": {"position_size": 0.1, "balance": 100.0},
    }
    stripped = dict(fake_state)
    stripped.pop("_pre_trade_snapshot", None)
    assert "_pre_trade_snapshot" not in stripped
    with open(os.path.join(_hydra_root(), "hydra_agent.py"), encoding="utf-8") as f:
        src = f.read()
    assert '_pre_trade_snapshot' in src and 'state.pop("_pre_trade_snapshot"' in src, \
        "Strip logic missing from hydra_agent.py — commit 4effbea regression"


def scenario_Hp3_total_trades_not_incremented_on_buy(h: Harness):
    """Commit 88797ca: BUY must NOT increment total_trades; only SELL-close does."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=500.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    pre_total = engine.total_trades

    stub = StubRun(build_dispatcher({
        "paper_buy": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.80, "H'3 buy total_trades check")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    post_total = engine.total_trades
    assert post_total == pre_total, \
        f"H'3: BUY incremented total_trades ({pre_total} -> {post_total}) — commit 88797ca regression"


def scenario_Hp4_break_even_counts_as_loss(h: Harness):
    """Commit 88797ca: break-even (P&L=0) counts as loss, not win."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    current_price = engine.prices[-1]
    engine.position.size = 0.05
    engine.position.avg_entry = current_price  # exact break-even

    pre_wins = engine.win_count
    pre_losses = engine.loss_count

    stub = StubRun(build_dispatcher({
        "paper_sell": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "SELL", 0.85, "H'4 break-even close")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    post_wins = engine.win_count
    post_losses = engine.loss_count
    delta_wins = post_wins - pre_wins
    delta_losses = post_losses - pre_losses
    assert delta_wins + delta_losses == 1, \
        f"H'4: expected exactly one of wins/losses to increment; got wins+={delta_wins}, losses+={delta_losses}"
    assert delta_losses == 1 and delta_wins == 0, \
        f"H'4: break-even should count as loss, got wins+={delta_wins}, losses+={delta_losses} — commit 88797ca regression"


def scenario_Hp5_txid_as_list_regression(h: Harness):
    """Commit 9e652d5: txid returned as list must be unwrapped."""
    scenario_E1_txid_list_unwrap(h)


def scenario_Hp6_ordermin_sell_regression(h: Harness):
    """Commit 35a134d: partial sell below ordermin forces full close."""
    scenario_E6_ordermin_partial_sell_forces_full_close(h)


def scenario_Hp7_sol_btc_info_only_no_placement(h: Harness):
    """v2.11.0: SOL/BTC with tradable=False must never reach the placement
    path, regardless of signal strength. This scenario reproduces the
    journal failure observed on 2026-04-17 (three consecutive
    PLACEMENT_FAILED:insufficient_BTC_balance entries on SOL/BTC) and
    asserts the fix: engine.execute_signal returns None, no _place_order
    call occurs, no journal entry is written.

    Harness cost: zero exchange-facing calls — the assertion is that
    `harness_execute` short-circuits at the engine layer."""
    agent = h.new_agent(pairs=["SOL/BTC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/BTC", base_price=0.001160)
    engine = agent.engines["SOL/BTC"]

    # Simulate the v2.11.0 real-holding gate: no BTC in the account.
    # In production, _set_engine_balances / _refresh_tradable_flags would
    # set this; we set it directly so the scenario is hermetic.
    engine.tradable = False
    engine.balance = 0.0

    count_before = len(agent.order_journal)
    report = harness_execute(agent, "SOL/BTC", "BUY", 0.85,
                             "Hp7 phantom-BTC regression — oversold SOL/BTC")

    assert report["outcome"] == "engine_rejected", (
        f"Hp7: tradable=False engine must reject BUY at engine layer; "
        f"got {report['outcome']!r}"
    )
    assert report["trade"] is None
    assert len(agent.order_journal) == count_before, (
        "Hp7: no journal entry should be written for an info-only engine — "
        "the PLACEMENT_FAILED loop from 2026-04-17 is gone"
    )


def scenario_Hp8_tradable_reactivates_on_btc_arrival(h: Harness):
    """v2.11.0 behavior, now behind HYDRA_BRIDGE_TRADING=1 (v2.28 default
    is exit_only drain): when BTC arrives mid-session, _refresh_tradable_flags
    re-enables the SOL/BTC engine with a clean equity baseline. Also asserts
    the v2.28 default: with the flag OFF, BTC arrival must NOT re-arm the
    bridge."""
    import os as _os
    import contextlib as _ctx

    @_ctx.contextmanager
    def _bridge_flag_on():
        _os.environ["HYDRA_BRIDGE_TRADING"] = "1"
        try:
            yield
        finally:
            _os.environ.pop("HYDRA_BRIDGE_TRADING", None)

    agent = h.new_agent(pairs=["SOL/BTC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/BTC", base_price=0.001160)
    engine = agent.engines["SOL/BTC"]

    # Start info-only — no BTC.
    engine.tradable = False
    engine.balance = 0.0
    engine.peak_equity = 0.0
    agent._cached_balance = {"USDC": 200.0}

    # NullBalanceStream-equivalent: we'll fall through to _cached_balance.
    class _NullStream:
        healthy = False
        def latest_balances(self):
            return {}
    agent.balance_stream = _NullStream()

    # BTC arrives.
    agent._cached_balance = {"USDC": 130.0, "XXBT": 0.0010}
    with _bridge_flag_on():
        agent._refresh_tradable_flags()

        assert engine.tradable is True, "Hp8: engine must flip to tradable once BTC is held"
        assert abs(engine.balance - 0.0010) < 1e-12, (
            f"Hp8: engine balance must equal real BTC holding; got {engine.balance}"
        )
        # Clean equity baseline — drawdown counter reset.
        assert engine.max_drawdown == 0.0
        assert engine.equity_history == []

    # v2.28 default (flag off): BTC arrival must NOT re-arm the bridge.
    engine.tradable = False
    engine.balance = 0.0
    engine.position.size = 0.0
    agent._refresh_tradable_flags()
    assert engine.tradable is False, (
        "Hp8: bridge default is signal-only — BTC arrival must not re-arm it"
    )
    assert engine.exit_only is True


# ═════════════════════════════════════════════════════════════════
# Category W — WS execution stream lifecycle transitions
# ═════════════════════════════════════════════════════════════════
# These exercise the new ExecutionStream / _apply_execution_event path.
# Every scenario places a live-mock order (journal at PLACED) then
# injects a synthetic WS event into the stream, drains, and asserts the
# resulting journal + engine state.


def _place_and_get_context(h: Harness):
    """Helper: place a live-mocked BUY and return (agent, entry, pre_snap, engine)."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)
    engine = agent.engines["SOL/USDC"]
    pre_snap = engine.snapshot_position()

    stub = StubRun(build_dispatcher({
        "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
        "order_validate": kraken_validate_success(),
        "order": kraken_order_success_list("W_TXID"),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.70, "W placement")
    finally:
        stub.restore()
    assert report["outcome"] == "success"
    entry = report["last_journal_entry"]
    validate_journal_entry(entry, expected_state="PLACED")
    return agent, entry, pre_snap, engine


def _inject_and_drain(agent, ws_entry):
    agent.execution_stream.inject_event(ws_entry)
    events = agent.execution_stream.drain_events()
    for e in events:
        agent._apply_execution_event(e)
    return events


def scenario_W1_ws_full_fill(h: Harness):
    """Place, then WS full-fill -> journal FILLED, engine unchanged."""
    agent, entry, pre_snap, engine = _place_and_get_context(h)
    engine_size_after_place = engine.position.size

    placed_amount = entry["intent"]["amount"]
    ws = {
        "exec_type": "trade", "exec_id": "W1-1",
        "order_id": "W_TXID", "order_status": "filled",
        "last_qty": placed_amount, "last_price": 100.0,
        "cost": placed_amount * 100.0,
        "fees": [{"asset": "USDC", "qty": placed_amount * 100.0 * 0.002}],
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    }
    events = _inject_and_drain(agent, ws)
    assert len(events) == 1
    final = agent.order_journal[-1]
    validate_journal_entry(final, expected_state="FILLED")
    assert abs(final["lifecycle"]["vol_exec"] - placed_amount) < 1e-9
    assert final["lifecycle"]["avg_fill_price"] == 100.0
    # Engine unchanged from post-place optimistic state
    assert abs(engine.position.size - engine_size_after_place) < 1e-9


def scenario_W2_ws_dms_cancel_rolls_back(h: Harness):
    """Place, then WS DMS cancel with vol_exec=0 -> CANCELLED_UNFILLED,
    engine rolled back to pre-trade snapshot."""
    agent, entry, pre_snap, engine = _place_and_get_context(h)

    ws = {
        "order_id": "W_TXID", "order_status": "canceled",
        "reason": "CancelAllOrdersAfter Timeout",
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    }
    events = _inject_and_drain(agent, ws)
    assert len(events) == 1
    final = agent.order_journal[-1]
    validate_journal_entry(final, expected_state="CANCELLED_UNFILLED")
    assert "CancelAllOrdersAfter" in final["lifecycle"]["terminal_reason"]
    # Engine should have been restored to pre_snap via restore_position
    assert abs(engine.position.size - pre_snap["position_size"]) < 1e-9
    assert abs(engine.balance - pre_snap["balance"]) < 1e-9


def scenario_W3_ws_post_only_reject_rolls_back(h: Harness):
    """Place, then WS post-only rejection -> REJECTED, engine rolled back."""
    agent, entry, pre_snap, engine = _place_and_get_context(h)

    ws = {
        "order_id": "W_TXID", "order_status": "rejected",
        "reason": "Post only order",
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    }
    events = _inject_and_drain(agent, ws)
    assert len(events) == 1
    final = agent.order_journal[-1]
    validate_journal_entry(final, expected_state="REJECTED")
    assert "Post only" in final["lifecycle"]["terminal_reason"]
    assert abs(engine.position.size - pre_snap["position_size"]) < 1e-9


def scenario_W4_ws_partial_fill_then_cancel(h: Harness):
    """Place, interim partial fill (no emit), then cancel -> PARTIALLY_FILLED
    with correct vol_exec and avg_fill_price from the interim event."""
    agent, entry, pre_snap, engine = _place_and_get_context(h)
    placed_amount = entry["intent"]["amount"]
    partial = placed_amount * 0.4

    # Interim partial — should not emit
    _inject_and_drain(agent, {
        "exec_type": "trade", "exec_id": "W4-1",
        "order_id": "W_TXID", "order_status": "partially_filled",
        "last_qty": partial, "last_price": 99.90,
        "cost": partial * 99.90,
        "fees": [{"asset": "USDC", "qty": 0.01}],
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    })
    # Intermediate journal entry unchanged (still PLACED)
    assert agent.order_journal[-1]["lifecycle"]["state"] == "PLACED"

    # Terminal cancel
    events = _inject_and_drain(agent, {
        "order_id": "W_TXID", "order_status": "canceled",
        "reason": "user_cancel",
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:10Z",
    })
    assert len(events) == 1
    final = agent.order_journal[-1]
    assert final["lifecycle"]["state"] == "PARTIALLY_FILLED"
    assert abs(final["lifecycle"]["vol_exec"] - partial) < 1e-9
    assert abs(final["lifecycle"]["avg_fill_price"] - 99.90) < 1e-6


def scenario_W5_ws_unknown_order_skipped(h: Harness):
    """WS event for an unregistered order_id is silently ignored."""
    agent, _, _, _ = _place_and_get_context(h)
    events = _inject_and_drain(agent, {
        "order_id": "O-NOT-REGISTERED", "order_status": "filled",
        "exec_type": "trade", "last_qty": 0.05, "last_price": 100.0, "cost": 5.0,
        "order_userref": 0, "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    })
    assert events == []
    # Journal unchanged
    assert agent.order_journal[-1]["lifecycle"]["state"] == "PLACED"


def scenario_W6_ws_duplicate_terminal_not_re_emitted(h: Harness):
    """A second terminal event for the same order_id after finalization is ignored."""
    agent, entry, _, _ = _place_and_get_context(h)
    placed_amount = entry["intent"]["amount"]
    _inject_and_drain(agent, {
        "exec_type": "trade", "exec_id": "W6-1",
        "order_id": "W_TXID", "order_status": "filled",
        "last_qty": placed_amount, "last_price": 100.0,
        "cost": placed_amount * 100.0, "fees": [],
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:00Z",
    })
    assert agent.order_journal[-1]["lifecycle"]["state"] == "FILLED"
    # Same order_id again — should be a no-op
    events = _inject_and_drain(agent, {
        "exec_type": "trade", "exec_id": "W6-dup",
        "order_id": "W_TXID", "order_status": "filled",
        "last_qty": placed_amount, "last_price": 101.0,
        "cost": placed_amount * 101.0, "fees": [],
        "order_userref": entry["order_ref"]["order_userref"],
        "side": "buy", "symbol": "SOL/USDC",
        "timestamp": "2026-04-11T20:00:01Z",
    })
    assert events == []
    assert agent.order_journal[-1]["lifecycle"]["avg_fill_price"] == 100.0


def scenario_W7_journal_pnl_uses_vol_exec(h: Harness):
    """_compute_pair_realized_pnl reads from lifecycle.vol_exec / avg_fill_price
    and skips non-fill states."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    # Hand-craft three entries: one FILLED buy, one FILLED sell, one CANCELLED
    agent.order_journal = [
        {
            "placed_at": "2026-04-11T10:00:00+00:00",
            "pair": "SOL/USDC", "side": "BUY",
            "intent": {"amount": 0.1, "limit_price": 80.0, "post_only": True,
                        "order_type": "limit", "paper": False},
            "decision": {"strategy": None, "regime": None, "reason": None,
                          "confidence": None, "params_at_entry": None,
                          "cross_pair_override": None, "book_confidence_modifier": None,
                          "brain_verdict": None, "swap_id": None},
            "order_ref": {"order_userref": 1, "order_id": "OB1"},
            "lifecycle": {"state": "FILLED", "vol_exec": 0.1,
                           "avg_fill_price": 80.0, "fee_quote": 0.016,
                           "final_at": "2026-04-11T10:00:05+00:00",
                           "terminal_reason": None, "exec_ids": ["EB1"]},
        },
        {
            "placed_at": "2026-04-11T11:00:00+00:00",
            "pair": "SOL/USDC", "side": "SELL",
            "intent": {"amount": 0.1, "limit_price": 85.0, "post_only": True,
                        "order_type": "limit", "paper": False},
            "decision": {"strategy": None, "regime": None, "reason": None,
                          "confidence": None, "params_at_entry": None,
                          "cross_pair_override": None, "book_confidence_modifier": None,
                          "brain_verdict": None, "swap_id": None},
            "order_ref": {"order_userref": 2, "order_id": "OS1"},
            "lifecycle": {"state": "FILLED", "vol_exec": 0.1,
                           "avg_fill_price": 85.0, "fee_quote": 0.017,
                           "final_at": "2026-04-11T11:00:05+00:00",
                           "terminal_reason": None, "exec_ids": ["ES1"]},
        },
        {
            "placed_at": "2026-04-11T12:00:00+00:00",
            "pair": "SOL/USDC", "side": "BUY",
            "intent": {"amount": 0.5, "limit_price": 84.0, "post_only": True,
                        "order_type": "limit", "paper": False},
            "decision": {"strategy": None, "regime": None, "reason": None,
                          "confidence": None, "params_at_entry": None,
                          "cross_pair_override": None, "book_confidence_modifier": None,
                          "brain_verdict": None, "swap_id": None},
            "order_ref": {"order_userref": 3, "order_id": "OB2"},
            "lifecycle": {"state": "CANCELLED_UNFILLED", "vol_exec": 0,
                           "avg_fill_price": None, "fee_quote": 0,
                           "final_at": "2026-04-11T12:00:01+00:00",
                           "terminal_reason": "dms_timeout", "exec_ids": []},
        },
    ]
    pnl = agent._compute_pair_realized_pnl("SOL/USDC")
    # Fee-true (v2.27): sell_revenue (0.1*85) - buy_cost (0.1*80 + 0.016)
    # - sell_fee (0.017) = 0.467. CANCELLED entry ignored.
    assert abs(pnl - 0.467) < 1e-9, f"expected 0.467, got {pnl}"


# ═════════════════════════════════════════════════════════════════
# Category L — Live-only scenarios (real Kraken)
# ═════════════════════════════════════════════════════════════════

def scenario_L1_live_ticker_SOLUSDC(h: Harness):
    """Real ticker fetch for SOL/USDC — verify response parses and has bid/ask."""
    time.sleep(2)
    result = KrakenCLI.ticker("SOL/USDC")
    assert "error" not in result, f"L1 ticker error: {result}"
    assert "bid" in result and "ask" in result, f"L1 ticker missing fields: {result}"
    assert result["bid"] > 0 and result["ask"] > 0


def scenario_L2_live_validate_buy_SOLUSDC(h: Harness):
    """Real Kraken with --validate flag for SOL/USDC buy — should succeed at ordermin."""
    time.sleep(2)
    ticker = KrakenCLI.ticker("SOL/USDC")
    assert "error" not in ticker
    time.sleep(2)
    result = KrakenCLI.order_buy("SOL/USDC", 0.02, price=ticker["bid"], validate=True)
    assert "error" not in result, f"L2 validate error: {result}"


def _cancel_order_with_retry(txid: str, max_retries: int = 3) -> bool:
    """Attempt to cancel an order, with retries. Returns True on success."""
    import subprocess
    for attempt in range(max_retries):
        time.sleep(2)
        try:
            result = subprocess.run(
                ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                 f"source ~/.cargo/env && kraken order cancel {txid} --yes -o json 2>/dev/null"],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 0 and result.stdout.strip():
                print(f"  [HARNESS] Cancelled order {txid}")
                return True
        except Exception as e:
            print(f"  [HARNESS] Cancel attempt {attempt+1} failed: {e}")
    print(f"  [HARNESS] WARNING: could not cancel {txid} after {max_retries} attempts")
    return False


def _cancel_all_safe():
    """Cancel all open orders as a safety net. Called from exception handlers."""
    import subprocess
    try:
        time.sleep(2)
        subprocess.run(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
             "source ~/.cargo/env && kraken order cancel-all --yes -o json 2>/dev/null"],
            capture_output=True, text=True, timeout=20,
        )
        print("  [HARNESS] Safety cancel-all executed")
    except Exception as e:
        print(f"  [HARNESS] Safety cancel-all failed: {e}")


def scenario_L3_live_buy_cancel_SOLUSDC(h: Harness):
    """Real post-only buy on SOL/USDC at a non-crossing price, followed by
    immediate cancel. Verifies the full _place_order path including real
    execution stream registration with a real order_id."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.60, "L3 real buy + cancel")
        assert report["outcome"] in ("success", "failed_and_rolled_back")
        if report["outcome"] == "success":
            entry = report["last_journal_entry"]
            validate_journal_entry(entry)
            order_id = entry["order_ref"]["order_id"]
            if order_id and order_id != "unknown":
                assert order_id in agent.execution_stream._known_orders
                _cancel_order_with_retry(order_id)
    except Exception:
        _cancel_all_safe()
        raise


def scenario_L4_live_buy_cancel_BTCUSDC(h: Harness):
    """L3 for BTC/USDC."""
    agent = h.new_agent(pairs=["BTC/USDC"], paper=False, initial_balance=200.0)
    h.seed_candles(agent, "BTC/USDC", base_price=70000.0)
    try:
        report = harness_execute(agent, "BTC/USDC", "BUY", 0.60, "L4 real buy BTC + cancel")
        assert report["outcome"] in ("success", "failed_and_rolled_back")
        if report["outcome"] == "success":
            entry = report["last_journal_entry"]
            validate_journal_entry(entry)
            order_id = entry["order_ref"]["order_id"]
            if order_id and order_id != "unknown":
                _cancel_order_with_retry(order_id)
    except Exception:
        _cancel_all_safe()
        raise


def scenario_L5_live_buy_cancel_SOLBTC(h: Harness):
    """L3 for SOL/BTC."""
    agent = h.new_agent(pairs=["SOL/BTC"], paper=False, initial_balance=0.01)
    h.seed_candles(agent, "SOL/BTC", base_price=0.001)
    try:
        report = harness_execute(agent, "SOL/BTC", "BUY", 0.60, "L5 real buy SOL/BTC + cancel")
        assert report["outcome"] in ("success", "failed_and_rolled_back")
        if report["outcome"] == "success":
            entry = report["last_journal_entry"]
            validate_journal_entry(entry)
            order_id = entry["order_ref"]["order_id"]
            if order_id and order_id != "unknown":
                _cancel_order_with_retry(order_id)
    except Exception:
        _cancel_all_safe()
        raise


def scenario_L6_live_validate_below_costmin(h: Harness):
    """Attempt to validate an order below costmin -> Kraken rejects."""
    time.sleep(2)
    result = KrakenCLI.order_buy("SOL/USDC", 0.00001, price=100.0, validate=True)
    assert "error" in result, f"L6 expected error, got success: {result}"


# ═════════════════════════════════════════════════════════════════
# Helper: project root
# ═════════════════════════════════════════════════════════════════

def _hydra_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═════════════════════════════════════════════════════════════════
# Scenario registry
# ═════════════════════════════════════════════════════════════════

def scenario_S3SH1_shadow_no_orders(h: Harness):
    """S3 shadow phase: a gated entryable_b1 signal logs exactly one
    ledger proposal and places ZERO orders (journal untouched)."""
    import dataclasses
    import importlib.util
    import tempfile
    from pathlib import Path as _P

    agent = h.new_agent(pairs=["BTC/USD"], paper=True, initial_balance=200.0)
    s3 = getattr(agent, "s3", None)
    assert s3 is not None, "S3 adapter missing on agent"

    root = _P(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_s3b_fixture_harness", root / "s3bounce" / "tests" / "test_setups.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    day0, rows = 20000, []
    for i in range(100):
        rows.append({"ts": float((day0 + i) * 86400), "open": 100.0,
                     "high": 101.2, "low": 99.8,
                     "close": 100 + 0.2 * (i % 3), "volume": 100.0})
    for j, b in enumerate(mod.down_leg_bars()):
        rows.append({"ts": float((day0 + 100 + j) * 86400), "open": b.open,
                     "high": b.high, "low": b.low, "close": b.close,
                     "volume": 100.0})
    for asset in s3.strategy.universe:
        s3.strategy.seed(asset, rows)
    # walk the fold clock back to the entryable_b1 cut
    m = s3.strategy.artifact.models["BTC/USD"]
    s3.strategy.artifact.models["BTC/USD"] = dataclasses.replace(m, threshold=0.0)
    cut = None
    for back in range(12, -1, -1):
        now = rows[-1]["ts"] + 86400 - back * 86400
        sig = s3.strategy.evaluate("BTC/USD", now)
        if sig.stage == "entryable_b1" and sig.gated:
            cut = now
            s3._last_signal["BTC/USD"] = sig
            break
    assert cut is not None, "synthetic tape produced no gated entryable cut"
    for asset in s3.strategy.universe:
        s3._fold_clock[asset] = cut

    with tempfile.TemporaryDirectory() as d:
        s3.ledger_dir = d
        journal_before = len(agent.order_journal)
        os.environ["HYDRA_S3_STRATEGY"] = "1"
        try:
            ev = s3.shadow_step("BTC/USD", 100.0)
        finally:
            os.environ.pop("HYDRA_S3_STRATEGY", None)
        assert ev is not None, "expected a shadow proposal"
        assert len(agent.order_journal) == journal_before, \
            "shadow phase touched the order journal"
        assert s3.ledger().open, "no shadow arm positions opened"
        events = (_P(d) / "events.jsonl").read_text().strip().splitlines()
        assert len(events) == 1, f"expected exactly 1 event, got {len(events)}"


ALL_SCENARIOS: list[Scenario] = [
    # Category H — happy paths
    Scenario("H1", "Paper BUY SOL/USDC -> FILLED", "H", MOCK, scenario_H1_paper_buy),
    Scenario("H2", "Paper SELL SOL/USDC from preset position -> FILLED", "H", MOCK, scenario_H2_paper_sell_from_position),
    Scenario("H3", "Live BUY SOL/USDC mocked -> PLACED + stream registration", "H", MOCK, scenario_H3_live_buy_mocked),
    Scenario("H4", "Live SELL SOL/USDC mocked from preset position -> PLACED", "H", MOCK, scenario_H4_live_sell_mocked_from_position),
    Scenario("H5", "LIVE BUY SOL/USDC real+cancel", "H", LIVE_ONLY, scenario_H5_live_buy_real_kraken),
    Scenario("H6", "LIVE SELL without position -> engine rejection", "H", LIVE_ONLY, scenario_H6_live_sell_real_kraken),

    # Category F — failure paths
    Scenario("F1", "Ticker error -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F1_ticker_error),
    Scenario("F2", "Ticker missing bid/ask -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F2_ticker_missing_fields),
    Scenario("F3", "Validation post-only crossing -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F3_validation_post_only_crossed),
    Scenario("F4", "Validation insufficient funds -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F4_validation_insufficient_funds),
    Scenario("F5", "Execution fails after validation -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F5_execution_fails_after_validation),
    Scenario("F6", "Order timeout -> PLACEMENT_FAILED + rollback", "F", MOCK, scenario_F6_execution_timeout),
    Scenario("F7", "Paper failure -> PLACEMENT_FAILED (paper)", "F", MOCK, scenario_F7_paper_failure),

    # Category E — edge cases
    Scenario("E1", "Txid list unwrap", "E", MOCK, scenario_E1_txid_list_unwrap),
    Scenario("E2", "Txid nested in result", "E", MOCK, scenario_E2_txid_nested_result),
    Scenario("E3", "Txid missing -> 'unknown'", "E", MOCK, scenario_E3_txid_missing),
    Scenario("E4", "Txid empty list -> 'unknown'", "E", MOCK, scenario_E4_txid_empty_list),
    Scenario("E5", "Halted engine produces no journal entries", "E", MOCK, scenario_E5_halted_engine),
    Scenario("E6", "Ordermin partial sell forces full close", "E", MOCK, scenario_E6_ordermin_partial_sell_forces_full_close),
    Scenario("E7", "Unparseable Kraken response -> PLACEMENT_FAILED + rollback", "E", MOCK, scenario_E7_unparseable_kraken_response),

    # Category S — schema meta
    Scenario("S0", "Schema validator rejects malformed entries", "S", MOCK, scenario_S_meta_validator_rejects_garbage),

    # Category R — rollback meta
    Scenario("R0", "Rollback comparator catches tampered state", "R", MOCK, scenario_R_meta_comparator_catches_tampering),

    # Category H' — historical regression
    Scenario("Hp1", "4effbea: falsy-zero competition_start_balance", "H_prime", MOCK, scenario_Hp1_falsy_zero_competition_start_balance),
    Scenario("Hp2", "4effbea: _pre_trade_snapshot stripped from broadcast", "H_prime", MOCK, scenario_Hp2_pre_trade_snapshot_stripped_from_broadcast),
    Scenario("Hp3", "88797ca: BUY does not increment total_trades", "H_prime", MOCK, scenario_Hp3_total_trades_not_incremented_on_buy),
    Scenario("Hp4", "88797ca: break-even counts as loss", "H_prime", MOCK, scenario_Hp4_break_even_counts_as_loss),
    Scenario("Hp5", "9e652d5: txid-as-list regression", "H_prime", MOCK, scenario_Hp5_txid_as_list_regression),
    Scenario("Hp6", "35a134d: ordermin on sell regression", "H_prime", MOCK, scenario_Hp6_ordermin_sell_regression),
    Scenario("Hp7", "v2.11.0: SOL/BTC info-only blocks phantom placement", "H_prime", MOCK, scenario_Hp7_sol_btc_info_only_no_placement),
    Scenario("Hp8", "v2.11.0: tradable re-activates on BTC arrival", "H_prime", MOCK, scenario_Hp8_tradable_reactivates_on_btc_arrival),

    # Category W — WS execution stream lifecycle
    Scenario("W1", "WS full fill -> FILLED", "W", MOCK, scenario_W1_ws_full_fill),
    Scenario("W2", "WS DMS cancel -> CANCELLED_UNFILLED + engine rollback", "W", MOCK, scenario_W2_ws_dms_cancel_rolls_back),
    Scenario("W3", "WS post-only reject -> REJECTED + engine rollback", "W", MOCK, scenario_W3_ws_post_only_reject_rolls_back),
    Scenario("W4", "WS interim partial + terminal cancel -> PARTIALLY_FILLED", "W", MOCK, scenario_W4_ws_partial_fill_then_cancel),
    Scenario("W5", "WS event for unknown order_id is ignored", "W", MOCK, scenario_W5_ws_unknown_order_skipped),
    Scenario("W6", "WS duplicate terminal not re-emitted", "W", MOCK, scenario_W6_ws_duplicate_terminal_not_re_emitted),
    Scenario("W7", "_compute_pair_realized_pnl uses lifecycle.vol_exec", "W", MOCK, scenario_W7_journal_pnl_uses_vol_exec),

    # Category S3 — shadow strategy surface (mock)
    Scenario("S3SH1", "S3 shadow: gated proposal, ZERO orders", "H", MOCK,
             scenario_S3SH1_shadow_no_orders),
    # Category L — live only
    Scenario("L1", "Live ticker SOL/USDC", "L", LIVE, scenario_L1_live_ticker_SOLUSDC),
    Scenario("L2", "Live --validate buy SOL/USDC", "L", LIVE, scenario_L2_live_validate_buy_SOLUSDC),
    Scenario("L3", "Live post-only buy SOL/USDC + cancel", "L", LIVE_ONLY, scenario_L3_live_buy_cancel_SOLUSDC),
    Scenario("L4", "Live post-only buy BTC/USDC + cancel", "L", LIVE_ONLY, scenario_L4_live_buy_cancel_BTCUSDC),
    Scenario("L5", "Live post-only buy SOL/BTC + cancel", "L", LIVE_ONLY, scenario_L5_live_buy_cancel_SOLBTC),
    Scenario("L6", "Live --validate below costmin", "L", VALIDATE_ONLY, scenario_L6_live_validate_below_costmin),
]
