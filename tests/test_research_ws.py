"""Tests for v2.20.0 Research tab WS routes.

Covers:
  - research_dataset_coverage: returns per-(pair, grain_sec) rows from the store
  - research_lab_run: T30B — functional Mode B; synchronous ack returns
    {success, job_id, n_folds, pair}; daemon thread streams progress
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest_server import BacktestWorkerPool, mount_backtest_routes  # noqa: E402
from hydra_experiments import ExperimentStore  # noqa: E402
from hydra_history_store import CandleRow, HistoryStore  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Minimal fake broadcaster — matches the existing test_backtest_server.py pattern
# ═══════════════════════════════════════════════════════════════

class _MockBroadcaster:
    """Captures register_handler calls so tests can invoke handlers directly.
    broadcast_message captures into self.broadcasts for T30B async assertions."""
    def __init__(self):
        self.handlers: Dict[str, Callable] = {}
        self.broadcasts: list = []  # T30B — list of (msg_type, payload) tuples

    def broadcast_message(self, msg_type: str, payload: Dict[str, Any]) -> None:
        self.broadcasts.append((msg_type, payload))

    def register_handler(self, msg_type: str, fn: Callable) -> None:
        self.handlers[msg_type] = fn


# ═══════════════════════════════════════════════════════════════
# Shared fixture
# ═══════════════════════════════════════════════════════════════

class _ResearchFixture(unittest.TestCase):
    """setUp/tearDown: tmp dir, history DB, mock broadcaster, pool."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-research-test-"))
        self.db_path = self.tmp / "h.sqlite"
        # Initialise the store so schema is created before handlers are mounted.
        self.history_store = HistoryStore(str(self.db_path))
        os.environ["HYDRA_HISTORY_DB"] = str(self.db_path)

        self.experiment_store = ExperimentStore(root=self.tmp / "experiments")
        self.bc = _MockBroadcaster()
        self.pool = BacktestWorkerPool(
            max_workers=1,
            store=self.experiment_store,
            broadcaster=self.bc,
            error_log_dir=self.tmp,
        )
        mount_backtest_routes(self.bc, self.pool)

    def tearDown(self):
        try:
            self.pool.shutdown(timeout=3.0)
        finally:
            os.environ.pop("HYDRA_HISTORY_DB", None)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, handler_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.bc.handlers[handler_name](payload)


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

class TestResearchDatasetCoverage(_ResearchFixture):
    def test_returns_pair_rows(self):
        self.history_store.upsert_candles([
            CandleRow("BTC/USD", 3600, 1_700_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
            CandleRow("BTC/USD", 3600, 1_700_003_600, 1, 1, 1, 1, 1, "kraken_archive"),
        ])
        reply = self._call("research_dataset_coverage", {})
        self.assertTrue(reply["success"])
        self.assertEqual(len(reply["data"]), 1)
        row = reply["data"][0]
        self.assertEqual(row["pair"], "BTC/USD")
        self.assertEqual(row["grain_sec"], 3600)
        self.assertEqual(row["candle_count"], 2)

    def test_empty_store_returns_empty_list(self):
        reply = self._call("research_dataset_coverage", {})
        self.assertTrue(reply["success"])
        self.assertEqual(reply["data"], [])

    def test_returns_success_false_on_bad_db_path(self):
        os.environ["HYDRA_HISTORY_DB"] = "/nonexistent/path/that/cannot/be/created/h.sqlite"
        try:
            reply = self._call("research_dataset_coverage", {})
            self.assertFalse(reply["success"])
            self.assertIn("error", reply)
        finally:
            os.environ["HYDRA_HISTORY_DB"] = str(self.db_path)


class TestResearchLabRun(_ResearchFixture):
    def test_invalid_pair_rejected(self):
        """Unknown pair returns success=False before touching the store."""
        reply = self._call("research_lab_run", {"pair": "ETH/USD"})
        self.assertFalse(reply["success"])
        self.assertIn("pair required", reply["error"])

    def test_missing_pair_rejected(self):
        reply = self._call("research_lab_run", {})
        self.assertFalse(reply["success"])
        self.assertIn("pair required", reply["error"])

    def test_no_history_rejected(self):
        """Valid pair but no candles in the store → error before spawning thread."""
        reply = self._call("research_lab_run", {
            "pair": "BTC/USD",
            "baseline_params": {},
            "candidate_params": {},
        })
        self.assertFalse(reply["success"])
        self.assertIn("no history", reply["error"])

    def test_lab_run_returns_job_id_synchronously(self):
        """Mode B run is async via daemon thread; the ack returns job_id and
        n_folds immediately. We don't wait for the run to finish in tests."""
        # Insert a couple of candles so coverage isn't empty.
        self.history_store.upsert_candles([
            CandleRow("BTC/USD", 3600, 1_700_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
            CandleRow("BTC/USD", 3600, 1_800_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
        ])
        reply = self._call("research_lab_run", {
            "pair": "BTC/USD",
            "baseline_params": {"momentum_rsi_upper": 70.0},
            "candidate_params": {"momentum_rsi_upper": 75.0},
        })
        self.assertTrue(reply["success"], reply.get("error"))
        self.assertIn("job_id", reply)
        self.assertEqual(reply["pair"], "BTC/USD")
        self.assertGreaterEqual(reply["n_folds"], 0)

    def test_lab_run_broadcasts_started_message(self):
        """Daemon thread emits at least the 'started' research_lab_progress
        broadcast within a short window after the ack."""
        import time
        self.history_store.upsert_candles([
            CandleRow("BTC/USD", 3600, 1_700_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
            CandleRow("BTC/USD", 3600, 1_800_000_000, 1, 1, 1, 1, 1, "kraken_archive"),
        ])
        reply = self._call("research_lab_run", {
            "pair": "BTC/USD",
            "baseline_params": {},
            "candidate_params": {},
        })
        self.assertTrue(reply["success"], reply.get("error"))
        # Give the daemon thread a moment to emit the started broadcast.
        time.sleep(0.15)
        started = [
            (t, p) for (t, p) in self.bc.broadcasts
            if t == "research_lab_progress" and p.get("phase") == "started"
        ]
        self.assertGreater(len(started), 0, "no 'started' broadcast within 150ms")


class TestAllResearchHandlersRegistered(_ResearchFixture):
    def test_research_handlers_present(self):
        for name in (
            "research_dataset_coverage",
            "research_lab_run",
        ):
            self.assertIn(name, self.bc.handlers, f"missing handler: {name}")


if __name__ == "__main__":
    unittest.main()
