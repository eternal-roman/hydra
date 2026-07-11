"""HYDRA Walk-Forward Methodology — anchored quarterly folds + paired
Wilcoxon signed-rank test. Stdlib only.

Kernel for the Research Lab hypothesis path: baseline params vs candidate
params across OOS folds. (The old release-gate / snapshot path was removed
as inert self-comparison.)
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence


@dataclass(frozen=True)
class WilcoxonVerdict:
    n: int
    w_plus: float
    w_minus: float
    p_value: float
    candidate_wins: int
    median_delta: float
    verdict: str   # "better" | "worse" | "equivocal"


def wilcoxon_signed_rank(deltas: Sequence[float],
                         alpha: float = 0.05) -> WilcoxonVerdict:
    """Two-sided Wilcoxon signed-rank test on paired-difference samples.

    For n <= 25, uses the exact distribution (enumerate all 2^n sign
    permutations of the ranks). For larger n, uses the normal approximation
    with continuity correction.
    """
    nonzero = [d for d in deltas if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return WilcoxonVerdict(0, 0.0, 0.0, 1.0, 0, 0.0, "equivocal")
    abs_vals = [abs(d) for d in nonzero]
    # Average ranks for ties.
    indexed = sorted(range(n), key=lambda i: abs_vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_vals[indexed[j + 1]] == abs_vals[indexed[i]]:
            j += 1
        avg = (i + j + 2) / 2.0   # ranks 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    w = min(w_plus, w_minus)
    candidate_wins = sum(1 for d in nonzero if d > 0)
    sorted_nonzero = sorted(nonzero)
    median_delta = sorted_nonzero[n // 2] if n % 2 == 1 else (
        (sorted_nonzero[n // 2 - 1] + sorted_nonzero[n // 2]) / 2.0
    )
    if n <= 25:
        p_value = _exact_p(ranks, w)
    else:
        # Normal approx with continuity correction.
        mean = n * (n + 1) / 4.0
        var = n * (n + 1) * (2 * n + 1) / 24.0
        z = (w - mean + 0.5) / math.sqrt(var)
        # Two-sided.
        p_value = 2.0 * _norm_cdf(-abs(z))
    if p_value < alpha:
        verdict = "better" if median_delta > 0 else "worse"
    else:
        verdict = "equivocal"
    return WilcoxonVerdict(n, w_plus, w_minus, p_value, candidate_wins,
                           median_delta, verdict)


def _exact_p(ranks: Sequence[float], w_observed: float) -> float:
    """Exact two-sided p-value: enumerate all 2^n sign assignments of the
    ranks, compute W- (sum of ranks assigned negative sign), and count how
    many are <= w_observed (or symmetrically >=)."""
    n = len(ranks)
    total = 1 << n
    le = 0
    for mask in range(total):
        s = 0.0
        for i in range(n):
            if mask & (1 << i):
                s += ranks[i]
        if s <= w_observed:
            le += 1
    p_one = le / total
    return min(1.0, 2.0 * p_one)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Walk-forward fold construction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkForwardSpec:
    fold_kind: str = "quarterly"
    is_lookback_quarters: int = 8
    min_oos_trades: int = 5


@dataclass(frozen=True)
class Fold:
    idx: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int


def _add_months(d: _dt.datetime, months: int) -> _dt.datetime:
    m = d.month - 1 + months
    y = d.year + m // 12
    return d.replace(year=y, month=(m % 12) + 1)


def _quarter_starts_between(start_ts: int, end_ts: int) -> List[int]:
    """Return a list of UTC unix-second timestamps at each quarter start
    (Jan/Apr/Jul/Oct, day 1, 00:00 UTC) within [start_ts, end_ts]."""
    starts: List[int] = []
    d = _dt.datetime.fromtimestamp(start_ts, tz=_dt.timezone.utc)
    # Round up to next quarter start.
    next_q_month = ((d.month - 1) // 3) * 3 + 1
    cursor = _dt.datetime(d.year, next_q_month, 1, tzinfo=_dt.timezone.utc)
    if cursor.timestamp() < start_ts:
        cursor = _add_months(cursor, 3)
    end_d = _dt.datetime.fromtimestamp(end_ts, tz=_dt.timezone.utc)
    while cursor <= end_d:
        starts.append(int(cursor.timestamp()))
        cursor = _add_months(cursor, 3)
    return starts


def build_quarterly_folds(history_start_ts: int, history_end_ts: int,
                          spec: WalkForwardSpec) -> List[Fold]:
    boundaries = _quarter_starts_between(history_start_ts, history_end_ts)
    if len(boundaries) < 2:
        return []
    folds: List[Fold] = []
    for i in range(1, len(boundaries) - 1):
        oos_start = boundaries[i]
        oos_end = boundaries[i + 1]
        is_end = oos_start
        is_start_idx = max(0, i - spec.is_lookback_quarters)
        is_start = boundaries[is_start_idx]
        if is_start == is_end:
            continue
        folds.append(Fold(
            idx=len(folds),
            is_start=is_start, is_end=is_end,
            oos_start=oos_start, oos_end=oos_end,
        ))
    return folds


# ---------------------------------------------------------------------------
# Runner + result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FoldMetrics:
    sharpe: float
    total_return_pct: float
    max_dd_pct: float
    fee_adj_return_pct: float
    n_trades: int


@dataclass
class FoldResult:
    fold: Fold
    baseline: FoldMetrics
    candidate: FoldMetrics
    deltas: Dict[str, float]


@dataclass
class WalkForwardResult:
    pair: str
    folds: List[FoldResult]
    wilcoxon: Dict[str, WilcoxonVerdict]
    skipped_folds: int


_HEADLINE_METRICS = ("sharpe", "total_return_pct", "max_dd_pct", "fee_adj_return_pct")


RunnerFn = Callable[[str, Dict, Fold], FoldMetrics]


def run_walk_forward(
    pair: str,
    history_start_ts: int,
    history_end_ts: int,
    baseline_params: Dict,
    candidate_params: Dict,
    spec: WalkForwardSpec,
    runner: RunnerFn,
) -> WalkForwardResult:
    folds = build_quarterly_folds(history_start_ts, history_end_ts, spec)
    fold_results: List[FoldResult] = []
    skipped = 0
    for fold in folds:
        baseline = runner(pair, baseline_params, fold)
        candidate = runner(pair, candidate_params, fold)
        if (baseline.n_trades < spec.min_oos_trades or
                candidate.n_trades < spec.min_oos_trades):
            skipped += 1
            continue
        # max_dd_pct: lower is better → flip sign so positive = candidate-better.
        deltas = {
            "sharpe": candidate.sharpe - baseline.sharpe,
            "total_return_pct": candidate.total_return_pct - baseline.total_return_pct,
            "max_dd_pct": baseline.max_dd_pct - candidate.max_dd_pct,
            "fee_adj_return_pct": candidate.fee_adj_return_pct - baseline.fee_adj_return_pct,
        }
        fold_results.append(FoldResult(fold, baseline, candidate, deltas))
    wilcoxon = {}
    for m in _HEADLINE_METRICS:
        wilcoxon[m] = wilcoxon_signed_rank([fr.deltas[m] for fr in fold_results])
    return WalkForwardResult(pair, fold_results, wilcoxon, skipped)
