"""Unit tests for Phase 6 backend bridge.

Covers:
  - BacktestWorkerPool: submit → worker runs → progress/result broadcast
  - Cancel signals the token; experiment status becomes cancelled
  - Shutdown drains workers cleanly
  - Queue-full raises instead of blocking (live-safety)
  - Exceptions inside a run are logged + error broadcast, pool survives
  - mount_backtest_routes registers all expected inbound handlers
  - Handler behaviors: start → submit; cancel → token set; delete denied;
    missing fields → error; unknown experiment → not found

Also lightly exercises the DashboardBroadcaster's new
`broadcast_message(type, payload)` path with a fake asyncio.run
replacement (we don't spin up real websockets — tests never hit the
network).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest import make_quick_config  # noqa: E402
from hydra_backtest_server import (  # noqa: E402
    BacktestWorkerPool,
    ERROR_LOG_NAME,
    _compact,
    mount_backtest_routes,
)
from hydra_experiments import ExperimentStore, new_experiment  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Mock broadcaster
# ═══════════════════════════════════════════════════════════════

class _MockBroadcaster:
    """Captures broadcast_message calls + stores inbound-handler registrations.

    This matches the Phase 6 DashboardBroadcaster contract without
    starting a real WS server.
    """
    def __init__(self):
        self.msgs: List[Dict[str, Any]] = []
        self.handlers: Dict[str, Callable] = {}
        self._lock = threading.Lock()

    def broadcast_message(self, msg_type, payload):
        with self._lock:
            self.msgs.append({"type": msg_type, **payload})

    def register_handler(self, msg_type, fn):
        self.handlers[msg_type] = fn

    def of_type(self, msg_type):
        with self._lock:
            return [m for m in self.msgs if m["type"] == msg_type]


class _PoolFixture(unittest.TestCase):
    """Shared setUp/tearDown: tmp store, mock broadcaster, pool with 1 worker."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-server-test-"))
        self.store = ExperimentStore(root=self.tmp)
        self.bc = _MockBroadcaster()
        self.pool = BacktestWorkerPool(
            max_workers=1,                 # deterministic for assertions
            store=self.store,
            broadcaster=self.bc,
            error_log_dir=self.tmp,
            progress_every_n_ticks=1,      # emit every tick in tests
        )

    def tearDown(self):
        try:
            self.pool.shutdown(timeout=3.0)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_status(self, exp_id, *states, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.pool.status(exp_id)["status"]
            if s in states:
                return s
            time.sleep(0.05)
        raise AssertionError(f"status did not reach {states} within {timeout}s; last={s}")


# ═══════════════════════════════════════════════════════════════
# Worker pool lifecycle
# ═══════════════════════════════════════════════════════════════

class TestWorkerPoolLifecycle(_PoolFixture):
    def test_submit_runs_to_complete(self):
        cfg = make_quick_config(name="wl1", n_candles=80, seed=3)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, triggered_by="cli",
                                      hypothesis="smoke hypothesis")
        status = self._wait_status(eid, "complete", "failed")
        self.assertEqual(status, "complete")
        exp = self.store.load(eid)
        self.assertEqual(exp.status, "complete")
        self.assertIsNotNone(exp.result)

    def test_progress_and_result_broadcast(self):
        cfg = make_quick_config(name="wl2", n_candles=60, seed=7)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, triggered_by="cli",
                                      hypothesis="progress-broadcast test")
        self._wait_status(eid, "complete", "failed")
        # At least one progress message + exactly one result message
        progress = self.bc.of_type("backtest_progress")
        results = self.bc.of_type("backtest_result")
        self.assertGreaterEqual(len(progress), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["experiment_id"], eid)
        self.assertIn("metrics", results[0])

    def test_cancel_signals_token_and_marks_status(self):
        cfg = make_quick_config(name="wl3", n_candles=5000, seed=1)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, triggered_by="cli",
                                      hypothesis="cancel mid-run test")
        # Let it start, then cancel
        time.sleep(0.1)
        self.assertTrue(self.pool.cancel(eid))
        # Status must resolve to cancelled or complete (race: if it finished
        # between start and cancel, still acceptable)
        status = self._wait_status(eid, "cancelled", "complete")
        self.assertIn(status, ("cancelled", "complete"))

    def test_cancel_unknown_returns_false(self):
        self.assertFalse(self.pool.cancel("no-such-id"))

    def test_queue_full_raises(self):
        # Make a small pool + queue depth to force saturation quickly.
        # Hold the running worker on a threading.Event so the queue
        # actually saturates before submission #3 (otherwise on fast CI
        # runners worker #1 finishes before #3 is submitted and the
        # queue is never full — flaky in CI prior to the v2.13.5 audit).
        import threading
        from unittest.mock import patch
        self.pool.shutdown(timeout=1.0)
        pool = BacktestWorkerPool(
            max_workers=1, store=self.store, broadcaster=self.bc,
            queue_depth=1, error_log_dir=self.tmp,
        )
        hold = threading.Event()
        original_run = __import__("hydra_backtest", fromlist=["BacktestRunner"]).BacktestRunner.run

        def blocking_run(self_runner, *args, **kwargs):
            hold.wait(timeout=5.0)
            return original_run(self_runner, *args, **kwargs)

        try:
            with patch("hydra_backtest.BacktestRunner.run", blocking_run):
                cfg = make_quick_config(name="wlq", n_candles=5000, seed=1)
                cfg = replace(cfg, coordinator_enabled=False)
                # First one goes to the running worker (now blocked);
                # second fills the queue; third should raise queue.Full.
                pool.submit_config(cfg, triggered_by="cli", hypothesis="queue saturate 1")
                pool.submit_config(cfg, triggered_by="cli", hypothesis="queue saturate 2")
                # Give the worker a beat to pick up #1 and start blocking
                # so the queue accounting reflects #2 sitting in the queue.
                deadline = time.time() + 2.0
                while time.time() < deadline and pool.snapshot().get("queue_size", 0) < 1:
                    time.sleep(0.01)
                with self.assertRaises(queue.Full):
                    pool.submit_config(cfg, triggered_by="cli", hypothesis="queue saturate 3")
        finally:
            hold.set()
            pool.shutdown(timeout=5.0)

    def test_shutdown_stops_workers(self):
        alive_before = sum(1 for t in self.pool._workers if t.is_alive())
        self.assertGreaterEqual(alive_before, 1)
        self.pool.shutdown(timeout=3.0)
        # Workers should have exited within the timeout
        for t in self.pool._workers:
            self.assertFalse(t.is_alive(),
                             f"worker {t.name} still alive after shutdown")

    def test_shutdown_refuses_new_submissions(self):
        self.pool.shutdown(timeout=2.0)
        cfg = make_quick_config(name="closed", n_candles=40)
        with self.assertRaises(RuntimeError):
            self.pool.submit_config(cfg, triggered_by="cli",
                                    hypothesis="after shutdown should fail")

    def test_snapshot_shape(self):
        snap = self.pool.snapshot()
        self.assertIn("max_workers", snap)
        self.assertIn("queue_size", snap)
        self.assertIn("worker_threads_alive", snap)
        self.assertIn("running", snap)


