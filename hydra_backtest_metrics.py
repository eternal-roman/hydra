#!/usr/bin/env python3
"""
HYDRA Backtest — Advanced Metrics (Phase 2 of v2.10.0 backtest platform).

Institutional-grade robustness analytics for backtest results. Pure Python
stdlib (no numpy/scipy) so the engine's "zero dependencies" stance extends
to the metrics layer. See docs/BACKTEST_SPEC.md §6.2.

Functions
---------
annualization_factor(candle_interval_min)
    sqrt(365·24·60 / interval) — the factor we scale mean/std of per-bar
    returns by to get annualized Sharpe/Sortino.

bootstrap_ci(values, n_iter, ci, seed)
    Vanilla percentile bootstrap for the mean of `values`.

monte_carlo_resample(trade_profits, n_iter, block_len, seed, candle_interval_min)
    Block bootstrap over realized trade profits. Returns CIs for:
      total_return_pct, sharpe, max_drawdown_pct, profit_factor.

walk_forward(base_config, train_pct, test_pct, n_windows)
    Slides train/test windows across the full candle series; returns a
    WalkForwardReport with per-slice Sharpe + stability metrics.

Design invariants
-----------------
- Deterministic: all functions seeded; same inputs → identical outputs (I12).
- Stdlib only: no numpy/pandas/scipy (inherits engine stance).
- Safe reuse of Phase-1 BacktestRunner via its `sources_override` hook — no
  duplication of `_loop`, preserving I7 (zero drift) across walk-forward slices.
- Live-state safe: consumes only completed BacktestResult / trade_log data;
  never holds refs to live agent state (I2).
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Sequence, Tuple

from hydra_engine import Candle
from hydra_backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    CandleSource,
    make_candle_source,
)


# ═══════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════

@dataclass
class WalkForwardSlice:
    """Per-window record from a walk-forward run.

    `window_index` — 0-based position of this slice across the full series.
    `candles_start/end` — [start, end) indices into the materialized candle list.
    """

    window_index: int
    candles_start: int
    candles_end: int
    total_trades: int
    total_return_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    final_equity: float


@dataclass
class WalkForwardReport:
    n_windows: int
    train_pct: float
    test_pct: float
    slices: List[WalkForwardSlice] = field(default_factory=list)
    mean_sharpe: float = 0.0
    std_sharpe: float = 0.0
    sharpe_stability: float = 0.0  # std / |mean| — lower is more stable
    improved_slices: int = 0       # count where sharpe > 0
    improvement_pct_per_slice: List[float] = field(default_factory=list)


@dataclass
class MonteCarloCI:
    lower: float
    upper: float
    mean: float
    std_error: float


@dataclass
class MonteCarloReport:
    n_iter: int
    block_len: int
    total_return_ci: MonteCarloCI
    sharpe_ci: MonteCarloCI
    max_drawdown_ci: MonteCarloCI
    profit_factor_ci: MonteCarloCI


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def annualization_factor(candle_interval_min: int) -> float:
    """sqrt(365·24·60 / interval_min). Same formula used in live Sharpe/Sortino."""
    if candle_interval_min <= 0:
        raise ValueError("candle_interval_min must be positive")
    return math.sqrt((365.0 * 24.0 * 60.0) / float(candle_interval_min))


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile on a pre-sorted sequence; pct in [0,1]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = pct * (len(sorted_vals) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def bootstrap_ci(
    values: Sequence[float],
    n_iter: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean of `values`. Empty input → (0, 0)."""
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    if not (0 < ci < 1):
        raise ValueError("ci must be in (0, 1)")
    rng = random.Random(seed)
    n = len(values)
    means: List[float] = []
    for _ in range(n_iter):
        sample_sum = 0.0
        for _i in range(n):
            sample_sum += values[rng.randint(0, n - 1)]
        means.append(sample_sum / n)
    means.sort()
    alpha = (1 - ci) / 2
    return (_percentile(means, alpha), _percentile(means, 1 - alpha))


def _returns_from_profits(profits: Sequence[float], starting_equity: float) -> Tuple[List[float], List[float]]:
    """Convert a sequence of per-trade profit dollars into (equity_curve, returns).

    Returns are relative per-trade increments (trade_pnl / prior_equity). Equity
    is cumulative starting from `starting_equity`.
    """
    equity = [starting_equity]
    returns: List[float] = []
    for p in profits:
        prev = equity[-1]
        if prev <= 0:
            returns.append(0.0)
            equity.append(prev + p)
            continue
        returns.append(p / prev)
        equity.append(prev + p)
    return equity, returns


def _sharpe_from_returns(returns: Sequence[float], annual_factor: float) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)  # pop stdev is fine for resamples of fixed length
    if sd <= 0:
        return 0.0
    return (mean / sd) * annual_factor


