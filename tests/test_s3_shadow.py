"""S3 shadow phase: proposal-once semantics across restarts, confirmer
fixtures, kill switches, and the structural no-order-path guard."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hydra_s3  # noqa: E402
from hydra_s3 import S3Adapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
H = 3600


def daily_row(day, o, h, low, c, v=100.0):
    # package-layer keys ("ts"): these rows go straight to strategy.seed
    return {"ts": float(day * DAY), "open": o, "high": h,
            "low": low, "close": c, "volume": v}


def entryable_adapter(tmpdir, start_day=20000):
    """Adapter whose BTC tape ends at an entryable_b1 bar of a real-shaped
    setup: reuse the synthetic down-leg from the package's setup tests,
    shifted into daily rows, then confirm bars via the 1h feed."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_s3b_setup_fixture", ROOT / "s3bounce" / "tests" / "test_setups.py")
    fixture_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_mod)
    down_leg_bars = fixture_mod.down_leg_bars
    a = S3Adapter(["BTC/USD", "ETH/USD", "ZEC/USD"], interval_min=60,
                  ledger_dir=str(tmpdir))
    bars = down_leg_bars()
    # 90+ warmup days of quiet tape before the down-leg
    rows = [daily_row(start_day + i, 100, 101.2, 99.8, 100 + 0.2 * (i % 3))
            for i in range(100)]
    for j, b in enumerate(bars):
        rows.append(daily_row(start_day + 100 + j, b.open, b.high, b.low,
                              b.close))
    for asset in ("BTC/USD", "ETH/USD", "ZEC/USD"):
        a.strategy.seed(asset, rows)
        a._fold_clock[asset] = rows[-1]["ts"] + DAY
    return a


class TestShadowPhase(unittest.TestCase):
    def setUp(self):
        os.environ["HYDRA_S3_STRATEGY"] = "1"
        os.environ.pop("HYDRA_S3_DISABLED", None)

    def tearDown(self):
        os.environ.pop("HYDRA_S3_STRATEGY", None)
        os.environ.pop("HYDRA_S3_DISABLED", None)

    def _entry_cut(self, a):
        """Advance the fold clock day by day until the signal is
        entryable_b1 + gated; returns True when found."""
        asset = "BTC/USD"
        base = a._fold_clock[asset]
        for back in range(12, -1, -1):
            now = base - back * DAY
            for m in a.strategy.universe:
                a._fold_clock[m] = now
            sig = a.strategy.evaluate(asset, now)
            if sig.stage == "entryable_b1":
                a._last_signal[asset] = sig
                return sig.gated
        return None

    @staticmethod
    def _force_gate_open(a):
        """The synthetic tape scores ~0.30 < the frozen 0.5677 threshold;
        drop the gate so the proposal path itself is exercised (the
        frozen-threshold behavior is covered by s3bounce/tests)."""
        import dataclasses
        m = a.strategy.artifact.models["BTC/USD"]
        a.strategy.artifact.models["BTC/USD"] = \
            dataclasses.replace(m, threshold=0.0)

    def test_ungated_signal_logs_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = entryable_adapter(d)
            gated = self._entry_cut(a)
            self.assertFalse(gated)            # frozen threshold rejects it
            self.assertIsNone(a.shadow_step("BTC/USD", 100.0))
            self.assertEqual(a.ledger().open, [])

    def test_proposal_logged_once_across_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = entryable_adapter(d)
            self._force_gate_open(a)
            self.assertTrue(self._entry_cut(a))
            ev = a.shadow_step("BTC/USD", 100.0)
            self.assertIsNotNone(ev)
            self.assertGreater(len(a.ledger().open), 0)   # arm positions open
            self.assertIsNone(a.shadow_step("BTC/USD", 100.0))  # dedupe
            a2 = entryable_adapter(d)
            self._force_gate_open(a2)
            self.assertTrue(self._entry_cut(a2))
            self.assertIsNone(a2.shadow_step("BTC/USD", 100.0))  # restart dedupe

    def test_flags_gate_the_phase(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = entryable_adapter(d)
            self._entry_cut(a)
            os.environ["HYDRA_S3_STRATEGY"] = "0"
            self.assertIsNone(a.shadow_step("BTC/USD", 100.0))
            os.environ["HYDRA_S3_STRATEGY"] = "1"
            os.environ["HYDRA_S3_DISABLED"] = "1"
            self.assertIsNone(a.shadow_step("BTC/USD", 100.0))

    def test_confirmer_fixture_variants(self):
        import tempfile
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            os.environ["HYDRA_S3_HEARTBEAT_STATUS_DIR"] = d
            try:
                # module-level constant snapshot: reload to pick up env
                import importlib
                importlib.reload(hydra_s3)
                a = hydra_s3.S3Adapter(["BTC/USD"], interval_min=60,
                                       ledger_dir=str(Path(d) / "led"))
                p = Path(d) / "heartbeat_status_BTC_USD.json"
                self.assertEqual(a._read_confirmer("BTC/USD", _t.time()),
                                 {"status": "no_opinion", "why": "missing"})
                p.write_text(json.dumps({"ts": _t.time(), "p_up": 0.7,
                                         "tainted": True}))
                self.assertEqual(a._read_confirmer("BTC/USD", _t.time())["why"],
                                 "tainted")
                p.write_text(json.dumps({"ts": _t.time() - 900, "p_up": 0.7}))
                self.assertEqual(a._read_confirmer("BTC/USD", _t.time())["why"],
                                 "stale")
                p.write_text(json.dumps({"ts": _t.time(), "p_up": 0.7}))
                ok = a._read_confirmer("BTC/USD", _t.time())
                self.assertEqual(ok["status"], "ok")
                self.assertAlmostEqual(ok["p_up"], 0.7)
            finally:
                os.environ.pop("HYDRA_S3_HEARTBEAT_STATUS_DIR", None)
                import importlib
                importlib.reload(hydra_s3)

    def test_no_order_path_in_adapter(self):
        """Structural guard: research surfaces never place orders
        (product thesis — S3 + heartbeat are signal/shadow/display only)."""
        forbidden = re.compile(
            r"_place_order|add_order|execute_signal|execute_s3_entry"
            r"|ExecutionStream|KrakenCLI\.(buy|sell|order)")
        files = (
            [ROOT / "hydra_s3.py", ROOT / "hydra_heartbeat_surface.py"]
            + sorted((ROOT / "s3bounce" / "s3bounce").glob("*.py"))
        )
        for f in files:
            if not f.is_file():
                continue
            hits = forbidden.findall(f.read_text(encoding="utf-8"))
            self.assertEqual(hits, [], f"{f.name}: {hits}")


if __name__ == "__main__":
    unittest.main()
