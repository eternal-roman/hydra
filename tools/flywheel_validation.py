"""Flywheel Phase 0 — validate the current signal core on REAL market data.

Runs the live HydraEngine (verbatim, via BacktestRunner) over the canonical
hydra_history.sqlite store and compares every run against buy-and-hold on the
same window. This is the evidence gate: no directional strategy graduates to
the flywheel's live sleeve without beating its benchmark here, after fees,
on real candles.

Usage:
    python tools/flywheel_validation.py            # full suite (minutes)
    python tools/flywheel_validation.py --quick    # 1y windows only

Output: human table on stdout + .hydra-flywheel/validation_results.json
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_backtest import BacktestConfig, BacktestRunner  # noqa: E402

DB = str(ROOT / "hydra_history.sqlite")
OUT_DIR = ROOT / ".hydra-flywheel"
GRAIN = 3600  # 60m candles
SECONDS_PER_YEAR = 365.25 * 24 * 3600


def _window(pair: str, start_ts: int, end_ts: int):
    """First/last close inside [start_ts, end_ts] for buy-and-hold."""
    db = sqlite3.connect(DB)
    try:
        row = db.execute(
            "select min(ts), max(ts) from ohlc where pair=? and grain_sec=? "
            "and ts between ? and ?", (pair, GRAIN, start_ts, end_ts)).fetchone()
        lo, hi = row
        if lo is None:
            raise SystemExit(f"no candles for {pair} in window")
        first = db.execute("select close from ohlc where pair=? and grain_sec=? and ts=?",
                           (pair, GRAIN, lo)).fetchone()[0]
        last = db.execute("select close from ohlc where pair=? and grain_sec=? and ts=?",
                          (pair, GRAIN, hi)).fetchone()[0]
        return lo, hi, first, last
    finally:
        db.close()


def buy_and_hold(pair: str, start_ts: int, end_ts: int):
    lo, hi, first, last = _window(pair, start_ts, end_ts)
    years = max((hi - lo) / SECONDS_PER_YEAR, 1e-9)
    total = (last / first - 1.0) * 100.0
    annual = ((last / first) ** (1.0 / years) - 1.0) * 100.0
    return {"pair": pair, "total_pct": total, "annualized_pct": annual,
            "years": years, "first_close": first, "last_close": last}


def run_case(name, pairs, mode, start_ts, end_ts, coordinator):
    cfg = BacktestConfig(
        name=name,
        description="flywheel real-data validation",
        pairs=tuple(pairs),
        initial_balance_per_pair=10_000.0,
        candle_interval=60,
        mode=mode,
        coordinator_enabled=coordinator,
        data_source="sqlite",
        data_source_params_json=json.dumps({
            "db_path": DB, "grain_sec": GRAIN,
            "start_ts": start_ts, "end_ts": end_ts,
        }),
        fill_model="realistic",
        maker_fee_bps=16.0,
    )
    t0 = time.time()
    result = BacktestRunner(cfg).run()
    elapsed = time.time() - t0
    m = result.metrics
    bh = {p: buy_and_hold(p, start_ts, end_ts) for p in pairs}
    final_equity = sum(curve[-1] for curve in result.equity_curve.values() if curve)
    initial = 10_000.0 * len(pairs)
    summary = {
        "name": name, "pairs": list(pairs), "mode": mode,
        "status": result.status,
        "errors": result.errors[:2],
        "candles": result.candles_processed,
        "wall_clock_s": round(elapsed, 1),
        "strategy": {
            "total_return_pct": m.total_return_pct,
            "annualized_return_pct": m.annualized_return_pct,
            "sharpe": m.sharpe,
            "max_drawdown_pct": m.max_drawdown_pct,
            "trades": m.total_trades,
            "win_rate_pct": m.win_rate_pct,
            "fills": m.fills, "rejects": m.rejects,
            "final_equity": final_equity, "initial_equity": initial,
        },
        "buy_and_hold": bh,
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    TS_2021 = 1623942000   # 2021-06-17 (SOL listing in store)
    TS_2025_JUN = 1749024000  # 2025-06-04 (funding-history overlap start)
    TS_END = 1777248000    # 2026-04-27 (store end)
    TS_2018 = 1514764800   # 2018-01-01

    cases = [
        ("SOLUSD_1y_competition", ["SOL/USD"], "competition", TS_2025_JUN, TS_END, False),
        ("BTCUSD_1y_competition", ["BTC/USD"], "competition", TS_2025_JUN, TS_END, False),
    ]
    if not args.quick:
        cases += [
            ("SOLUSD_full_conservative", ["SOL/USD"], "conservative", TS_2021, TS_END, False),
            ("SOLUSD_full_competition", ["SOL/USD"], "competition", TS_2021, TS_END, False),
            ("BTCUSD_2018on_conservative", ["BTC/USD"], "conservative", TS_2018, TS_END, False),
            ("TRIANGLE_full_competition", ["SOL/USD", "SOL/BTC", "BTC/USD"],
             "competition", TS_2021, TS_END, True),
        ]

    OUT_DIR.mkdir(exist_ok=True)
    results = []
    for case in cases:
        name = case[0]
        print(f"[{time.strftime('%H:%M:%S')}] running {name} ...", flush=True)
        s = run_case(*case)
        results.append(s)
        st, bh = s["strategy"], s["buy_and_hold"]
        bh_line = " | ".join(
            f"{p} B&H {v['total_pct']:+.1f}% ({v['annualized_pct']:+.1f}%/yr)"
            for p, v in bh.items())
        print(f"  status={s['status']} candles={s['candles']} "
              f"wall={s['wall_clock_s']}s")
        print(f"  STRAT  total {st['total_return_pct']:+.1f}%  "
              f"annual {st['annualized_return_pct']:+.1f}%/yr  "
              f"sharpe {st['sharpe']:.2f}  maxDD {st['max_drawdown_pct']:.1f}%  "
              f"trades {st['trades']}  win {st['win_rate_pct']:.0f}%  "
              f"fills/rejects {st['fills']}/{st['rejects']}")
        print(f"  BENCH  {bh_line}", flush=True)

    out = OUT_DIR / "validation_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
