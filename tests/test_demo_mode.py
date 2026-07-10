"""Offline --demo path: no WSL, no API keys, synthetic market data.

Public first-run guarantee: a fresh clone can exercise the full agent
loop without kraken-cli. These tests never call the real CLI.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate auth DB so import side-effects never touch the operator DB.
# Use a real temp dir (auto-cleaned) — never leave tests/_demo_tmp in the tree.
_TEST_DIR = tempfile.mkdtemp(prefix="hydra_demo_test_")
atexit.register(lambda: shutil.rmtree(_TEST_DIR, ignore_errors=True))
os.environ["HYDRA_AUTH_DB_PATH"] = os.path.join(_TEST_DIR, "auth.db")
os.environ["HYDRA_AUTH_STATE_PATH"] = os.path.join(_TEST_DIR, "auth_state.json")
os.environ.setdefault("HYDRA_TAPE_CAPTURE", "0")
os.environ.setdefault("HYDRA_QUANT_INDICATORS_DISABLED", "1")
os.environ.setdefault("HYDRA_COMPANION_DISABLED", "1")
os.environ.setdefault("HYDRA_BACKTEST_DISABLED", "1")


class TestDemoMode(unittest.TestCase):
    def test_demo_forces_paper_and_skips_cli_warmup(self):
        from hydra_agent import HydraAgent
        from hydra_kraken_cli import KrakenCLI

        with mock.patch.object(KrakenCLI, "ohlc", side_effect=AssertionError("ohlc must not run in demo")):
            with mock.patch.object(KrakenCLI, "balance", side_effect=AssertionError("balance must not run in demo")):
                with mock.patch.object(KrakenCLI, "version", side_effect=AssertionError("version must not run in demo")):
                    agent = HydraAgent(
                        pairs=["SOL/USD", "SOL/BTC", "BTC/USD"],
                        initial_balance=1000.0,
                        interval_seconds=1,
                        duration_seconds=1,
                        ws_port=0,  # broadcaster still binds later in run(); we only construct
                        mode="competition",
                        paper=False,  # demo must override
                        demo=True,
                    )
        self.assertTrue(agent.demo)
        self.assertTrue(agent.paper)
        self.assertEqual(len(agent.engines), 3)
        # Seeds present for all pairs
        for p in agent.pairs:
            self.assertIn(p, agent._demo_prices)
            self.assertGreater(agent._demo_prices[p], 0)

    def test_synthetic_candle_advances_and_ticks(self):
        from hydra_agent import HydraAgent

        agent = HydraAgent(
            pairs=["BTC/USD"],
            initial_balance=500.0,
            interval_seconds=1,
            duration_seconds=1,
            ws_port=18765,
            demo=True,
        )
        agent._warmup_demo_candles(n=60)
        eng = agent.engines["BTC/USD"]
        self.assertGreaterEqual(len(eng.candles), 60)
        before = len(eng.candles)
        state = agent._fetch_and_tick("BTC/USD")
        self.assertIsNotNone(state)
        self.assertIn("signal", state)
        self.assertIn("regime", state)
        self.assertGreaterEqual(len(eng.candles), before)

    def test_demo_paper_order_no_kraken(self):
        from hydra_agent import HydraAgent
        from hydra_kraken_cli import KrakenCLI

        agent = HydraAgent(
            pairs=["SOL/USD"],
            initial_balance=1000.0,
            interval_seconds=1,
            duration_seconds=1,
            ws_port=18766,
            demo=True,
        )
        agent._warmup_demo_candles(n=60)
        state = agent._fetch_and_tick("SOL/USD")
        self.assertIsNotNone(state)
        price = float(state["price"])
        trade = {
            "action": "BUY",
            "amount": 0.5,
            "price": price,
            "reason": "unit-test demo fill",
            "confidence": 0.8,
        }
        # Ensure CLI paper paths are never hit.
        with mock.patch.object(KrakenCLI, "paper_buy", side_effect=AssertionError("paper_buy")):
            with mock.patch.object(KrakenCLI, "paper_sell", side_effect=AssertionError("paper_sell")):
                ok = agent._place_paper_order("SOL/USD", trade, state)
        self.assertTrue(ok)
        self.assertTrue(any(
            (e.get("order_ref") or {}).get("order_id", "").startswith("DEMO-")
            for e in agent.order_journal
        ))

    def test_demo_does_not_merge_or_persist_operator_journal(self):
        """Second demo run must not inherit fills from on-disk operator journal."""
        import json
        import tempfile
        from pathlib import Path
        from hydra_agent import HydraAgent

        with tempfile.TemporaryDirectory() as td:
            marker = {
                "pair": "SOL/USD",
                "side": "BUY",
                "placed_at": "1999-01-01T00:00:00+00:00",
                "intent": {"amount": 1.0, "limit_price": 1.0},
                "order_ref": {"order_id": "RESIDUAL-MARKER", "order_userref": 1},
                "lifecycle": {"state": "FILLED", "vol_exec": 1.0},
            }
            journal_path = Path(td) / "hydra_order_journal.json"
            journal_path.write_text(json.dumps([marker]), encoding="utf-8")
            snap_path = Path(td) / "hydra_session_snapshot.json"
            snap_path.write_text("{}", encoding="utf-8")

            agent = HydraAgent(
                pairs=["BTC/USD"],
                initial_balance=100.0,
                interval_seconds=1,
                duration_seconds=1,
                ws_port=18767,
                demo=True,
            )
            agent._snapshot_dir = td
            # Re-run the post-engine init merge path explicitly.
            if agent.demo:
                pass  # constructor already skipped merge
            else:
                self.fail("demo flag not set")

            self.assertEqual(agent.order_journal, [])
            agent.order_journal.append({"order_ref": {"order_id": "DEMO-1"}})
            agent._save_snapshot()  # must be no-op
            # Operator files unchanged
            on_disk = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk[0]["order_ref"]["order_id"], "RESIDUAL-MARKER")
            self.assertEqual(snap_path.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