# ═══════════════════════════════════════════════════════════════
# Error isolation + logging
# ═══════════════════════════════════════════════════════════════

class TestErrorIsolation(_PoolFixture):
    def test_runner_internal_failure_captured_on_experiment(self):
        # Bad data_source: BacktestRunner.run() catches internally and sets
        # result.status="failed" with traceback on result.errors. The worker
        # broadcasts a result (not error) because no exception propagated to it.
        cfg = make_quick_config(name="err", n_candles=50)
        bad_cfg = replace(cfg, data_source="bogus")
        eid = self.pool.submit_config(bad_cfg, triggered_by="cli",
                                      hypothesis="invalid source path test")
        self._wait_status(eid, "failed", "complete")
        exp = self.store.load(eid)
        self.assertEqual(exp.status, "failed")
        self.assertIsNotNone(exp.result)
        self.assertGreaterEqual(len(exp.result.errors), 1)
        # Result broadcast emitted with failed status
        results = self.bc.of_type("backtest_result")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[-1]["status"], "failed")

    def test_pool_level_exception_logs_to_file(self):
        # Force a pool-level fault by monkey-patching BacktestRunner to raise
        # during construction — this simulates a bug in the runner layer
        # that bypasses BacktestRunner.run()'s internal try/except.
        import hydra_backtest_server as srv
        orig_runner = srv.BacktestRunner
        class _BoomRunner:
            def __init__(self, *a, **kw):
                raise RuntimeError("runner boom")
        srv.BacktestRunner = _BoomRunner
        try:
            cfg = make_quick_config(name="boom", n_candles=40)
            cfg = replace(cfg, coordinator_enabled=False)
            eid = self.pool.submit_config(cfg, triggered_by="cli",
                                          hypothesis="runner explodes on init")
            self._wait_status(eid, "failed")
        finally:
            srv.BacktestRunner = orig_runner

        # Error broadcast + error log file populated
        errors = self.bc.of_type("error")
        self.assertGreaterEqual(len(errors), 1)
        log_path = self.tmp / ERROR_LOG_NAME
        self.assertTrue(log_path.exists())
        record = json.loads(log_path.read_text().splitlines()[-1])
        self.assertEqual(record["experiment_id"], eid)
        self.assertIn("traceback", record)

    def test_pool_survives_after_exception(self):
        cfg = make_quick_config(name="surv1", n_candles=40)
        bad_cfg = replace(cfg, data_source="bogus")
        eid1 = self.pool.submit_config(bad_cfg, triggered_by="cli",
                                       hypothesis="bad run 1")
        self._wait_status(eid1, "failed")

        # Subsequent submissions still work
        good_cfg = replace(cfg, coordinator_enabled=False)
        eid2 = self.pool.submit_config(good_cfg, triggered_by="cli",
                                       hypothesis="follow up good run")
        status = self._wait_status(eid2, "complete", "failed")
        self.assertEqual(status, "complete")


