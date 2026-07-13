"""Unit tests for hydra_backtest_metrics (Phase 2): bootstrap CI, Monte
Carlo block bootstrap, and walk-forward.

Stdlib-only (unittest) to match Phase 1 + project convention.
"""
from __future__ import annotations

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest import make_quick_config  # noqa: E402
from hydra_backtest_metrics import (  # noqa: E402
    ListCandleSource,
    MonteCarloReport,
    WalkForwardReport,
    _block_bootstrap_sample,
    _max_dd_from_equity,
    _percentile,
    _profit_factor,
    _returns_from_profits,
    _sharpe_from_returns,
    annualization_factor,
    bootstrap_ci,
    monte_carlo_resample,
    walk_forward,
)


# ═══════════════════════════════════════════════════════════════
# Annualization / helper math
# ═══════════════════════════════════════════════════════════════

class TestAnnualizationFactor(unittest.TestCase):
    def test_15min_matches_engine_formula(self):
        # Live engine formula: sqrt(365*24*60 / interval_min)
        expected = math.sqrt((365 * 24 * 60) / 15)
        self.assertAlmostEqual(annualization_factor(15), expected, places=9)

    def test_1min_larger_than_60min(self):
        self.assertGreater(annualization_factor(1), annualization_factor(60))

    def test_zero_raises(self):
        with self.assertRaises(ValueError):
            annualization_factor(0)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            annualization_factor(-5)


