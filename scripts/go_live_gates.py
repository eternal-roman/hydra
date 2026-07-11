#!/usr/bin/env python3
"""Go-live gates for HYDRA (PR-D / remediation plan §6).

Runs a minimal sqlite backtest matrix and exits 0 only if soft gates pass.
Not a substitute for human judgment — blocks obviously broken deploys.

Usage:
  python scripts/go_live_gates.py
  python scripts/go_live_gates.py --db hydra_history.sqlite
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_backtest import BacktestConfig, BacktestRunner  # noqa: E402


def _coverage(db: str):
    c = sqlite3.connect(db)
    rows = list(c.execute(
        "SELECT grain_sec, pair, MIN(ts), MAX(ts), COUNT(*) FROM ohlc "
        "GROUP BY grain_sec, pair"
    ))
    c.close()
    return rows


def _run(db, pairs, grain, t0, t1, fill, mode):
    cfg = BacktestConfig(
        name=f"gate_{fill}_{mode}",
        pairs=tuple(pairs),
        initial_balance_per_pair=100.0,
        candle_interval=max(1, grain // 60),
        mode=mode,
        coordinator_enabled=len(pairs) > 1,
        data_source="sqlite",
        data_source_params_json=json.dumps({
            "db_path": db, "grain_sec": grain, "start_ts": t0, "end_ts": t1,
        }),
        fill_model=fill,
        maker_fee_bps=16.0,
        max_ticks=200_000,
    )
    return BacktestRunner(cfg).run()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "hydra_history.sqlite"))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    db = args.db
    if not Path(db).exists():
        print(f"FAIL: missing history db {db}")
        return 2

    cov = _coverage(db)
    sol = [r for r in cov if r[0] == 3600 and r[1] == "SOL/USD"]
    if not sol:
        print("FAIL: no SOL/USD 1h coverage")
        return 2
    _, _, tmin, tmax, _ = sol[0]
    t0 = max(tmin, tmax - 90 * 86400)

    print(f"Running go-live gates on {db} SOL/USD 90d 1h ...")
    results = {}
    for fill in ("pessimistic", "realistic"):
        r = _run(db, ["SOL/USD"], 3600, t0, tmax, fill, "competition")
        m = r.metrics
        results[fill] = {
            "status": r.status,
            "return_pct": m.total_return_pct,
            "max_dd": m.max_drawdown_pct,
            "sharpe": m.sharpe,
            "trades": m.total_trades,
            "fill_rate": r.fills / max(1, r.fills + r.rejects),
        }
        print(f"  {fill}: ret={m.total_return_pct:.2f}% dd={m.max_drawdown_pct:.1f}% "
              f"sharpe={m.sharpe:.2f} trades={m.total_trades}")

    # Soft gates — fail closed on broken plumbing, not on alpha.
    failures = []
    for fill, row in results.items():
        if row["status"] != "complete":
            failures.append(f"{fill} status={row['status']}")
        if row["max_dd"] > 99.0:
            failures.append(f"{fill} max_dd absurd {row['max_dd']}")
        # After PR-A, halted engines should flatten — open inventory freeze
        # should not drive 100% session loss as the only outcome forever.
    # Unit invariant re-check via import
    from hydra_quant_rules import QFE_MIN_PROFIT_PCT, apply_rules, FUNDING_EXTREME_BPS
    from hydra_engine import HydraEngine
    if QFE_MIN_PROFIT_PCT < 1.0:
        failures.append(f"QFE floor {QFE_MIN_PROFIT_PCT} < 1.0")
    r2 = apply_rules(
        "SELL",
        {},
        {"funding_bps_8h": -(FUNDING_EXTREME_BPS + 10),
         "oi_delta_1h_pct": 0.0, "basis_apr_pct": 5.0,
         "oi_price_regime": "balanced", "staleness_s": 1.0},
    )
    if r2.force_hold:
        failures.append("R2 still force_holds SELL")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    eng.halted = True
    eng.position.size = 0.5
    eng.position.avg_entry = 100.0
    for i in range(30):
        eng.ingest_candle({
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 10, "timestamp": float(i),
        })
    t = eng.execute_signal("SELL", 0.55, "gate", "DEFENSIVE")
    if t is None:
        failures.append("halted engine refuses SELL")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "results": results, "failures": failures,
        }, indent=2), encoding="utf-8")

    if failures:
        print("FAIL gates:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS go-live gates (plumbing + exit invariants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
