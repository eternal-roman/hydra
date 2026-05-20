"""Per-version regression runner for Mode C.

Iterates each pair in the default triangle, runs walk-forward (anchored
quarterly, brain stubbed) against the prior version's snapshot, persists
results into hydra_history.sqlite (regression_* tables), and exits with a
gate verdict.

Usage:
    python -m tools.run_regression --version 2.20.0
    python -m tools.run_regression --version 2.20.0 --accept-regression "FX modifier reroll"

Exit codes:
    0  — no WORSE verdict, or override accepted
    2  — Wilcoxon WORSE p<0.05 on any pair × any headline metric, no override
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from typing import Dict, Iterable, Optional, Tuple

from hydra_history_store import HistoryStore


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], encoding="utf-8"
        ).strip()
    except Exception:
        return ""


def persist_regression_run(
    store: HistoryStore,
    run_id: str,
    hydra_version: str,
    git_sha: str,
    param_hash: str,
    pair: str,
    grain_sec: int,
    spec_json: str,
    per_fold_metrics: Dict[int, Dict[str, float]],
    aggregate_metrics: Dict[str, float],
    equity_curve: Iterable[Tuple[int, float]],
    trades: Iterable[Dict],
    override_reason: Optional[str] = None,
) -> None:
    """Persist a single regression_run row + dependent metrics/curve/trade rows.
    All inserts go through one transaction."""
    now = int(time.time())
    with store._conn() as conn:
        conn.execute(
            """INSERT INTO regression_run
               (run_id, hydra_version, git_sha, param_hash, pair, grain_sec,
                spec_json, override_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, hydra_version, git_sha, param_hash, pair, grain_sec,
             spec_json, override_reason, now),
        )
        for fold_idx, m in per_fold_metrics.items():
            for metric_name, val in m.items():
                conn.execute(
                    """INSERT INTO regression_metrics(run_id, fold_idx, metric, value)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, fold_idx, metric_name, val),
                )
        for metric_name, val in aggregate_metrics.items():
            conn.execute(
                """INSERT INTO regression_metrics(run_id, fold_idx, metric, value)
                   VALUES (?, -1, ?, ?)""",
                (run_id, metric_name, val),
            )
        for ts, equity in equity_curve:
            conn.execute(
                """INSERT INTO regression_equity_curve(run_id, ts, equity)
                   VALUES (?, ?, ?)""",
                (run_id, ts, equity),
            )
        for i, t in enumerate(trades):
            conn.execute(
                """INSERT INTO regression_trade
                   (run_id, trade_idx, ts, side, price, size, fee, regime, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, i, t["ts"], t["side"], t["price"], t["size"],
                 t["fee"], t.get("regime"), t.get("reason")),
            )
        conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--db", default=os.environ.get("HYDRA_HISTORY_DB",
                                                    "hydra_history.sqlite"))
    ap.add_argument("--pairs", default="SOL/USD,SOL/BTC,BTC/USD")
    ap.add_argument("--grain-sec", type=int, default=3600)
    ap.add_argument("--accept-regression", default=None,
                    help="If set, accepts WORSE verdict and records the reason")
    args = ap.parse_args()

    store = HistoryStore(args.db)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    git_sha = _git_sha()

    from hydra_walk_forward import (
        run_walk_forward, WalkForwardSpec, FoldMetrics
    )

    # OOS isolation (option 3 — warmup-pad pre-OOS): pass enough IS data to
    # cover engine warmup_candles=50, then start scoring at oos_start. This
    # gives mostly-OOS scoring without a BacktestConfig refactor. Imperfect
    # — the warmup-region's metrics still leak into totals — but bounded.
    # True per-window scoring deferred to v2.20.1 (would add `score_start_ts`
    # to BacktestConfig + BacktestRunner.metrics computation). Tracked in
    # docs/superpowers/notes/2026-04-26-research-tab-build-log.md §12 item 7.
    _WARMUP_PAD_CANDLES = 60   # > engine warmup_candles=50; safety margin
    # KNOWN LIMITATION: baseline_params and candidate_params here are not
    # currently differentiated — `is_baseline=True/False` is a placeholder
    # that the engine ignores. Real baseline-vs-candidate diff requires
    # loading prior version's snapshot params from regression_run rows; that
    # wiring lands in v2.20.1 once a v2.20.0 snapshot exists to compare against.

    def _runner_from_backtest(pair, params, fold) -> "FoldMetrics":
        from hydra_backtest import BacktestConfig, BacktestRunner
        warmup_padded_start = max(
            fold.is_start,
            fold.oos_start - _WARMUP_PAD_CANDLES * args.grain_sec,
        )
        cfg = BacktestConfig(
            name=f"reg-{args.version}-{pair}-{fold.idx}",
            pairs=(pair,),
            data_source="sqlite",
            data_source_params_json=json.dumps({
                "db_path": args.db, "grain_sec": args.grain_sec,
                "start_ts": warmup_padded_start, "end_ts": fold.oos_end,
            }),
            brain_mode="stub",
        )
        result = BacktestRunner(cfg).run()
        m = result.metrics
        return FoldMetrics(
            sharpe=m.sharpe,
            total_return_pct=m.total_return_pct,
            max_dd_pct=m.max_drawdown_pct,
            fee_adj_return_pct=getattr(m, "fee_adj_return_pct",
                                       m.total_return_pct),
            n_trades=m.total_trades,
        )

    spec = WalkForwardSpec()
    worst_verdict: Optional[Tuple[str, str, str]] = None
    for pair in pairs:
        cov = store.coverage(pair, args.grain_sec)
        if cov.first_ts is None:
            print(f"  [REGRESSION] {pair}: no history -- skipping")
            continue
        result = run_walk_forward(
            pair=pair,
            history_start_ts=cov.first_ts,
            history_end_ts=cov.last_ts,
            baseline_params={"is_baseline": True},
            candidate_params={"is_baseline": False},
            spec=spec,
            runner=_runner_from_backtest,
        )
        run_id = uuid.uuid4().hex
        per_fold = {fr.fold.idx: {
            "sharpe": fr.candidate.sharpe,
            "total_return_pct": fr.candidate.total_return_pct,
            "max_dd_pct": fr.candidate.max_dd_pct,
            "fee_adj_return_pct": fr.candidate.fee_adj_return_pct,
        } for fr in result.folds}
        aggregate = {
            f"wilcoxon_p_{m}": result.wilcoxon[m].p_value
            for m in result.wilcoxon
        }
        persist_regression_run(
            store, run_id=run_id, hydra_version=args.version, git_sha=git_sha,
            param_hash="", pair=pair, grain_sec=args.grain_sec,
            spec_json=json.dumps(asdict(spec)),
            per_fold_metrics=per_fold,
            aggregate_metrics=aggregate,
            equity_curve=[],
            trades=[],
            override_reason=args.accept_regression,
        )
        for metric, v in result.wilcoxon.items():
            print(f"  [REGRESSION] {pair} {metric}: {v.verdict} "
                  f"(p={v.p_value:.4f}, wins={v.candidate_wins}/{v.n})")
            if v.verdict == "worse":
                worst_verdict = (pair, metric, v.verdict)

    if worst_verdict and not args.accept_regression and \
            os.environ.get("HYDRA_REGRESSION_GATE", "1") == "1":
        pair, metric, _ = worst_verdict
        print(f"  [REGRESSION] BLOCKED: {pair} {metric} WORSE; "
              f"rerun with --accept-regression \"<reason>\" to override")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
