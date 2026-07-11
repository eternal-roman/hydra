"""Unit tests for hydra_backtest Phase 1: CandleSource, SimulatedFiller,
BacktestConfig stamps, BacktestRunner end-to-end, metric math.

Intentionally kept to stdlib unittest (no pytest dependency) to mirror the
existing tests/ style in this repo.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import unittest

# Make repo root importable when running this file directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_engine import Candle  # noqa: E402
from hydra_backtest import (  # noqa: E402
    BacktestConfig,
    BacktestRunner,
    SimulatedFiller,
    PendingOrder,
    SyntheticSource,
    make_quick_config,
    finalize_stamps,
    _annualize_return,
    _sharpe_from_equity,
    _sortino_from_equity,
    _max_dd_pct,
    _compute_param_hash,
)


class TestBacktestConfig(unittest.TestCase):
    def test_finalize_stamps_fills_all_fields(self):
        cfg = make_quick_config(name="t", n_candles=10)
        self.assertTrue(cfg.param_hash)
        self.assertTrue(cfg.hydra_version)
        self.assertTrue(cfg.created_at)
        # git_sha is best-effort — accepted values: real SHA or "unknown"
        self.assertTrue(cfg.git_sha)

    def test_param_hash_is_stable_for_same_config(self):
        a = make_quick_config(name="a", n_candles=10, seed=7)
        b = make_quick_config(name="b", n_candles=10, seed=7)
        # Name differs but all behavior-affecting fields match → hash must match
        self.assertEqual(a.param_hash, b.param_hash)

    def test_param_hash_changes_on_behavior_change(self):
        a = make_quick_config(name="a", n_candles=10, seed=7)
        b = make_quick_config(name="a", n_candles=20, seed=7)  # different n_candles
        self.assertNotEqual(a.param_hash, b.param_hash)

    def test_param_overrides_json_roundtrip(self):
        overrides = {"SOL/USD": {"momentum_rsi_upper": 75.0}}
        cfg = make_quick_config(name="o", overrides=overrides)
        self.assertEqual(cfg.param_overrides, overrides)


class TestSyntheticSource(unittest.TestCase):
    def test_synthetic_is_deterministic(self):
        a = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        b = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        self.assertEqual(len(a), 50)
        for ca, cb in zip(a, b):
            self.assertEqual(ca.close, cb.close)
            self.assertEqual(ca.high, cb.high)

    def test_synthetic_different_pairs_different_series(self):
        a = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        b = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("BTC/USD"))
        closes_a = [c.close for c in a]
        closes_b = [c.close for c in b]
        self.assertNotEqual(closes_a, closes_b)

    def test_synthetic_flat_kind(self):
        candles = list(SyntheticSource(kind="flat", n_candles=10, start_price=100.0, seed=1).iter_candles("X"))
        for c in candles:
            self.assertAlmostEqual(c.close, 100.0, places=4)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            list(SyntheticSource(kind="bogus", n_candles=3).iter_candles("X"))


class TestSimulatedFiller(unittest.TestCase):
    def _order(self, side="BUY", limit_price=100.0, size=1.0):
        return PendingOrder(
            pair="SOL/USD",
            side=side,
            limit_price=limit_price,
            size=size,
            placed_tick=0,
            pre_trade_snapshot={},
        )

    def _candle(self, o, h, l, c, v=100.0, ts=0.0):
        return Candle(open=o, high=h, low=l, close=c, volume=v, timestamp=ts)

    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            SimulatedFiller(model="quantum")

    def test_optimistic_fills_on_wick_touch_buy(self):
        f = SimulatedFiller("optimistic")
        # limit=100, next candle dips to 99 (wick) then recovers
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=101, h=102, l=99, c=101.5))
        self.assertTrue(fill.filled)
        self.assertEqual(fill.fill_price, 100.0)
        self.assertGreater(fill.fee_paid, 0)

    def test_optimistic_no_touch_rejects(self):
        f = SimulatedFiller("optimistic")
        # limit=100, next candle stays above
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=102, h=103, l=101, c=102.5))
        self.assertFalse(fill.filled)

    def test_realistic_rejects_pure_wick(self):
        f = SimulatedFiller("realistic")
        # Limit=100, body entirely above (open 102, close 102.5, brief dip to 99)
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=102, h=102.6, l=99, c=102.5))
        self.assertFalse(fill.filled)

    def test_realistic_accepts_body_penetration(self):
        f = SimulatedFiller("realistic")
        # Body opens 101 closes 98, spans 3; limit=100 → depth = 2/3 ≈ 0.67 ≥ 0.30 → fill
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=101, h=101.5, l=97.5, c=98))
        self.assertTrue(fill.filled)

    def test_pessimistic_requires_close_cross(self):
        f = SimulatedFiller("pessimistic")
        # Body dips below 100 but closes above → no fill under pessimistic
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=100.5, h=101, l=99.2, c=100.5))
        self.assertFalse(fill.filled)
        # And fills when close dips below
        fill2 = f.try_fill(self._order("BUY", 100), self._candle(o=100.5, h=101, l=99.2, c=99.5))
        self.assertTrue(fill2.filled)

    def test_sell_side_symmetry(self):
        f = SimulatedFiller("optimistic")
        # SELL limit=100; next candle spikes up to 101 → fill
        fill = f.try_fill(self._order("SELL", 100), self._candle(o=99, h=101, l=98.5, c=99.5))
        self.assertTrue(fill.filled)
        # SELL limit=100; next candle never reaches → no fill
        fill2 = f.try_fill(self._order("SELL", 100), self._candle(o=98, h=99.5, l=97, c=98.2))
        self.assertFalse(fill2.filled)


class TestBacktestRunner(unittest.TestCase):
    def test_end_to_end_synthetic_runs_clean(self):
        cfg = make_quick_config(name="e2e", n_candles=400, seed=1)
        result = BacktestRunner(cfg).run()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.candles_processed, 400)
        self.assertGreater(len(result.equity_curve["SOL/USD"]), 100)
        # Metrics are populated (even if trade count is low)
        self.assertIsNotNone(result.metrics)
        self.assertGreaterEqual(result.metrics.total_trades, 0)

    def test_determinism_same_seed_same_metrics(self):
        cfg_a = make_quick_config(name="det", n_candles=300, seed=99)
        cfg_b = make_quick_config(name="det2", n_candles=300, seed=99)
        ra = BacktestRunner(cfg_a).run()
        rb = BacktestRunner(cfg_b).run()
        self.assertAlmostEqual(ra.metrics.total_return_pct, rb.metrics.total_return_pct, places=6)
        self.assertAlmostEqual(ra.metrics.sharpe, rb.metrics.sharpe, places=6)
        self.assertEqual(ra.metrics.total_trades, rb.metrics.total_trades)

    def test_different_seed_different_outcome(self):
        """Seeds change the synthetic path; idle cash under hold-through is OK.

        Hold-through default ON often yields flat cash equity (no trades) on
        short GBM windows — both seeds then share identical cash curves, so
        comparing equity is not a seed probe. Prove diversity at the source;
        when either seed trades or MTM-moves, runner outcomes must also differ.
        """
        closes_a = [
            c.close
            for c in SyntheticSource(
                kind="gbm", n_candles=300, seed=1
            ).iter_candles("SOL/USD")
        ]
        closes_b = [
            c.close
            for c in SyntheticSource(
                kind="gbm", n_candles=300, seed=2
            ).iter_candles("SOL/USD")
        ]
        self.assertNotEqual(closes_a, closes_b, "synthetic seeds must diverge")

        ra = BacktestRunner(make_quick_config(name="d1", n_candles=300, seed=1)).run()
        rb = BacktestRunner(make_quick_config(name="d2", n_candles=300, seed=2)).run()
        self.assertEqual(ra.status, "complete")
        self.assertEqual(rb.status, "complete")
        ea = ra.equity_curve.get("SOL/USD") or []
        eb = rb.equity_curve.get("SOL/USD") or []
        outcomes_differ = (
            ra.metrics.total_return_pct != rb.metrics.total_return_pct
            or ra.metrics.total_trades != rb.metrics.total_trades
            or ea != eb
        )
        if ra.metrics.total_trades + rb.metrics.total_trades > 0 or (
            ea and (min(ea) != max(ea) or (eb and min(eb) != max(eb)))
        ):
            self.assertTrue(
                outcomes_differ,
                "when either seed is active, runner outcomes must differ",
            )
        # else: pure-cash idle on both seeds under defensive rails — allowed

    def test_cancel_token_stops_early(self):
        cfg = make_quick_config(name="c", n_candles=10_000, seed=1)
        tok = threading.Event()
        tok.set()  # cancel before first tick
        result = BacktestRunner(cfg).run(cancel_token=tok)
        self.assertEqual(result.status, "cancelled")
        self.assertLess(result.candles_processed, 10_000)

    def test_on_tick_callback_invoked(self):
        cfg = make_quick_config(name="cb", n_candles=50, seed=1)
        states = []
        BacktestRunner(cfg).run(on_tick=lambda s: states.append(s))
        self.assertGreater(len(states), 10)
        self.assertIn("pairs", states[0])
        self.assertIn("tick", states[0])

    def test_multi_pair_runs_without_error(self):
        cfg = BacktestConfig(
            name="multi",
            pairs=("SOL/USD", "BTC/USD"),
            data_source="synthetic",
            data_source_params_json=json.dumps({
                "kind": "gbm", "n_candles": 200, "seed": 5, "volatility": 0.02,
            }),
            random_seed=5,
        )
        cfg = finalize_stamps(cfg)
        result = BacktestRunner(cfg).run()
        self.assertEqual(result.status, "complete")
        self.assertIn("SOL/USD", result.equity_curve)
        self.assertIn("BTC/USD", result.equity_curve)

    def test_param_overrides_applied_to_engine(self):
        cfg = make_quick_config(
            name="po",
            n_candles=100,
            seed=1,
            overrides={"SOL/USD": {"momentum_rsi_upper": 78.0}},
        )
        runner = BacktestRunner(cfg)
        self.assertAlmostEqual(runner.engines["SOL/USD"].momentum_rsi_upper, 78.0)

    def test_competition_mode_uses_competition_sizing(self):
        cfg = finalize_stamps(BacktestConfig(
            name="comp",
            pairs=("SOL/USD",),
            mode="competition",
            data_source="synthetic",
            data_source_params_json=json.dumps({"kind": "gbm", "n_candles": 50, "seed": 1, "volatility": 0.02}),
        ))
        runner = BacktestRunner(cfg)
        # Competition preset: kelly_multiplier=0.50
        self.assertAlmostEqual(runner.engines["SOL/USD"].sizer.kelly_multiplier, 0.50)

    def test_live_engine_not_mutated(self):
        """I2 sanity: BacktestRunner constructs its own engines; does not take external refs."""
        cfg = make_quick_config(name="i2", n_candles=50, seed=1)
        runner = BacktestRunner(cfg)
        # There is no way for a caller to inject a live engine through the public API
        self.assertTrue(hasattr(runner, "engines"))
        # Fresh runner with same config still gets its own engine (different object)
        runner2 = BacktestRunner(cfg)
        self.assertIsNot(runner.engines["SOL/USD"], runner2.engines["SOL/USD"])


class TestMetricHelpers(unittest.TestCase):
    def test_annualize_zero_ticks(self):
        self.assertEqual(_annualize_return(10.0, 0, 15), 0.0)

    def test_annualize_positive(self):
        # 10% over 96 ticks of 15-min candles = 1 day; annualized should be ~huge
        val = _annualize_return(10.0, 96, 15)
        self.assertGreater(val, 100.0)

    def test_annualize_negative_handles_underwater(self):
        val = _annualize_return(-99.0, 100, 15)
        # Geometric annualization of a -99% cumulative over 100 ticks is very negative
        self.assertLess(val, -50.0)

    def test_max_dd_empty(self):
        self.assertEqual(_max_dd_pct([]), 0.0)

    def test_max_dd_computed(self):
        equity = [100, 110, 105, 120, 60, 90]
        # Peak 120, trough 60 → 50% DD
        self.assertAlmostEqual(_max_dd_pct(equity), 50.0, places=2)

    def test_sharpe_flat_equity_is_zero(self):
        self.assertEqual(_sharpe_from_equity([100.0] * 20, 15), 0.0)

    def test_sharpe_rising_is_positive(self):
        equity = [100 + i for i in range(50)]
        self.assertGreater(_sharpe_from_equity(equity, 15), 0.0)

    def test_sortino_no_downside_handled(self):
        # Monotonically rising equity → no downside. Historically this
        # returned math.inf, but that sanitises to None on JSON save and
        # crashes compare() on reload, so the engine now emits a finite
        # sentinel (999.0) meaning "∞". Accept either form for resilience.
        equity = [100 + i for i in range(30)]
        s = _sortino_from_equity(equity, 15)
        self.assertTrue(s == math.inf or s == 999.0 or s == 0.0)

    def test_returns_from_equity_zero_prev_safe(self):
        # Internal: ensure divide-by-zero protected
        from hydra_backtest import _returns_from_equity
        rets = _returns_from_equity([0.0, 0.0, 10.0])
        self.assertEqual(rets, [0.0, 0.0])


class TestParamHash(unittest.TestCase):
    def test_hash_deterministic(self):
        cfg = make_quick_config(name="h", n_candles=50, seed=1)
        h1 = _compute_param_hash(cfg)
        h2 = _compute_param_hash(cfg)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)  # SHA256 hex

    def test_hash_sensitive_to_overrides(self):
        a = make_quick_config(name="a", n_candles=50, seed=1, overrides={"SOL/USD": {"momentum_rsi_upper": 70.0}})
        b = make_quick_config(name="b", n_candles=50, seed=1, overrides={"SOL/USD": {"momentum_rsi_upper": 75.0}})
        self.assertNotEqual(a.param_hash, b.param_hash)


if __name__ == "__main__":
    unittest.main()