# ═══════════════════════════════════════════════════════════════
# Handler wiring (mount_backtest_routes)
# ═══════════════════════════════════════════════════════════════

class TestMountRoutes(_PoolFixture):
    def setUp(self):
        super().setUp()
        mount_backtest_routes(self.bc, self.pool)

    def test_all_expected_handlers_registered(self):
        expected = {
            "backtest_start", "backtest_cancel",
            "experiment_list_request", "experiment_get_request",
            "experiment_compare_request",
            "experiment_delete", "review_request",
        }
        self.assertTrue(expected.issubset(self.bc.handlers.keys()))

    def test_start_handler_submits(self):
        cfg = make_quick_config(name="mh1", n_candles=60)
        cfg = replace(cfg, coordinator_enabled=False)
        reply = self.bc.handlers["backtest_start"]({
            "config": {**cfg.__dict__},
            "triggered_by": "dashboard",
            "hypothesis": "UI submitted run",
        })
        self.assertTrue(reply["success"])
        self.assertIn("experiment_id", reply)
        self._wait_status(reply["experiment_id"], "complete", "failed")

    def test_start_handler_rejects_missing_config(self):
        reply = self.bc.handlers["backtest_start"]({})
        self.assertFalse(reply["success"])

    def test_start_handler_enforces_dispatcher_quota(self):
        """With a dispatcher mounted, dashboard
        backtest_start must consume QuotaTracker and be denied past the cap."""
        from hydra_backtest_tool import BacktestToolDispatcher, QuotaTracker

        bc2 = _MockBroadcaster()
        dispatcher = BacktestToolDispatcher(
            store=self.store,
            quota=QuotaTracker(per_caller_daily=1, global_daily=1),
            pool=self.pool,
        )
        mount_backtest_routes(bc2, self.pool, dispatcher=dispatcher)

        cfg = make_quick_config(name="mhq", n_candles=60)
        cfg = replace(cfg, coordinator_enabled=False)
        payload = {
            "config": {**cfg.__dict__},
            "triggered_by": "dashboard",
            "hypothesis": "quota pin",
        }
        first = bc2.handlers["backtest_start"](payload)
        self.assertTrue(first["success"], first)
        second = bc2.handlers["backtest_start"](payload)
        self.assertFalse(second["success"])
        self.assertIn("quota", second["error"].lower())
        self._wait_status(first["experiment_id"], "complete", "failed")

    def test_start_handler_invalid_config(self):
        reply = self.bc.handlers["backtest_start"]({
            "config": {"not": "a valid config"},
        })
        self.assertFalse(reply["success"])

    def test_cancel_handler_requires_id(self):
        reply = self.bc.handlers["backtest_cancel"]({})
        self.assertFalse(reply["success"])

    def test_cancel_handler_unknown_id(self):
        reply = self.bc.handlers["backtest_cancel"]({"experiment_id": "x"})
        self.assertFalse(reply["success"])

    def test_cancel_handler_known_id(self):
        cfg = make_quick_config(name="mh2", n_candles=2000)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, hypothesis="cancel me soon", triggered_by="cli")
        reply = self.bc.handlers["backtest_cancel"]({"experiment_id": eid})
        self.assertTrue(reply["success"])

    def test_list_handler(self):
        cfg = make_quick_config(name="mh3", n_candles=60)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, hypothesis="list me please", triggered_by="cli")
        self._wait_status(eid, "complete", "failed")
        reply = self.bc.handlers["experiment_list_request"]({"limit": 10})
        self.assertTrue(reply["success"])
        self.assertGreaterEqual(len(reply["experiments"]), 1)

    def test_get_handler_not_found(self):
        reply = self.bc.handlers["experiment_get_request"]({"experiment_id": "nope"})
        self.assertFalse(reply["success"])

    def test_get_handler_happy(self):
        cfg = make_quick_config(name="mh4", n_candles=60)
        cfg = replace(cfg, coordinator_enabled=False)
        eid = self.pool.submit_config(cfg, hypothesis="fetch me later", triggered_by="cli")
        self._wait_status(eid, "complete", "failed")
        reply = self.bc.handlers["experiment_get_request"]({"experiment_id": eid})
        self.assertTrue(reply["success"])
        self.assertEqual(reply["experiment"]["id"], eid)

    def test_compare_handler_requires_two(self):
        reply = self.bc.handlers["experiment_compare_request"]({"experiment_ids": ["only-one"]})
        self.assertFalse(reply["success"])

    def test_compare_handler_rejects_too_many(self):
        reply = self.bc.handlers["experiment_compare_request"]({
            "experiment_ids": [f"fake-{i}" for i in range(9)],
        })
        self.assertFalse(reply["success"])
        self.assertIn("max 8", reply["error"])

    def test_compare_handler_missing_ids(self):
        reply = self.bc.handlers["experiment_compare_request"]({
            "experiment_ids": ["fake-a", "fake-b"],
        })
        self.assertFalse(reply["success"])
        self.assertIn("missing", reply["error"].lower())

    def test_compare_handler_happy(self):
        cfg = make_quick_config(name="cmp", n_candles=80)
        cfg = replace(cfg, coordinator_enabled=False)
        eid1 = self.pool.submit_config(cfg, hypothesis="compare one", triggered_by="cli")
        eid2 = self.pool.submit_config(cfg, hypothesis="compare two", triggered_by="cli")
        self._wait_status(eid1, "complete", "failed")
        self._wait_status(eid2, "complete", "failed")
        reply = self.bc.handlers["experiment_compare_request"]({
            "experiment_ids": [eid1, eid2],
        })
        self.assertTrue(reply["success"], reply)
        self.assertIn("winner_per_metric", reply)
        self.assertIn("rows", reply)
        self.assertEqual(len(reply["rows"]), 2)
        # Pairwise p-values keyed as "a__b"
        self.assertTrue(any("__" in k for k in reply["pairwise_sharpe_p_values"].keys()))

    def test_delete_handler_always_denied(self):
        reply = self.bc.handlers["experiment_delete"]({"experiment_id": "anything"})
        self.assertFalse(reply["success"])
        self.assertIn("delete is not exposed", reply["error"])

    def test_review_handler_no_reviewer(self):
        # Phase 6 has no reviewer configured
        reply = self.bc.handlers["review_request"]({"experiment_id": "x"})
        self.assertFalse(reply["success"])
        self.assertIn("reviewer not configured", reply["error"])