class TestPercentile(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(_percentile([], 0.5), 0.0)

    def test_single_value(self):
        self.assertEqual(_percentile([7.0], 0.5), 7.0)

    def test_median_of_1to5(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(_percentile(vals, 0.5), 3.0, places=9)

    def test_interpolated(self):
        vals = [0.0, 10.0]
        # 0.3 of range 0–10 = 3.0
        self.assertAlmostEqual(_percentile(vals, 0.3), 3.0, places=9)


# ═══════════════════════════════════════════════════════════════
# Bootstrap CI
# ═══════════════════════════════════════════════════════════════

class TestBootstrapCI(unittest.TestCase):
    def test_empty_returns_zero_tuple(self):
        self.assertEqual(bootstrap_ci([]), (0.0, 0.0))

    def test_single_value_degenerate(self):
        lo, hi = bootstrap_ci([5.0])
        self.assertEqual((lo, hi), (5.0, 5.0))

    def test_deterministic_same_seed(self):
        rng = random.Random(1)
        vals = [rng.gauss(0, 1) for _ in range(100)]
        a = bootstrap_ci(vals, n_iter=500, seed=42)
        b = bootstrap_ci(vals, n_iter=500, seed=42)
        self.assertEqual(a, b)

    def test_ci_contains_true_mean_approx(self):
        # For N(0,1) size 200, 95% CI of bootstrap mean should contain ~0
        rng = random.Random(7)
        vals = [rng.gauss(0.0, 1.0) for _ in range(200)]
        lo, hi = bootstrap_ci(vals, n_iter=800, seed=11)
        self.assertLess(lo, 0.3)
        self.assertGreater(hi, -0.3)

    def test_ci_invalid_bounds_raises(self):
        with self.assertRaises(ValueError):
            bootstrap_ci([1.0, 2.0, 3.0], ci=1.5)
        with self.assertRaises(ValueError):
            bootstrap_ci([1.0, 2.0, 3.0], ci=0.0)

    def test_positive_mean_ci_around_positive(self):
        vals = [1.0 + i * 0.1 for i in range(50)]  # positive values
        lo, hi = bootstrap_ci(vals, n_iter=500, seed=3)
        self.assertGreater(lo, 0.0)
        self.assertGreater(hi, lo)


# ═══════════════════════════════════════════════════════════════
# Block bootstrap + MC resample internals
# ═══════════════════════════════════════════════════════════════

class TestBlockBootstrap(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_block_bootstrap_sample([], 20, random.Random(1)), [])

    def test_length_matches_input(self):
        profits = [float(i) for i in range(100)]
        sample = _block_bootstrap_sample(profits, 20, random.Random(1))
        self.assertEqual(len(sample), len(profits))

    def test_block_len_too_large_falls_back_to_iid(self):
        profits = [1.0, 2.0, 3.0]
        # block_len=10 ≥ n → iid fallback
        sample = _block_bootstrap_sample(profits, 10, random.Random(1))
        self.assertEqual(len(sample), 3)
        for v in sample:
            self.assertIn(v, profits)

    def test_determinism(self):
        profits = [float(i) for i in range(50)]
        a = _block_bootstrap_sample(profits, 10, random.Random(99))
        b = _block_bootstrap_sample(profits, 10, random.Random(99))
        self.assertEqual(a, b)

    def test_no_circular_wrap_within_block(self):
        """Fix 4: a block must not contain the sequence [..., n-1, 0, ...].
        With profits = [0, 1, 2, ..., n-1], that pattern is a strict-decrease
        followed by 0 inside a single block — impossible if blocks are
        contiguous non-wrapping slices, but possible under the old modulo
        implementation."""
        n = 50
        profits = [float(i) for i in range(n)]
        block_len = 10
        # Sample many times across diverse seeds so a regression would show
        for seed in range(64):
            sample = _block_bootstrap_sample(profits, block_len, random.Random(seed))
            # Walk the sample looking for "... n-1 → 0 ..." inside a block.
            # Each block is block_len consecutive entries starting at index
            # 0, block_len, 2*block_len, ... Check every offset inside each
            # block for the n-1 → 0 transition.
            for block_start in range(0, n, block_len):
                block = sample[block_start:block_start + block_len]
                for k in range(len(block) - 1):
                    if block[k] == float(n - 1) and block[k + 1] == 0.0:
                        self.fail(
                            f"wrap detected in seed={seed} block={block!r} "
                            "— _block_bootstrap_sample must NOT wrap circularly"
                        )

    def test_block_contents_are_consecutive(self):
        """Every block of length block_len in the sample must be a contiguous
        slice of the original sequence (start..start+block_len-1).
        With profits = [0, 1, 2, ...] this means each block is an arithmetic
        progression with step=1."""
        n = 100
        profits = [float(i) for i in range(n)]
        block_len = 15
        sample = _block_bootstrap_sample(profits, block_len, random.Random(42))
        for block_start in range(0, len(sample) - block_len + 1, block_len):
            block = sample[block_start:block_start + block_len]
            if len(block) < block_len:
                continue  # truncated tail
            first = block[0]
            for offset, value in enumerate(block):
                self.assertEqual(
                    value, first + offset,
                    f"block at {block_start} not consecutive: {block!r}",
                )


class TestReturnsHelpers(unittest.TestCase):
    def test_returns_from_profits_happy(self):
        equity, returns = _returns_from_profits([10.0, -5.0, 20.0], starting_equity=100.0)
        self.assertEqual(equity[0], 100.0)
        self.assertEqual(equity[-1], 125.0)
        self.assertAlmostEqual(returns[0], 0.1, places=9)
        self.assertAlmostEqual(returns[1], -0.05 / 1.1, places=6)

    def test_returns_negative_equity_safe(self):
        # Trade wipes out balance then recovers — prev≤0 protection
        equity, returns = _returns_from_profits([-200.0, 50.0], starting_equity=100.0)
        self.assertEqual(returns[1], 0.0)  # prev was -100 → divide-by-zero guard → 0

    def test_sharpe_flat_is_zero(self):
        self.assertEqual(_sharpe_from_returns([0.0] * 10, annualization_factor(15)), 0.0)

    def test_sharpe_constant_returns_is_zero(self):
        # Constant returns → stdev=0 → sharpe=0 by contract (matches engine)
        self.assertEqual(_sharpe_from_returns([0.01] * 50, annualization_factor(15)), 0.0)

    def test_sharpe_varying_positive_is_positive(self):
        returns = [0.01 + (i % 3) * 0.001 for i in range(50)]
        self.assertGreater(_sharpe_from_returns(returns, annualization_factor(15)), 0.0)

    def test_max_dd_peak_trough(self):
        equity = [100, 120, 100, 60, 80]
        # peak 120 trough 60 → 50%
        self.assertAlmostEqual(_max_dd_from_equity(equity), 50.0, places=2)

    def test_profit_factor_mixed(self):
        # wins 30, losses 10 → 3.0
        self.assertAlmostEqual(_profit_factor([10.0, -5.0, 20.0, -5.0]), 3.0, places=6)

    def test_profit_factor_all_wins(self):
        self.assertEqual(_profit_factor([1.0, 2.0]), math.inf)

    def test_profit_factor_empty(self):
        self.assertEqual(_profit_factor([]), 0.0)


# ═══════════════════════════════════════════════════════════════
# Monte Carlo resample
# ═══════════════════════════════════════════════════════════════

class TestMonteCarloResample(unittest.TestCase):
    def test_empty_input(self):
        r = monte_carlo_resample([])
        self.assertEqual(r.n_iter, 0)
        self.assertEqual(r.sharpe_ci.mean, 0.0)

    def test_determinism(self):
        profits = [0.5, -0.3, 1.2, -0.7, 0.4, 0.6, -0.2, 1.0] * 5
        a = monte_carlo_resample(profits, n_iter=100, seed=7)
        b = monte_carlo_resample(profits, n_iter=100, seed=7)
        self.assertEqual(a.sharpe_ci.mean, b.sharpe_ci.mean)
        self.assertEqual(a.total_return_ci.lower, b.total_return_ci.lower)

    def test_ci_bounds_ordered(self):
        profits = [0.5, -0.3, 1.2, -0.7, 0.4, 0.6, -0.2, 1.0] * 5
        r = monte_carlo_resample(profits, n_iter=200, seed=1)
        self.assertLessEqual(r.sharpe_ci.lower, r.sharpe_ci.upper)
        self.assertLessEqual(r.total_return_ci.lower, r.total_return_ci.upper)
        self.assertLessEqual(r.max_drawdown_ci.lower, r.max_drawdown_ci.upper)

    def test_all_positive_profits_positive_return_ci_lower(self):
        profits = [0.5] * 40
        r = monte_carlo_resample(profits, n_iter=200, seed=3)
        # All trades profitable → resampled total return is always positive
        self.assertGreater(r.total_return_ci.lower, 0.0)

    def test_report_type(self):
        r = monte_carlo_resample([1.0, 2.0, -1.0] * 10, n_iter=50, seed=1)
        self.assertIsInstance(r, MonteCarloReport)


# ═══════════════════════════════════════════════════════════════
# ListCandleSource + walk_forward
# ═══════════════════════════════════════════════════════════════

class TestListCandleSource(unittest.TestCase):
    def test_describe_has_counts(self):
        from hydra_engine import Candle
        c = [Candle(1, 2, 0.5, 1.5, 10.0, 0.0)]
        src = ListCandleSource({"X": c}, label="unit")
        d = src.describe()
        self.assertEqual(d["kind"], "list")
        self.assertEqual(d["label"], "unit")
        self.assertEqual(d["counts"], {"X": 1})

    def test_iter_yields_in_order(self):
        from hydra_engine import Candle
        a = Candle(1, 2, 0.5, 1.5, 10.0, 0.0)
        b = Candle(1.5, 3, 1.0, 2.0, 11.0, 60.0)
        src = ListCandleSource({"X": [a, b]})
        got = list(src.iter_candles("X"))
        self.assertEqual(got, [a, b])


class TestWalkForward(unittest.TestCase):
    def test_basic_run(self):
        cfg = make_quick_config(name="wf-basic", n_candles=400, seed=11)
        from dataclasses import replace
        cfg = replace(cfg, coordinator_enabled=False)
        report = walk_forward(cfg, train_pct=0.6, test_pct=0.4, n_windows=3)
        self.assertEqual(report.n_windows, 3)
        self.assertEqual(len(report.slices), 3)
        self.assertIsInstance(report, WalkForwardReport)

    def test_invalid_n_windows_raises(self):
        cfg = make_quick_config(name="wf-bad", n_candles=100)
        with self.assertRaises(ValueError):
            walk_forward(cfg, n_windows=0)

    def test_invalid_pcts_raises(self):
        cfg = make_quick_config(name="wf-bad", n_candles=100)
        with self.assertRaises(ValueError):
            walk_forward(cfg, train_pct=0.0, test_pct=0.4)
        with self.assertRaises(ValueError):
            walk_forward(cfg, train_pct=0.6, test_pct=2.0)

    def test_stability_is_nonneg(self):
        cfg = make_quick_config(name="wf-stab", n_candles=300, seed=2)
        from dataclasses import replace
        cfg = replace(cfg, coordinator_enabled=False)
        rep = walk_forward(cfg, n_windows=2, train_pct=0.5, test_pct=0.5)
        self.assertGreaterEqual(rep.sharpe_stability, 0.0)
        self.assertEqual(len(rep.improvement_pct_per_slice), len(rep.slices))

    def test_windows_distinct_at_default_pcts(self):
        """train+test == 1.0 (the default) previously collapsed every window
        to the identical slice — zero variance reported as fake perfect
        stability. Test segments must be distinct and non-overlapping."""
        cfg = make_quick_config(name="wf-distinct", n_candles=400, seed=7)
        from dataclasses import replace
        cfg = replace(cfg, coordinator_enabled=False)
        rep = walk_forward(cfg, train_pct=0.6, test_pct=0.4, n_windows=4)
        spans = [(s.candles_start, s.candles_end) for s in rep.slices]
        starts = [s.candles_start for s in rep.slices]
        self.assertEqual(len(set(spans)), 4, f"identical windows: {spans}")
        # Consecutive test segments must not overlap (step == test size)
        for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s2, e1, f"overlapping tests: {spans}")
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