def _max_dd_from_equity(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > worst:
                worst = dd
    return worst


def _profit_factor(profits: Sequence[float]) -> float:
    gains = sum(p for p in profits if p > 0)
    losses = -sum(p for p in profits if p < 0)
    if losses <= 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def _block_bootstrap_sample(
    profits: Sequence[float],
    block_len: int,
    rng: random.Random,
) -> List[float]:
    """NON-CIRCULAR block resample preserving local temporal structure.

    For each block draw, sample a start index uniformly from the set of
    valid starts (0..n-block_len) so every block fits without wrapping.
    Emit block_len consecutive profits; repeat until length ≥ n; truncate.
    Blocks within a single resample are drawn independently, so two draws
    can share overlapping ranges — "non-circular" refers to wrap-around,
    not cross-draw disjointness.

    Fix 4: previously used `(start + j) % n` circular indexing, which
    joined tail-of-sequence to head-of-sequence inside a block. For small
    trade counts (n ≤ ~50) this was effectively IID and yielded CIs that
    were too narrow — rigor gate `mc_ci_lower_positive` passed marginal
    strategies. Non-circular blocks preserve the intended autocorrelation
    structure of the original sequence.
    """
    n = len(profits)
    if n == 0:
        return []
    if block_len <= 0 or block_len >= n:
        # Degenerate: fall back to iid bootstrap so the call still produces a sample
        return [profits[rng.randint(0, n - 1)] for _ in range(n)]
    max_start = n - block_len  # inclusive upper bound — no wrap needed
    sample: List[float] = []
    while len(sample) < n:
        start = rng.randint(0, max_start)
        sample.extend(profits[start:start + block_len])
    return sample[:n]


# ═══════════════════════════════════════════════════════════════
# Monte Carlo resampling
# ═══════════════════════════════════════════════════════════════

def monte_carlo_resample(
    trade_profits: Sequence[float],
    n_iter: int = 500,
    block_len: int = 20,
    seed: int = 42,
    candle_interval_min: int = 15,
    starting_equity: float = 100.0,
) -> MonteCarloReport:
    """Block bootstrap over realized trade profits.

    For each iteration, resample a same-length trade sequence via block
    bootstrap (block_len preserves short-horizon autocorrelation), then
    recompute total_return / sharpe / max_dd / profit_factor. Return 95% CIs.

    Note: the sharpe here is computed on trade-level returns, not candle-level.
    Reviewer uses this to bound the significance of its observed improvement.
    """
    if not trade_profits:
        empty = MonteCarloCI(0.0, 0.0, 0.0, 0.0)
        return MonteCarloReport(
            n_iter=0, block_len=block_len,
            total_return_ci=empty, sharpe_ci=empty,
            max_drawdown_ci=empty, profit_factor_ci=empty,
        )

    rng = random.Random(seed)
    af = annualization_factor(candle_interval_min)

    total_returns: List[float] = []
    sharpes: List[float] = []
    max_dds: List[float] = []
    pfs: List[float] = []

    for _ in range(n_iter):
        sample = _block_bootstrap_sample(trade_profits, block_len, rng)
        equity, returns = _returns_from_profits(sample, starting_equity)
        total_returns.append((equity[-1] - starting_equity) / starting_equity * 100.0)
        sharpes.append(_sharpe_from_returns(returns, af))
        max_dds.append(_max_dd_from_equity(equity))
        pfs.append(_profit_factor(sample))

    def _ci(values: List[float]) -> MonteCarloCI:
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return MonteCarloCI(0.0, 0.0, 0.0, 0.0)
        finite_sorted = sorted(finite)
        lo = _percentile(finite_sorted, 0.025)
        hi = _percentile(finite_sorted, 0.975)
        mu = statistics.fmean(finite)
        se = statistics.pstdev(finite) if len(finite) > 1 else 0.0
        return MonteCarloCI(lo, hi, mu, se)

    return MonteCarloReport(
        n_iter=n_iter,
        block_len=block_len,
        total_return_ci=_ci(total_returns),
        sharpe_ci=_ci(sharpes),
        max_drawdown_ci=_ci(max_dds),
        profit_factor_ci=_ci(pfs),
    )


# ═══════════════════════════════════════════════════════════════
# Walk-forward
# ═══════════════════════════════════════════════════════════════

class ListCandleSource(CandleSource):
    """In-memory candle source. Yields a pre-materialized list per pair.

    Used by walk_forward to feed sliced candle views into a BacktestRunner
    without duplicating `_loop`. Kept in metrics module (not
    hydra_backtest.py) because it's only meaningful when you already have
    materialized candles in hand.
    """

    def __init__(self, candles_by_pair: Dict[str, List[Candle]], label: str = "list") -> None:
        self._candles = candles_by_pair
        self._label = label

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        for c in self._candles.get(pair, []):
            yield c

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": "list",
            "label": self._label,
            "counts": {p: len(v) for p, v in self._candles.items()},
        }


def _materialize_candles(cfg: BacktestConfig) -> Dict[str, List[Candle]]:
    """Pull the full candle series per pair once, so downstream slicers don't
    re-hit the source (fast for synthetic, rate-limit-friendly for Kraken)."""
    out: Dict[str, List[Candle]] = {}
    for pair in cfg.pairs:
        src = make_candle_source(cfg)
        out[pair] = list(src.iter_candles(pair))
    return out


