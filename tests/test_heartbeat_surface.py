"""Read-only heartbeat status surface — TDD for Phase-1 dashboard wiring.

Invariant: this module never places orders. Grep-guard + behavioral tests.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
import hydra_heartbeat_surface as hbs


class TestStatusPathContract(unittest.TestCase):
    def test_pair_filename_uses_underscore_asset(self):
        self.assertEqual(hbs.pair_status_filename("BTC/USD"),
                         "heartbeat_status_BTC_USD.json")
        self.assertEqual(hbs.pair_status_filename("ETH/USD"),
                         "heartbeat_status_ETH_USD.json")

    def test_resolve_generic_status_file_to_pair_named(self):
        """Generic heartbeat_status.json must resolve to Hydra/S3 pair name."""
        p = hbs.resolve_status_path("data/heartbeat_status.json", "BTC/USD")
        self.assertEqual(p.name, "heartbeat_status_BTC_USD.json")
        self.assertEqual(p.parent, Path("data"))

    def test_resolve_directory_to_pair_named(self):
        p = hbs.resolve_status_path("heartbeat/data", "ETH/USD")
        self.assertEqual(p, Path("heartbeat/data") / "heartbeat_status_ETH_USD.json")

    def test_explicit_pair_path_passes_through(self):
        raw = "heartbeat/data/heartbeat_status_BTC_USD.json"
        self.assertEqual(hbs.resolve_status_path(raw, "BTC/USD"), Path(raw))


class TestReadStatusSemantics(unittest.TestCase):
    def setUp(self):
        self._dir = Path(os.environ.get("TEMP", ".")) / f"hydra_hb_t_{os.getpid()}"
        self._dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for f in self._dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._dir.rmdir()
        except OSError:
            pass

    def _write(self, name, payload):
        path = self._dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_is_no_opinion(self):
        out = hbs.read_status(self._dir / "nope.json", now=1000.0)
        self.assertEqual(out["status"], "no_opinion")
        self.assertEqual(out["why"], "missing")
        self.assertIsNone(out.get("p_up"))

    def test_tainted_is_no_opinion(self):
        path = self._write("heartbeat_status_BTC_USD.json", {
            "pair": "BTC/USD", "p_up": 0.9, "ts": 999.0, "tainted": True,
        })
        out = hbs.read_status(path, now=1000.0)
        self.assertEqual(out["status"], "no_opinion")
        self.assertEqual(out["why"], "tainted")

    def test_stale_is_no_opinion(self):
        path = self._write("heartbeat_status_BTC_USD.json", {
            "pair": "BTC/USD", "p_up": 0.8, "ts": 100.0, "tainted": False,
        })
        out = hbs.read_status(path, now=1000.0, stale_s=300.0)
        self.assertEqual(out["status"], "no_opinion")
        self.assertEqual(out["why"], "stale")

    def test_ok_returns_p_up(self):
        path = self._write("heartbeat_status_BTC_USD.json", {
            "pair": "BTC/USD", "tf": "1h", "p_up": 0.6421, "L": 0.58,
            "ts": 990.0, "tainted": False, "candle_progress": 0.4,
            "features": {"ofi": {"z": 0.2}, "clv": {"z": 0.5}},
        })
        out = hbs.read_status(path, now=1000.0)
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["p_up"], 0.6421)
        self.assertEqual(out["features"]["clv"]["z"], 0.5)

    def test_never_emits_half_as_missing_default(self):
        """Missing/stale/tainted must not fabricate p_up=0.5."""
        for why in ("missing", "tainted", "stale"):
            if why == "missing":
                out = hbs.read_status(self._dir / "x.json", now=1.0)
            elif why == "tainted":
                p = self._write("t.json", {"p_up": 0.5, "ts": 1.0, "tainted": True})
                out = hbs.read_status(p, now=1.0)
            else:
                p = self._write("s.json", {"p_up": 0.5, "ts": 0.0, "tainted": False})
                out = hbs.read_status(p, now=1000.0, stale_s=10)
            self.assertNotEqual(out.get("p_up"), 0.5,
                                f"fabricated 0.5 for {why}: {out}")


class TestHeartbeatSurface(unittest.TestCase):
    def setUp(self):
        self._dir = Path(os.environ.get("TEMP", ".")) / f"hydra_hbs_{os.getpid()}"
        self._dir.mkdir(parents=True, exist_ok=True)
        os.environ.pop("HYDRA_HEARTBEAT_SURFACE", None)
        os.environ.pop("HYDRA_S3_HEARTBEAT_STATUS_DIR", None)

    def tearDown(self):
        os.environ.pop("HYDRA_HEARTBEAT_SURFACE", None)
        os.environ.pop("HYDRA_S3_HEARTBEAT_STATUS_DIR", None)
        for f in self._dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._dir.rmdir()
        except OSError:
            pass

    def test_kill_switch_removes_block(self):
        os.environ["HYDRA_HEARTBEAT_SURFACE"] = "0"
        surf = hbs.HeartbeatSurface(["BTC/USD"], status_dir=str(self._dir))
        self.assertEqual(surf.indicator_block("BTC/USD"), {})

    def test_indicator_block_ok_and_history(self):
        path = self._dir / "heartbeat_status_BTC_USD.json"
        path.write_text(json.dumps({
            "pair": "BTC/USD", "tf": "1h", "p_up": 0.71, "L": 0.9,
            "ts": time.time(), "tainted": False, "candle_progress": 0.5,
        }), encoding="utf-8")
        surf = hbs.HeartbeatSurface(["BTC/USD"], status_dir=str(self._dir))
        blk = surf.indicator_block("BTC/USD")
        self.assertEqual(blk["status"], "ok")
        self.assertAlmostEqual(blk["p_up"], 0.71)
        self.assertTrue(blk["active"])
        # second poll appends history when p_up present
        blk2 = surf.indicator_block("BTC/USD")
        self.assertGreaterEqual(len(blk2.get("history") or []), 1)
        self.assertIn("p_up", blk2["history"][-1])

    def test_sol_zec_still_readable_but_flagged_fail_asset(self):
        """SOL/ZEC flow classifier FAIL — surface marks asset class, still no order."""
        path = self._dir / "heartbeat_status_SOL_USD.json"
        path.write_text(json.dumps({
            "pair": "SOL/USD", "p_up": 0.8, "ts": time.time(), "tainted": False,
        }), encoding="utf-8")
        surf = hbs.HeartbeatSurface(["SOL/USD"], status_dir=str(self._dir))
        blk = surf.indicator_block("SOL/USD")
        self.assertEqual(blk["status"], "ok")
        self.assertTrue(blk.get("flow_gate_fail"))

    def test_r10_does_not_count_nested_heartbeat_as_stale_fields(self):
        from hydra_quant_rules import _count_stale_fields
        qi = {
            "funding_bps_8h": 1.0,
            "funding_predicted_bps": 1.0,
            "oi_delta_1h_pct": 0.1,
            "oi_delta_24h_pct": 0.2,
            "oi_price_regime": "neutral",
            "basis_apr_pct": 5.0,
            "staleness_s": 1.0,
            "cvd_divergence_sigma": 0.0,
            "heartbeat": {"status": "missing", "p_up": None, "active": False},
        }
        # full covered track: all primary fields present → 0 stale
        self.assertEqual(_count_stale_fields(qi), 0)


class TestNoOrderPathGuard(unittest.TestCase):
    def test_module_source_has_no_order_verbs(self):
        src = Path(hbs.__file__).read_text(encoding="utf-8").lower()
        for needle in ("order_buy", "order_sell", "krakencli", "place_order",
                       "subprocess"):
            self.assertNotIn(needle, src)


class TestHeartbeatApiPairPath(unittest.TestCase):
    """heartbeat package: generic status_file resolves to pair-named file."""

    def test_resolve_status_path_in_api(self):
        import sys
        hb_src = Path(__file__).resolve().parents[1] / "heartbeat" / "src"
        if str(hb_src) not in sys.path:
            sys.path.insert(0, str(hb_src))
        from heartbeat.api import resolve_status_path
        p = resolve_status_path("data/heartbeat_status.json", "BTC/USD")
        self.assertEqual(p.name, "heartbeat_status_BTC_USD.json")


if __name__ == "__main__":
    unittest.main()
