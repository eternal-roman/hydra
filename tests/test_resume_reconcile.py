"""
HYDRA Resume Reconciliation Test Suite
Validates _reconcile_stale_placed(): on --resume, PLACED journal entries from
previous sessions are queried against the exchange and finalized or re-registered.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import HydraAgent
from hydra_kraken_cli import KrakenCLI
from hydra_streams import ExecutionStream, FakeExecutionStream
from hydra_engine import HydraEngine


# ═══════════════════════════════════════════════════════════════
# Stub helpers
# ═══════════════════════════════════════════════════════════════

class _StubRun:
    def __init__(self, response):
        self._response = response
        self.calls = []
        self._original = None

    def install(self):
        self._original = KrakenCLI._run
        outer = self

        def fake(args, timeout=20):
            outer.calls.append(list(args))
            if callable(outer._response):
                return outer._response(args)
            return outer._response

        KrakenCLI._run = staticmethod(fake)

    def restore(self):
        if self._original is not None:
            KrakenCLI._run = staticmethod(self._original)
            self._original = None


def _make_agent(journal=None, pairs=None):
    """Minimal HydraAgent with just enough state for _reconcile_stale_placed."""
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.pairs = pairs or ["SOL/USDC"]
    agent.order_journal = journal if journal is not None else []
    agent.execution_stream = FakeExecutionStream()
    agent.execution_stream.start()
    # Create minimal engines
    agent.engines = {}
    for pair in agent.pairs:
        engine = object.__new__(HydraEngine)
        engine.prices = [100.0]
        agent.engines[pair] = engine
    return agent


def _make_placed_entry(pair="SOL/USDC", side="BUY", order_id="TX_ABC",
                        userref=12345, amount=1.0):
    """Create a minimal PLACED journal entry."""
    return {
        "placed_at": "2026-04-12T20:00:00Z",
        "pair": pair,
        "side": side,
        "intent": {
            "amount": amount,
            "limit_price": 130.0,
            "post_only": True,
            "order_type": "limit",
            "paper": False,
        },
        "decision": {
            "strategy": "MOMENTUM",
            "regime": "TREND_UP",
            "reason": "test",
            "confidence": 0.75,
        },
        "order_ref": {
            "order_userref": userref,
            "order_id": order_id,
        },
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


def _make_filled_entry(pair="SOL/USDC", side="BUY", order_id="TX_DONE"):
    """Create a journal entry already in FILLED state."""
    entry = _make_placed_entry(pair=pair, side=side, order_id=order_id)
    entry["lifecycle"]["state"] = "FILLED"
    entry["lifecycle"]["vol_exec"] = 1.0
    entry["lifecycle"]["avg_fill_price"] = 130.0
    entry["lifecycle"]["final_at"] = "2026-04-12T20:05:00Z"
    return entry


# ═══════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════

class TestReconcileStalePlaced:

    def test_no_placed_entries_noop(self):
        """Journal with no PLACED entries → no API calls."""
        journal = [_make_filled_entry()]
        agent = _make_agent(journal=journal)
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            agent._reconcile_stale_placed()
            assert stub.calls == [], "no API calls when no PLACED entries"
        finally:
            stub.restore()

    def test_empty_journal_noop(self):
        """Empty journal → no API calls."""
        agent = _make_agent(journal=[])
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            agent._reconcile_stale_placed()
            assert stub.calls == []
        finally:
            stub.restore()

    def test_placed_filled_updates_journal(self):
        """PLACED + exchange fill trues avg_entry to the exchange price.

        The optimistic book is the candle close. The journal carries
        snapshot_position() from before that book, and the exchange price
        is different. true_up_fill must win.
        """
        candle_close = 100.0
        exchange_px = 130.50
        eng = HydraEngine(initial_balance=5000.0, asset="SOL/USDC")
        eng.ingest_candle({
            "open": candle_close, "high": candle_close + 0.5,
            "low": candle_close - 0.5, "close": candle_close,
            "volume": 10.0, "timestamp": 1_700_000_000.0,
        })
        assert eng.prices[-1] == candle_close
        snap = eng.snapshot_position()
        # Optimistic fill at the candle close, not the exchange price.
        eng.position.size = 1.0
        eng.position.avg_entry = candle_close
        eng.balance -= candle_close

        entry = _make_placed_entry(order_id="TX_FILL", amount=1.0)
        entry["intent"]["limit_price"] = candle_close
        entry["pre_trade_snapshot"] = snap
        agent = _make_agent(journal=[entry])
        agent.engines["SOL/USDC"] = eng

        resp = {"TX_FILL": {
            "status": "closed", "vol_exec": "1.0", "price": str(exchange_px),
            "fee": "0.065", "closetm": "2026-04-12T20:10:00Z",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        lc = entry["lifecycle"]
        assert lc["state"] == "FILLED"
        assert lc["vol_exec"] == 1.0
        assert lc["avg_fill_price"] == exchange_px
        assert lc["fee_quote"] == 0.065
        assert "reconciled on resume" in lc["terminal_reason"]
        assert eng.position.avg_entry == exchange_px
        assert eng.position.avg_entry != candle_close

    def test_placed_partially_filled_closed(self):
        """PLACED + exchange closed with partial fill → PARTIALLY_FILLED."""
        entry = _make_placed_entry(order_id="TX_PART", amount=2.0)
        agent = _make_agent(journal=[entry])

        resp = {"TX_PART": {
            "status": "closed", "vol_exec": "0.5", "price": "130.00",
            "fee": "0.03",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert entry["lifecycle"]["state"] == "PARTIALLY_FILLED"
        assert entry["lifecycle"]["vol_exec"] == 0.5

    def test_placed_cancelled_updates_journal(self):
        """PLACED + exchange says canceled with no fill → CANCELLED_UNFILLED."""
        entry = _make_placed_entry(order_id="TX_CANC", amount=1.0)
        agent = _make_agent(journal=[entry])

        resp = {"TX_CANC": {
            "status": "canceled", "vol_exec": "0", "price": "0", "fee": "0",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert entry["lifecycle"]["state"] == "CANCELLED_UNFILLED"
        assert entry["lifecycle"]["vol_exec"] == 0.0

    def test_placed_still_open_registered(self):
        """PLACED + exchange says open → registered with ExecutionStream."""
        entry = _make_placed_entry(order_id="TX_OPEN", amount=1.0, userref=99999)
        agent = _make_agent(journal=[entry])

        resp = {"TX_OPEN": {
            "status": "open", "vol_exec": "0", "price": "0", "fee": "0",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        # Journal should still be PLACED (WS stream will finalize it)
        assert entry["lifecycle"]["state"] == "PLACED"
        # But order should be registered with the execution stream
        assert "TX_OPEN" in agent.execution_stream._known_orders

    def test_placed_unknown_order_id_skipped(self):
        """PLACED with order_id='unknown' → skipped, no API call."""
        entry = _make_placed_entry(order_id="unknown")
        agent = _make_agent(journal=[entry])

        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            agent._reconcile_stale_placed()
            assert stub.calls == []
        finally:
            stub.restore()

    def test_placed_none_order_id_skipped(self):
        """PLACED with order_id=None → skipped."""
        entry = _make_placed_entry(order_id="TX_X")
        entry["order_ref"]["order_id"] = None
        agent = _make_agent(journal=[entry])

        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            agent._reconcile_stale_placed()
            assert stub.calls == []
        finally:
            stub.restore()

    def test_api_error_graceful(self):
        """Query error → entries left as PLACED, no crash."""
        entry = _make_placed_entry(order_id="TX_ERR")
        agent = _make_agent(journal=[entry])

        stub = _StubRun({"error": "EAPI:Rate limit"})
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        # Entry should remain PLACED
        assert entry["lifecycle"]["state"] == "PLACED"

    def test_already_terminal_entries_skipped(self):
        """FILLED/CANCELLED entries should not be re-queried."""
        filled = _make_filled_entry(order_id="TX_DONE")
        placed = _make_placed_entry(order_id="TX_NEED")
        agent = _make_agent(journal=[filled, placed])

        resp = {"TX_NEED": {
            "status": "closed", "vol_exec": "1.0", "price": "130.00",
            "fee": "0.05",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        # Only TX_NEED should have been queried (in the query-orders call)
        query_calls = [c for c in stub.calls if c[0] == "query-orders"]
        assert len(query_calls) == 1
        assert "TX_NEED" in query_calls[0]
        assert "TX_DONE" not in query_calls[0]

        # TX_NEED finalized, TX_DONE unchanged
        assert placed["lifecycle"]["state"] == "FILLED"
        assert filled["lifecycle"]["state"] == "FILLED"

    def test_batching(self):
        """25 PLACED entries → 2 batch API calls."""
        entries = [_make_placed_entry(order_id=f"TX_{i:03d}", userref=i)
                   for i in range(25)]
        agent = _make_agent(journal=entries)

        def query_resp(args):
            result = {}
            for a in args:
                if a.startswith("TX_"):
                    result[a] = {"status": "open", "vol_exec": "0", "price": "0", "fee": "0"}
            return result

        stub = _StubRun(query_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        query_calls = [c for c in stub.calls if c[0] == "query-orders"]
        assert len(query_calls) == 2
        # First batch: 20 txids + --trades
        assert len(query_calls[0]) == 1 + 20 + 1
        # Second batch: 5 txids + --trades
        assert len(query_calls[1]) == 1 + 5 + 1

    def test_mixed_outcomes(self):
        """Multiple entries with different exchange statuses handled correctly."""
        e_filled = _make_placed_entry(order_id="TX_F", amount=1.0)
        e_canc = _make_placed_entry(order_id="TX_C", amount=1.0)
        e_open = _make_placed_entry(order_id="TX_O", amount=1.0, userref=77777)
        agent = _make_agent(journal=[e_filled, e_canc, e_open])

        resp = {
            "TX_F": {"status": "closed", "vol_exec": "1.0", "price": "130.0", "fee": "0.05"},
            "TX_C": {"status": "canceled", "vol_exec": "0", "price": "0", "fee": "0"},
            "TX_O": {"status": "open", "vol_exec": "0", "price": "0", "fee": "0"},
        }
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert e_filled["lifecycle"]["state"] == "FILLED"
        assert e_canc["lifecycle"]["state"] == "CANCELLED_UNFILLED"
        assert e_open["lifecycle"]["state"] == "PLACED"  # still open
        assert "TX_O" in agent.execution_stream._known_orders

    def test_duplicate_order_ids_deduped(self):
        """Same order_id in two entries → only one API query."""
        e1 = _make_placed_entry(order_id="TX_DUP")
        e2 = _make_placed_entry(order_id="TX_DUP")
        agent = _make_agent(journal=[e1, e2])

        resp = {"TX_DUP": {
            "status": "closed", "vol_exec": "1.0", "price": "130.0", "fee": "0.05",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                agent._reconcile_stale_placed()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        query_calls = [c for c in stub.calls if c[0] == "query-orders"]
        assert len(query_calls) == 1
        # Only one TX_DUP in the args (not two)
        assert query_calls[0].count("TX_DUP") == 1


# ═══════════════════════════════════════════════════════════════
# Userref counter persistence (Fix 2 — restart collision prevention)
# ═══════════════════════════════════════════════════════════════

class TestUserrefPersistence:
    """Fix 2: _userref_counter must never re-issue a value still in the
    journal/exchange after a restart. The current-time seed in __init__ is
    only a floor; _reseed_userref_from_history raises it above anything
    historically used, and _save_snapshot persists it so --resume inherits.
    """

    def _bare_agent(self, journal=None):
        """Construct a minimal agent with only the fields we need for the
        userref helpers. Bypasses the full __init__."""
        agent = object.__new__(HydraAgent)
        agent.order_journal = journal if journal is not None else []
        agent._userref_counter = 100  # low baseline
        return agent

    def test_journal_max_userref_scans_entries(self):
        journal = [
            {"order_ref": {"order_userref": 500, "order_id": "A"}},
            {"order_ref": {"order_userref": 1200, "order_id": "B"}},
            {"order_ref": {"order_userref": 800, "order_id": "C"}},
            # Non-int and malformed entries should be ignored, not raise
            {"order_ref": {"order_userref": None}},
            {"order_ref": None},
            {},
            "not a dict",
        ]
        agent = self._bare_agent(journal)
        assert agent._journal_max_userref() == 1200

    def test_journal_max_userref_empty(self):
        agent = self._bare_agent([])
        assert agent._journal_max_userref() == 0

    def test_reseed_raises_counter_above_journal_max(self):
        journal = [{"order_ref": {"order_userref": 99999, "order_id": "A"}}]
        agent = self._bare_agent(journal)
        agent._userref_counter = 50000  # below journal_max
        agent._reseed_userref_from_history()
        # Must be > 99999 + safety gap
        assert agent._userref_counter >= 99999 + HydraAgent._USERREF_SAFETY_GAP

    def test_reseed_does_not_lower_counter(self):
        journal = [{"order_ref": {"order_userref": 100, "order_id": "A"}}]
        agent = self._bare_agent(journal)
        agent._userref_counter = 500_000  # already far above
        agent._reseed_userref_from_history()
        # Must not decrease
        assert agent._userref_counter == 500_000

    def test_reseed_noop_on_empty_journal(self):
        agent = self._bare_agent([])
        agent._userref_counter = 42
        agent._reseed_userref_from_history()
        assert agent._userref_counter == 42

    def test_next_userref_monotonic(self):
        agent = self._bare_agent([])
        agent._userref_counter = 100
        a = agent._next_userref()
        b = agent._next_userref()
        c = agent._next_userref()
        assert a < b < c
        assert b == a + 1
        assert c == a + 2

    def test_next_userref_wrap_consults_journal(self):
        # Near the int32 ceiling; wrap should reseed above journal_max, not
        # blindly reset to a time-seed that could collide with old entries.
        journal = [{"order_ref": {"order_userref": 1_000_000, "order_id": "A"}}]
        agent = self._bare_agent(journal)
        agent._userref_counter = 0x7FFFFFFF  # next += 1 triggers wrap
        new = agent._next_userref()
        # Reseed should be at least max(time_seed, 1_000_000 + safety_gap)
        assert new >= 1_000_000 + HydraAgent._USERREF_SAFETY_GAP
        assert new <= 0x7FFFFFFF

    def test_snapshot_round_trip_persists_counter(self):
        """_save_snapshot writes userref_counter; _load_snapshot reads it and
        raises (never lowers) _userref_counter."""
        import json as _json
        import tempfile
        from hydra_agent import CrossPairCoordinator

        # Build a reasonably complete agent for the save/load path.
        agent = object.__new__(HydraAgent)
        agent.mode = "competition"
        agent.paper = True
        agent.pairs = ["SOL/USDC"]
        agent._competition_start_balance = 100.0
        agent.engines = {}
        agent.coordinator = CrossPairCoordinator(["SOL/USDC"])
        agent.order_journal = []
        agent._userref_counter = 777_777

        with tempfile.TemporaryDirectory() as tmp:
            agent._snapshot_dir = tmp
            agent._save_snapshot()

            # Verify the file actually persisted it
            snap_path = agent._snapshot_path()
            with open(snap_path) as f:
                on_disk = _json.load(f)
            assert on_disk["userref_counter"] == 777_777

            # Fresh agent, low initial counter, loads from disk.
            agent2 = object.__new__(HydraAgent)
            agent2.mode = "competition"
            agent2.paper = True
            agent2.pairs = ["SOL/USDC"]
            agent2._competition_start_balance = None
            agent2.engines = {}
            agent2.coordinator = CrossPairCoordinator(["SOL/USDC"])
            agent2.order_journal = []
            agent2._snapshot_dir = tmp
            agent2._userref_counter = 100  # below persisted
            agent2._load_snapshot()
            assert agent2._userref_counter >= 777_777

            # And if the in-memory floor was already higher, load must NOT
            # lower it.
            agent3 = object.__new__(HydraAgent)
            agent3.mode = "competition"
            agent3.paper = True
            agent3.pairs = ["SOL/USDC"]
            agent3._competition_start_balance = None
            agent3.engines = {}
            agent3.coordinator = CrossPairCoordinator(["SOL/USDC"])
            agent3.order_journal = []
            agent3._snapshot_dir = tmp
            agent3._userref_counter = 2_000_000  # already higher
            agent3._load_snapshot()
            assert agent3._userref_counter == 2_000_000


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [TestReconcileStalePlaced, TestUserrefPersistence]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                import traceback
                print(f"  FAIL  {test_name} (error): {e}")
                traceback.print_exc()

    print(f"\n  {'='*60}")
    print(f"  Resume Reconcile Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