def _final_equity(result: BacktestResult) -> float:
    total = 0.0
    for _pair, curve in result.equity_curve.items():
        if curve:
            total += curve[-1]
    return total


def _slice_length(full: Dict[str, List[Candle]]) -> int:
    # walk_forward iterates over the SHORTEST pair series to stay aligned;
    # per-pair candles are time-aligned in BacktestRunner.
    return min((len(v) for v in full.values()), default=0)


def walk_forward(
    base_config: BacktestConfig,
    train_pct: float = 0.6,
    test_pct: float = 0.4,
    n_windows: int = 5,
) -> WalkForwardReport:
    """Slide train+test windows across the full candle series.

    Window layout (indices into the materialized candle list):
        window_i: [start_i, start_i + (train+test)*W)
        test segment: last test_pct fraction of the window
    Backtest is run on the TEST segment only; training is nominal (params
    already baked into config — parameter fitting happens upstream).

    Window sizing guarantees DISTINCT, non-overlapping consecutive test
    segments: train_pct/test_pct set the train:test ratio within a window,
    and windows step by exactly one test segment so the n test slices tile
    the series. (The previous derivation collapsed every window to the
    identical slice whenever train_pct + test_pct == 1.0 — the default —
    reporting zero variance as fake "perfect stability".)

    For a 1000-candle series with train 0.6 / test 0.4 (ratio 1.5) and
    n_windows=5: test_size = 1000/(1.5+5) ≈ 153, window ≈ 382, step 153 →
    five distinct 153-candle out-of-sample slices.
    """
    if n_windows < 1:
        raise ValueError("n_windows must be ≥ 1")
    if not (0 < train_pct < 1) or not (0 < test_pct <= 1):
        raise ValueError("train_pct / test_pct must be in (0, 1]")

    full = _materialize_candles(base_config)
    total_len = _slice_length(full)
    if total_len == 0:
        return WalkForwardReport(n_windows=0, train_pct=train_pct, test_pct=test_pct)

    # total = test*(ratio+1) + (n-1)*test  →  test = total / (ratio + n)
    ratio = train_pct / test_pct
    test_size = max(1, int(total_len / (ratio + n_windows)))
    window_size = min(total_len, max(1, int(test_size * (ratio + 1.0))))
    step = test_size

    slices: List[WalkForwardSlice] = []
    for i in range(n_windows):
        start = min(i * step, max(0, total_len - window_size))
        end = start + window_size
        if end > total_len:
            end = total_len
        test_start = end - test_size

        sliced_by_pair = {p: full[p][test_start:end] for p in base_config.pairs}
        sources_override = {
            p: ListCandleSource({p: sliced_by_pair[p]}, label=f"wf_{i}")
            for p in base_config.pairs
        }
        runner = BacktestRunner(base_config, sources_override=sources_override)
        result = runner.run()
        slices.append(WalkForwardSlice(
            window_index=i,
            candles_start=test_start,
            candles_end=end,
            total_trades=result.metrics.total_trades,
            total_return_pct=result.metrics.total_return_pct,
            sharpe=result.metrics.sharpe,
            sortino=result.metrics.sortino,
            max_drawdown_pct=result.metrics.max_drawdown_pct,
            final_equity=_final_equity(result),
        ))

    # Aggregate
    sharpes = [s.sharpe for s in slices if math.isfinite(s.sharpe)]
    improvement_pcts = [s.total_return_pct for s in slices]
    improved = sum(1 for s in slices if s.sharpe > 0)
    mean_sh = statistics.fmean(sharpes) if sharpes else 0.0
    std_sh = statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0
    stability = (std_sh / abs(mean_sh)) if abs(mean_sh) > 1e-9 else (std_sh if std_sh > 0 else 0.0)

    return WalkForwardReport(
        n_windows=n_windows,
        train_pct=train_pct,
        test_pct=test_pct,
        slices=slices,
        mean_sharpe=mean_sh,
        std_sharpe=std_sh,
        sharpe_stability=stability,
        improved_slices=improved,
        improvement_pct_per_slice=improvement_pcts,
    )


# ═══════════════════════════════════════════════════════════════
# CLI smoke (no external deps — synthetic only)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    from hydra_backtest import make_quick_config

    cfg = make_quick_config(name="metrics-smoke", n_candles=300, seed=7)
    print("[metrics smoke] walk_forward…")
    wf = walk_forward(cfg, train_pct=0.6, test_pct=0.4, n_windows=3)
    print(f"  n={wf.n_windows} mean_sharpe={wf.mean_sharpe:.3f} stability={wf.sharpe_stability:.3f}")
    print("[metrics smoke] bootstrap_ci on synthetic returns…")
    vals = [0.01, -0.005, 0.02, -0.01, 0.015, 0.008, -0.003, 0.012, 0.0, 0.005]
    lo, hi = bootstrap_ci(vals, n_iter=500, seed=1)
    print(f"  mean CI 95%: [{lo:.5f}, {hi:.5f}]")
    print("[metrics smoke] done.")
