"""S3 signal surface: adapter block shape, kill switch, R10
non-interference, error inertness, registry-based pair mapping."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hydra_s3 import S3Adapter  # noqa: E402
from hydra_quant_rules import _count_stale_fields  # noqa: E402

DAY = 86400
H = 3600


def candle(ts, o=100.0, h=101.0, low=99.0, c=100.0, v=1.0):
    return {"timestamp": float(ts), "open": o, "high": h, "low": low,
            "close": c, "volume": v}


def feed_days(adapter, pair, n_days, start_day=20000):
    """Feed n_days of hourly candles (+1 confirming candle)."""
    ts = start_day * DAY
    for d in range(n_days):
        for hh in range(24):
            adapter.on_candle(pair, candle(ts + d * DAY + hh * H))
    adapter.on_candle(pair, candle(ts + n_days * DAY))   # confirms the last


class TestS3Adapter(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HYDRA_S3_DISABLED", None)

    def tearDown(self):
        os.environ.pop("HYDRA_S3_DISABLED", None)

    def test_pair_mapping_via_registry_aliases(self):
        a = S3Adapter(["XBT/ZUSD", "ETH/USD", "SOL/USD"], interval_min=60)
        self.assertEqual(a.asset_by_pair["XBT/ZUSD"], "BTC/USD")
        self.assertEqual(a.asset_by_pair["ETH/USD"], "ETH/USD")
        self.assertNotIn("SOL/USD", a.asset_by_pair)     # outside universe

    def test_block_shape_and_data_clock(self):
        a = S3Adapter(["BTC/USD"], interval_min=60)
        self.assertEqual(a.indicator_block("BTC/USD"),
                         {"active": False, "reason": "no_data"})
        feed_days(a, "BTC/USD", 3)
        blk = a.indicator_block("BTC/USD")
        self.assertTrue(blk["active"])
        self.assertTrue(blk["model_loaded"])
        self.assertTrue(blk["degraded"])                  # warmup < MIN_BARS
        self.assertFalse(blk["gated"])
        self.assertEqual(blk["n_daily_bars"], 3)
        self.assertEqual(a.indicator_block("SOL/USD"), {})

    def test_fold_only_on_confirmation(self):
        a = S3Adapter(["BTC/USD"], interval_min=60)
        ts = 20000 * DAY
        a.on_candle("BTC/USD", candle(ts, v=1.0))
        a.on_candle("BTC/USD", candle(ts, v=5.0))         # in-place update
        self.assertEqual(a.data_now("BTC/USD"), 0.0)      # nothing folded
        a.on_candle("BTC/USD", candle(ts + H))            # confirms first
        self.assertEqual(a.data_now("BTC/USD"), ts + H)   # folded close time
        bars = a.strategy.series["BTC/USD"]._days
        self.assertEqual(bars[20000].volume, 5.0)          # update won, once

    def test_kill_switch_removes_surface(self):
        a = S3Adapter(["BTC/USD"], interval_min=60)
        feed_days(a, "BTC/USD", 3)
        os.environ["HYDRA_S3_DISABLED"] = "1"
        self.assertEqual(a.indicator_block("BTC/USD"), {})
        a.on_candle("BTC/USD", candle(20010 * DAY))       # ignored, no error
        os.environ.pop("HYDRA_S3_DISABLED")
        self.assertTrue(a.indicator_block("BTC/USD")["active"])

    def test_error_inertness(self):
        a = S3Adapter(["BTC/USD"], interval_min=60)
        feed_days(a, "BTC/USD", 3)
        a.strategy.evaluate = None                        # poison
        blk = a.indicator_block("BTC/USD")
        self.assertEqual(blk, {"active": False, "reason": "error"})

    def test_r10_stale_count_unaffected(self):
        qi = {"funding_bps_8h": 1.0, "oi_delta_1h_pct": 0.5,
              "oi_price_regime": "neutral", "basis_apr_pct": 2.0,
              "cvd_divergence_sigma": 0.1}
        base = _count_stale_fields(qi)
        qi["s3"] = {"active": True, "stage": "none", "score": None,
                    "gated": False, "degraded": False}
        self.assertEqual(_count_stale_fields(qi), base)
        qi2 = {"derivatives_covered": False, "cvd_divergence_sigma": None,
               "s3": {"active": False}}
        self.assertEqual(_count_stale_fields(qi2), 1)


if __name__ == "__main__":
    unittest.main()
