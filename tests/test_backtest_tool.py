"""Unit tests for hydra_backtest_tool (Phase 4): tool schemas, quota
tracker, dispatcher routing + error handling + audit side effects.

Stdlib unittest; matches project style.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest_tool import (  # noqa: E402
    BACKTEST_TOOLS,
    BacktestToolDispatcher,
    QuotaTracker,
    _downsample,
    _finite_or,
)


class _TempStoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-tool-test-"))
        self.dispatcher = BacktestToolDispatcher(store_root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# Tool schemas
# ═══════════════════════════════════════════════════════════════

class TestToolSchemas(unittest.TestCase):
    def test_all_tools_have_name_description_input_schema(self):
        for tool in BACKTEST_TOOLS:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("input_schema", tool)
            self.assertEqual(tool["input_schema"]["type"], "object")

    def test_names_are_unique(self):
        names = [t["name"] for t in BACKTEST_TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_run_backtest_requires_preset_and_hypothesis(self):
        tool = next(t for t in BACKTEST_TOOLS if t["name"] == "run_backtest")
        required = tool["input_schema"].get("required", [])
        self.assertIn("preset", required)
        self.assertIn("hypothesis", required)

    def test_preset_enum_matches_library(self):
        # The schema enum must be a subset of actual PRESET_LIBRARY keys
        from hydra_experiments import PRESET_LIBRARY
        tool = next(t for t in BACKTEST_TOOLS if t["name"] == "run_backtest")
        enum = tool["input_schema"]["properties"]["preset"]["enum"]
        for name in enum:
            self.assertIn(name, PRESET_LIBRARY)


# ═══════════════════════════════════════════════════════════════
# QuotaTracker
# ═══════════════════════════════════════════════════════════════

class TestQuotaTracker(unittest.TestCase):
    def test_acquire_under_cap(self):
        q = QuotaTracker(per_caller_daily=5, per_caller_concurrent=2, global_daily=10)
        ok, _ = q.acquire("brain:analyst")
        self.assertTrue(ok)
        ok, _ = q.acquire("brain:analyst")
        self.assertTrue(ok)

    def test_concurrent_cap(self):
        q = QuotaTracker(per_caller_daily=10, per_caller_concurrent=2, global_daily=10)
        q.acquire("brain:analyst")
        q.acquire("brain:analyst")
        ok, reason = q.acquire("brain:analyst")
        self.assertFalse(ok)
        self.assertIn("concurrent", reason)

    def test_release_frees_concurrent(self):
        q = QuotaTracker(per_caller_daily=10, per_caller_concurrent=1)
        q.acquire("brain:analyst")
        ok, _ = q.acquire("brain:analyst")
        self.assertFalse(ok)
        q.release("brain:analyst")
        ok, _ = q.acquire("brain:analyst")
        self.assertTrue(ok)

    def test_release_safe_when_not_acquired(self):
        q = QuotaTracker()
        q.release("never-acquired")   # should not raise

    def test_per_caller_daily_cap(self):
        q = QuotaTracker(per_caller_daily=2, per_caller_concurrent=5)
        q.acquire("brain:analyst"); q.release("brain:analyst")
        q.acquire("brain:analyst"); q.release("brain:analyst")
        ok, reason = q.acquire("brain:analyst")
        self.assertFalse(ok)
        self.assertIn("daily cap", reason)

    def test_global_daily_cap(self):
        q = QuotaTracker(per_caller_daily=10, per_caller_concurrent=5, global_daily=3)
        for _ in range(3):
            ok, _ = q.acquire("caller-a")
            self.assertTrue(ok)
            q.release("caller-a")
        ok, reason = q.acquire("caller-b")
        self.assertFalse(ok)
        self.assertIn("global daily cap", reason)

    def test_cost_greater_than_one(self):
        q = QuotaTracker(per_caller_daily=5, global_daily=5)
        ok, _ = q.acquire("caller-a", cost=3)
        self.assertTrue(ok)
        ok, reason = q.acquire("caller-a", cost=3)
        self.assertFalse(ok)

    def test_can_acquire_is_pure(self):
        q = QuotaTracker(per_caller_daily=1)
        ok1, _ = q.can_acquire("caller-a")
        ok2, _ = q.can_acquire("caller-a")
        self.assertTrue(ok1 and ok2)   # no state mutation

    def test_snapshot_shape(self):
        q = QuotaTracker()
        q.acquire("brain:analyst")
        snap = q.snapshot()
        self.assertIn("global_daily", snap)
        self.assertIn("per_caller", snap)
        self.assertIn("brain:analyst", snap["per_caller"])

    def test_thread_safe_concurrent_acquire(self):
        q = QuotaTracker(per_caller_daily=1000, per_caller_concurrent=50, global_daily=1000)
        ok_count = [0]
        lock = threading.Lock()

        def worker():
            ok, _ = q.acquire("caller-a")
            if ok:
                with lock:
                    ok_count[0] += 1
                q.release("caller-a")

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(ok_count[0], 30)


# ═══════════════════════════════════════════════════════════════
# Dispatcher routing
# ═══════════════════════════════════════════════════════════════

class TestDispatcherRouting(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_unknown_tool_returns_error(self):
        out = self.dispatcher.execute("nope", {}, caller="test")
        self.assertFalse(out["success"])
        self.assertIn("unknown tool", out["error"])
        self.assertIn("known_tools", out)

    def test_list_presets_read_only(self):
        out = self.dispatcher.execute("list_presets", {}, caller="brain:analyst")
        self.assertTrue(out["success"])
        self.assertGreater(len(out["data"]), 0)
        # Read-only: no quota consumption
        snap = self.dispatcher.quota.snapshot()
        analyst = snap["per_caller"].get("brain:analyst", {})
        self.assertEqual(analyst.get("daily_count", 0), 0)

    def test_run_backtest_happy(self):
        out = self.dispatcher.execute("run_backtest", {
            "preset": "default",
            "hypothesis": "smoke test default preset",
            "n_candles": 80,
        }, caller="brain:analyst")
        self.assertTrue(out["success"], out)
        self.assertIn("id", out["data"])
        self.assertIn("metrics", out["data"])

    def test_run_backtest_missing_hypothesis(self):
        out = self.dispatcher.execute("run_backtest", {
            "preset": "default",
        }, caller="brain:analyst")
        self.assertFalse(out["success"])

    def test_run_backtest_short_hypothesis(self):
        out = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "short",
        }, caller="brain:analyst")
        self.assertFalse(out["success"])

    def test_run_backtest_unknown_preset(self):
        out = self.dispatcher.execute("run_backtest", {
            "preset": "doesnotexist",
            "hypothesis": "just trying", "n_candles": 50,
        }, caller="brain:analyst")
        self.assertFalse(out["success"])
        self.assertIn("preset", out["error"].lower())

    def test_get_experiment_not_found(self):
        out = self.dispatcher.execute("get_experiment",
                                      {"experiment_id": "no-such"}, caller="cli")
        self.assertFalse(out["success"])
        self.assertIn("not found", out["error"].lower())

    def test_get_experiment_missing_id(self):
        out = self.dispatcher.execute("get_experiment", {}, caller="cli")
        self.assertFalse(out["success"])

    def test_full_create_then_fetch_cycle(self):
        created = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "cycle test run", "n_candles": 80,
        }, caller="cli")
        self.assertTrue(created["success"])
        eid = created["data"]["id"]
        fetched = self.dispatcher.execute("get_experiment",
                                          {"experiment_id": eid}, caller="cli")
        self.assertTrue(fetched["success"])
        self.assertEqual(fetched["data"]["id"], eid)

    def test_list_experiments_filters(self):
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "list test", "n_candles": 60,
        }, caller="cli")
        out = self.dispatcher.execute("list_experiments", {"limit": 10}, caller="cli")
        self.assertTrue(out["success"])
        self.assertGreaterEqual(len(out["data"]), 1)

    def test_list_experiments_tag_filter(self):
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "tag filter test",
            "n_candles": 60,
        }, caller="cli")
        out = self.dispatcher.execute("list_experiments",
                                      {"tag": "caller:cli"}, caller="cli")
        self.assertTrue(out["success"])
        self.assertGreaterEqual(len(out["data"]), 1)
        for exp in out["data"]:
            self.assertIn("caller:cli", exp["tags"])

    def test_find_best_returns_none_when_no_qualifying_experiments(self):
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "find best test",
            "n_candles": 60,
        }, caller="cli")
        out = self.dispatcher.execute(
            "find_best",
            {"metric": "sharpe", "min_trades": 10000},
            caller="cli",
        )
        self.assertTrue(out["success"])
        self.assertIsNone(out["data"])

    def test_compare_experiments_requires_two(self):
        out = self.dispatcher.execute("compare_experiments",
                                      {"experiment_ids": ["only-one"]},
                                      caller="cli")
        self.assertFalse(out["success"])

    def test_compare_experiments_missing_ids(self):
        out = self.dispatcher.execute(
            "compare_experiments",
            {"experiment_ids": ["fake-a", "fake-b"]},
            caller="cli",
        )
        self.assertFalse(out["success"])
        self.assertIn("missing", out["error"].lower())

    def test_compare_experiments_happy(self):
        e1 = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "compare a", "n_candles": 80,
        }, caller="cli")["data"]["id"]
        e2 = self.dispatcher.execute("run_backtest", {
            "preset": "divergent", "hypothesis": "compare b", "n_candles": 80,
        }, caller="cli")["data"]["id"]
        out = self.dispatcher.execute(
            "compare_experiments", {"experiment_ids": [e1, e2]}, caller="cli",
        )
        self.assertTrue(out["success"], out)
        self.assertIn("winner_per_metric", out["data"])
        self.assertIn("rows", out["data"])

    def test_get_equity_curve_downsamples(self):
        eid = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "equity curve test",
            "n_candles": 400,
        }, caller="cli")["data"]["id"]
        out = self.dispatcher.execute("get_equity_curve", {
            "experiment_id": eid, "pair": "BTC/USD", "downsample_to": 50,
        }, caller="cli")
        self.assertTrue(out["success"])
        self.assertLessEqual(len(out["data"]["values"]), 50)
        self.assertEqual(out["data"]["length"], 400)

    def test_get_equity_curve_missing_pair(self):
        eid = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "equity pair missing",
            "n_candles": 80,
        }, caller="cli")["data"]["id"]
        out = self.dispatcher.execute("get_equity_curve", {
            "experiment_id": eid, "pair": "DOGE/USDC",
        }, caller="cli")
        self.assertFalse(out["success"])
        self.assertIn("available", out)


# ═══════════════════════════════════════════════════════════════
# Quota integration
# ═══════════════════════════════════════════════════════════════

class TestDispatcherQuota(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_run_backtest_counts_against_quota(self):
        self.dispatcher.quota = QuotaTracker(per_caller_daily=2,
                                             per_caller_concurrent=5,
                                             global_daily=10)
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "quota q1", "n_candles": 50,
        }, caller="brain:analyst")
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "quota q2", "n_candles": 50,
        }, caller="brain:analyst")
        blocked = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "quota exceeded", "n_candles": 50,
        }, caller="brain:analyst")
        self.assertFalse(blocked["success"])
        self.assertIn("quota", blocked["error"].lower())

    def test_sweep_param_cost_equals_values_count(self):
        self.dispatcher.quota = QuotaTracker(per_caller_daily=3,
                                             per_caller_concurrent=5,
                                             global_daily=10)
        # 3 values → cost 3, fits within per_caller_daily=3
        out = self.dispatcher.execute("sweep_param", {
            "preset": "default", "param": "momentum_rsi_upper",
            "values": [70.0, 75.0, 80.0],
            "hypothesis": "narrow sweep test rsi upper",
            "n_candles": 60,
        }, caller="brain:analyst")
        self.assertTrue(out["success"], out)
        self.assertEqual(len(out["data"]), 3)

    def test_sweep_param_quota_exceeded(self):
        self.dispatcher.quota = QuotaTracker(per_caller_daily=2,
                                             per_caller_concurrent=5,
                                             global_daily=10)
        # 3 values with 2 daily → denied
        out = self.dispatcher.execute("sweep_param", {
            "preset": "default", "param": "momentum_rsi_upper",
            "values": [70.0, 75.0, 80.0],
            "hypothesis": "should be denied over quota",
            "n_candles": 60,
        }, caller="brain:analyst")
        self.assertFalse(out["success"])

    def test_read_only_tools_dont_consume_quota(self):
        self.dispatcher.quota = QuotaTracker(per_caller_daily=1)
        for _ in range(5):
            out = self.dispatcher.execute("list_presets", {}, caller="brain:analyst")
            self.assertTrue(out["success"])
        snap = self.dispatcher.quota.snapshot()
        analyst = snap["per_caller"].get("brain:analyst", {})
        self.assertEqual(analyst.get("daily_count", 0), 0)


# ═══════════════════════════════════════════════════════════════
# Error isolation + audit
# ═══════════════════════════════════════════════════════════════

class TestDispatcherErrorIsolation(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_execute_never_raises_on_handler_exception(self):
        # Inject a failing handler to verify the outer try/except
        def boom(tool_input, caller):
            raise RuntimeError("intentional")
        self.dispatcher._tool_run_backtest = boom  # type: ignore
        out = self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "will blow up",
        }, caller="brain:analyst")
        self.assertFalse(out["success"])
        self.assertIn("RuntimeError", out["error"])

    def test_concurrent_slot_released_on_handler_exception(self):
        def boom(tool_input, caller):
            raise RuntimeError("fail")
        self.dispatcher._tool_run_backtest = boom  # type: ignore
        q = self.dispatcher.quota
        before = q.snapshot()["per_caller"].get("brain:analyst", {}).get("concurrent", 0)
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "will fail isolation",
        }, caller="brain:analyst")
        after = q.snapshot()["per_caller"]["brain:analyst"]["concurrent"]
        self.assertEqual(after, before)   # released

    def test_audit_log_records_tool_calls(self):
        self.dispatcher.execute("list_presets", {}, caller="brain:analyst")
        events = self.dispatcher.store.read_audit()
        kinds = [(e.get("event"), e.get("tool")) for e in events]
        self.assertIn(("tool_call", "list_presets"), kinds)

    def test_audit_records_denial_reason(self):
        self.dispatcher.quota = QuotaTracker(per_caller_daily=0)
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "will be denied",
        }, caller="brain:analyst")
        events = self.dispatcher.store.read_audit()
        denied = [e for e in events
                  if e.get("tool") == "run_backtest" and not e.get("allowed")]
        self.assertGreaterEqual(len(denied), 1)

    def test_audit_input_excludes_large_overrides(self):
        # Overrides dict is collapsed to override_keys to keep the log small
        self.dispatcher.execute("run_backtest", {
            "preset": "default", "hypothesis": "big overrides redacted",
            "n_candles": 50,
            "overrides": {"SOL/USD": {"foo": 1.0, "bar": 2.0}},
        }, caller="brain:analyst")
        events = self.dispatcher.store.read_audit()
        run_events = [e for e in events if e.get("tool") == "run_backtest"]
        self.assertTrue(run_events)
        # overrides redacted; override_keys present
        self.assertNotIn("overrides", run_events[-1]["input"])
        # override_keys is the top-level pair keys (per-pair override dict)
        self.assertEqual(run_events[-1]["input"]["override_keys"], ["SOL/USD"])


# ═══════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════

class TestUtilities(unittest.TestCase):
    def test_downsample_shorter_than_target(self):
        self.assertEqual(_downsample([1.0, 2.0], 10), [1.0, 2.0])

    def test_downsample_preserves_length(self):
        values = [float(i) for i in range(1000)]
        out = _downsample(values, 100)
        self.assertEqual(len(out), 100)

    def test_downsample_zero_target(self):
        # target<=0 returns the full list
        self.assertEqual(_downsample([1.0, 2.0, 3.0], 0), [1.0, 2.0, 3.0])

    def test_finite_or_inf_returns_fallback(self):
        import math
        self.assertEqual(_finite_or(math.inf, 9.0), 9.0)
        self.assertEqual(_finite_or(math.nan, 0.0), 0.0)
        self.assertEqual(_finite_or(3.14159, 0.0), 3.1416)


if __name__ == "__main__":
    unittest.main()