# ═══════════════════════════════════════════════════════════════
# DashboardBroadcaster refactor (Phase 6 additions)
# ═══════════════════════════════════════════════════════════════

class TestBroadcasterRefactor(unittest.TestCase):
    """Exercises the new type-dispatch bits of DashboardBroadcaster without
    spinning up an actual websocket server — we only verify the handler
    registry and the inbound-dispatch coroutine logic."""

    def setUp(self):
        from hydra_ws_server import DashboardBroadcaster
        self.DashboardBroadcaster = DashboardBroadcaster
        self.bc = DashboardBroadcaster(host="127.0.0.1", port=0)
        # No start() — we never accept real connections.

    def test_register_and_dispatch(self):
        calls = []
        self.bc.register_handler("ping", lambda payload: calls.append(payload) or {"pong": True})

        # Simulate the inbound dispatch directly.
        class _FakeWS:
            def __init__(self):
                self.sent = []
            async def send(self, msg):
                self.sent.append(msg)

        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                json.dumps({
                    "type": "ping", "payload": "hello",
                    "auth": self.bc.auth_token,
                }), ws,
            ))
        finally:
            loop.close()

        self.assertEqual(calls, [{"payload": "hello"}])
        self.assertEqual(len(ws.sent), 1)
        reply = json.loads(ws.sent[0])
        self.assertEqual(reply["type"], "ping_ack")
        self.assertTrue(reply["pong"])

    def test_unknown_type_silently_dropped(self):
        class _FakeWS:
            def __init__(self): self.sent = []
            async def send(self, msg): self.sent.append(msg)
        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                json.dumps({"type": "no_handler", "x": 1}), ws,
            ))
        finally:
            loop.close()
        self.assertEqual(ws.sent, [])

    def test_malformed_json_does_not_raise(self):
        class _FakeWS:
            def __init__(self): self.sent = []
            async def send(self, msg): self.sent.append(msg)
        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                "not json {[", ws,
            ))
        finally:
            loop.close()
        self.assertEqual(ws.sent, [])

    def test_handler_exception_becomes_error_reply(self):
        def boom(_p): raise RuntimeError("oops")
        self.bc.register_handler("do", boom)
        class _FakeWS:
            def __init__(self): self.sent = []
            async def send(self, msg): self.sent.append(msg)
        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                json.dumps({"type": "do", "auth": self.bc.auth_token}), ws,
            ))
        finally:
            loop.close()
        reply = json.loads(ws.sent[0])
        self.assertFalse(reply["success"])
        self.assertIn("RuntimeError", reply["error"])

    def test_missing_auth_rejected(self):
        self.bc.register_handler("ping", lambda _p: {"pong": True})
        class _FakeWS:
            def __init__(self): self.sent = []
            async def send(self, msg): self.sent.append(msg)
        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                json.dumps({"type": "ping"}), ws,  # no auth
            ))
        finally:
            loop.close()
        self.assertEqual(len(ws.sent), 1)
        reply = json.loads(ws.sent[0])
        self.assertFalse(reply["success"])
        self.assertEqual(reply["error"], "auth_required")

    def test_wrong_auth_rejected(self):
        self.bc.register_handler("ping", lambda _p: {"pong": True})
        class _FakeWS:
            def __init__(self): self.sent = []
            async def send(self, msg): self.sent.append(msg)
        import asyncio
        ws = _FakeWS()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.bc._dispatch_inbound(
                json.dumps({"type": "ping", "auth": "wrong"}), ws,
            ))
        finally:
            loop.close()
        reply = json.loads(ws.sent[0])
        self.assertEqual(reply["error"], "auth_required")

    def test_origin_check(self):
        self.assertTrue(self.bc._origin_allowed(""))
        self.assertTrue(self.bc._origin_allowed("http://localhost:5173"))
        self.assertTrue(self.bc._origin_allowed("http://127.0.0.1:5173"))
        self.assertFalse(self.bc._origin_allowed("http://evil.com"))
        self.assertFalse(self.bc._origin_allowed("https://example.com"))

    def test_broadcast_message_is_noop_without_loop(self):
        # No asyncio loop started → broadcast_message silently skips
        # (matches broadcast's behavior). No exception.
        self.bc.broadcast_message("foo", {"x": 1})

    def test_broadcast_noop_without_clients(self):
        self.bc.broadcast({"live": "state"})
        # No crash; just stored latest_state
        self.assertEqual(self.bc.latest_state, {"live": "state"})


# ═══════════════════════════════════════════════════════════════
# _compact helper
# ═══════════════════════════════════════════════════════════════

class TestCompact(unittest.TestCase):
    def test_compact_no_result(self):
        cfg = make_quick_config(name="c1", n_candles=30)
        exp = new_experiment(name="c1", config=cfg)
        out = _compact(exp)
        self.assertIsNone(out["metrics"])
        self.assertEqual(out["id"], exp.id)

    def test_compact_includes_metrics(self):
        from hydra_backtest import BacktestResult, BacktestMetrics
        cfg = make_quick_config(name="c2", n_candles=30)
        exp = new_experiment(name="c2", config=cfg)
        m = BacktestMetrics(total_trades=5, sharpe=1.234, total_return_pct=2.5)
        exp.result = BacktestResult(config=cfg, status="complete", metrics=m)
        out = _compact(exp)
        self.assertEqual(out["metrics"]["total_trades"], 5)
        self.assertAlmostEqual(out["metrics"]["sharpe"], 1.234)


class TestWorkerPoolPrune(_PoolFixture):
    def test_terminal_entries_pruned_after_run(self):
        """_cancel_tokens and _status_cache entries for completed experiments
        should be pruned after _PRUNE_AFTER_SEC, not grow forever."""
        cfg = make_quick_config(pairs=("SOL/USD",))
        eid = self.pool.submit_config(cfg, triggered_by="test")
        self._wait_status(eid, "complete", "failed")

        # Immediately after completion, entry should still be queryable
        self.assertIn(self.pool.status(eid)["status"], ("complete", "failed"))

        # Entry exists in status cache
        with self.pool._lock:
            has_cache = eid in self.pool._status_cache
        self.assertTrue(has_cache, "_status_cache should have the entry post-run")

        # Force-expire by backdating _completed_at, then submit+complete
        # another experiment to trigger a prune cycle
        with self.pool._lock:
            self.assertIn(eid, self.pool._completed_at,
                          "_completed_at should be set on completion")
            self.pool._completed_at[eid] = time.time() - 120  # 2 min ago

        cfg2 = make_quick_config(pairs=("BTC/USD",))
        eid2 = self.pool.submit_config(cfg2, triggered_by="test")
        self._wait_status(eid2, "complete", "failed")

        # After second run completes, the first (expired) entry should be pruned
        with self.pool._lock:
            pruned_token = eid not in self.pool._cancel_tokens
            pruned_cache = eid not in self.pool._status_cache
            pruned_completed = eid not in self.pool._completed_at

        self.assertTrue(pruned_token, "_cancel_tokens should prune expired entry")
        self.assertTrue(pruned_cache, "_status_cache should prune expired entry")
        self.assertTrue(pruned_completed, "_completed_at should prune expired entry")


if __name__ == "__main__":
    unittest.main()
