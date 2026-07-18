"""Bake-off: HYDRA backtest baseline vs heartbeat-gated BUY entries.

Protocol (data decides; no hand-tuned thresholds):
  1. Replay the stored REAL trade tape through the heartbeat pipeline per
     pair -> P(up) at every 1h candle close, keyed by candle open_ts.
  2. Run the stock HYDRA BacktestRunner (sqlite source, competition mode,
     coordinator on) over the exact tape window            -> BASELINE.
  3. Re-run with each engine's execute_signal wrapped: a BUY is SKIPPED
     when the pair's P(up) at the just-closed candle is below a
     threshold. Thresholds are per-pair percentiles (P20/P35/P50/P65) of
     that pair's own posterior distribution over the TRAIN segment —
     never hand-picked absolutes (HONEST_FINDINGS #4: the absolute level
     is regime-dominated).                                 -> GATED arms.
  4. INVERSE control: veto BUYs when P(up) is ABOVE the same thresholds.
     If gating low-P(up) helps while gating high-P(up) hurts, the signal
     direction is consistent; if both "help", the gate is just trading
     less and the improvement is noise, not information.
  5. Split-sample honesty: weights + thresholds derived from the first
     TRAIN_FRAC of the window are also evaluated on a backtest of the
     held-out tail only (OOS arms). Full-window arms are labeled
     in-sample wherever calibrated weights were fit on that same window.

Causality: at backtest tick t the engine has ingested the candle with
open_ts T (now closed); the heartbeat P(up) keyed to T was computed at
that same candle's close from its trades. Same information time — the
gate never sees the future. Tainted or missing posterior rows fail OPEN
(no veto), mirroring the trend-overlay convention.

SELLs are never touched (exit guarantees, PR-A).

Usage (from heartbeat/):
    PYTHONPATH=src python tools/hydra_bakeoff.py --pairs SOL/USD,BTC/USD \
        [--db ../hydra_history.sqlite] [--train-frac 0.6] [--out ...]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HYDRA_ROOT))

from heartbeat.config import load_config           # noqa: E402
from heartbeat.engine.pipeline import run_tape     # noqa: E402
from heartbeat.store import Store                  # noqa: E402

from hydra_backtest import BacktestConfig, BacktestRunner  # noqa: E402

PCTS = (20, 35, 50, 65)
TF = "1h"
GRAIN = 3600


def load_weights(pair: str, root: Path) -> dict | None:
    p = root / "reports" / f"weights_{pair.replace('/', '_')}_{TF}.json"
    if p.exists():
        return json.loads(p.read_text())["weights"]
    return None


def posterior_for(pair: str, cfg: dict, weights: dict | None) -> list[dict]:
    """Replay stored tape -> per-candle posterior rows (chronological)."""
    cfg = json.loads(json.dumps(cfg))  # deep copy; run is config-pure
    if weights:
        cfg.setdefault("features", {})["weights"] = weights
    store = Store(str(HEARTBEAT_ROOT / cfg["store"]["root"]))
    trades = store.read_tape(pair, TF)
    if not trades:
        raise SystemExit(f"no tape for {pair} {TF} — run backfill first")
    return run_tape(cfg, pair, TF, trades)


def percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.5
    k = min(len(sorted_vals) - 1, max(0, int(round(pct / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def sqlite_window(db: str, pairs: list[str]) -> tuple[int, int]:
    con = sqlite3.connect(db)
    lo, hi = 0, 1 << 62
    for p in pairs:
        row = con.execute(
            "SELECT MIN(ts), MAX(ts) FROM ohlc WHERE pair=? AND grain_sec=?",
            (p, GRAIN)).fetchone()
        if row[0] is None:
            raise SystemExit(f"no sqlite candles for {p}")
        lo, hi = max(lo, row[0]), min(hi, row[1])
    return lo, hi


def make_runner(db: str, pairs: list[str], start_ts: int, end_ts: int,
                name: str) -> BacktestRunner:
    cfg = BacktestConfig(
        name=name,
        pairs=tuple(pairs),
        mode="competition",
        coordinator_enabled=True,
        data_source="sqlite",
        data_source_params_json=json.dumps({
            "db_path": db, "grain_sec": GRAIN,
            "start_ts": start_ts, "end_ts": end_ts}),
        max_ticks=1_000_000,
    )
    return BacktestRunner(cfg)


def apply_gate(runner: BacktestRunner, pup: dict[str, dict[int, dict]],
               thresholds: dict[str, float], inverse: bool) -> dict[str, int]:
    """Wrap each engine's execute_signal with the BUY confirmation gate.
    Returns a mutable veto-counter dict updated during the run."""
    vetoes = {p: 0 for p in runner.engines}
    checked = {p: 0 for p in runner.engines}
    for pair, engine in runner.engines.items():
        orig = engine.execute_signal
        rows = pup.get(pair, {})
        thr = thresholds[pair]

        def gated(action, confidence, *a, _orig=orig, _rows=rows, _thr=thr,
                  _pair=pair, _engine=engine, **kw):
            if action == "BUY" and _engine.candles:
                row = _rows.get(int(_engine.candles[-1].timestamp))
                if row is not None and not row["tainted"]:
                    checked[_pair] += 1
                    p = row["p_up"]
                    veto = (p > _thr) if inverse else (p < _thr)
                    if veto:
                        vetoes[_pair] += 1
                        return None  # SKIP semantics: no order this tick
            return _orig(action, confidence, *a, **kw)

        engine.execute_signal = gated
    vetoes["_checked"] = checked
    return vetoes


_METRIC_KEYS = ("total_return_pct", "sharpe", "sortino", "max_drawdown_pct",
                "profit_factor", "total_trades", "win_rate_pct", "fills",
                "rejects", "avg_holding_ticks")


def _metric_dict(m) -> dict:
    return {k: getattr(m, k, None) for k in _METRIC_KEYS}


def summarize(result, vetoes=None) -> dict:
    out = {
        "status": result.status,
        "fills": result.fills, "rejects": result.rejects,
        "candles": result.candles_processed,
        "aggregate": _metric_dict(result.metrics) if result.metrics else None,
        "per_pair": {p: _metric_dict(m)
                     for p, m in (result.per_pair_metrics or {}).items()},
    }
    if result.errors:
        out["errors"] = [e.get("message") for e in result.errors]
    if vetoes is not None:
        out["buy_vetoes"] = {k: v for k, v in vetoes.items() if k != "_checked"}
        out["buys_checked"] = vetoes.get("_checked")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="SOL/USD,BTC/USD")
    ap.add_argument("--db", default=str(HYDRA_ROOT / "hydra_history.sqlite"))
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--out", default=str(HEARTBEAT_ROOT / "evidence" /
                                         "hydra_bakeoff.json"))
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",")]
    hb_cfg = load_config(None)
    store_root = HEARTBEAT_ROOT / hb_cfg["store"]["root"]

    # 1) posterior per pair (calibrated weights if present — flagged in output)
    pup: dict[str, dict[int, dict]] = {}
    weights_used: dict[str, bool] = {}
    rows_by_pair: dict[str, list[dict]] = {}
    for pair in pairs:
        w = load_weights(pair, store_root)
        weights_used[pair] = w is not None
        rows = posterior_for(pair, hb_cfg, w)
        rows_by_pair[pair] = rows
        pup[pair] = {int(r["candle_open_ts"]): r for r in rows}

    # window = tape overlap ∩ sqlite coverage
    tape_lo = max(min(pup[p]) for p in pairs)
    tape_hi = min(max(pup[p]) for p in pairs)
    db_lo, db_hi = sqlite_window(args.db, pairs)
    lo, hi = max(tape_lo, db_lo), min(tape_hi, db_hi)
    if hi - lo < 30 * 86400:
        print(f"WARNING: window is only {(hi - lo) / 86400:.1f} days")
    split = int(lo + (hi - lo) * args.train_frac)

    # thresholds from TRAIN-segment posterior distribution (per pair)
    thresholds: dict[int, dict[str, float]] = {}
    for pct in PCTS:
        thresholds[pct] = {}
        for pair in pairs:
            train_vals = sorted(r["p_up"] for ts, r in pup[pair].items()
                                if lo <= ts <= split and not r["tainted"])
            thresholds[pct][pair] = percentile(train_vals, pct)

    report: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pairs": pairs, "window": [lo, hi], "split_ts": split,
        "train_frac": args.train_frac,
        "calibrated_weights_used": weights_used,
        "thresholds": {str(k): v for k, v in thresholds.items()},
        "arms": {},
    }

    def run_arm(name: str, start: int, end: int, thr: dict | None,
                inverse: bool = False):
        runner = make_runner(args.db, pairs, start, end, name)
        vetoes = None
        if thr is not None:
            vetoes = apply_gate(runner, pup, thr, inverse)
        res = runner.run()
        report["arms"][name] = summarize(res, vetoes)
        agg = report["arms"][name].get("aggregate") or {}
        print(f"{name:>34}: ret={agg.get('total_return_pct')} "
              f"sharpe={agg.get('sharpe')} trades={agg.get('total_trades')} "
              f"vetoes={report['arms'][name].get('buy_vetoes')}", flush=True)

    # full-window arms (weights in-sample if calibrated on this window)
    run_arm("baseline_full", lo, hi, None)
    for pct in PCTS:
        run_arm(f"gated_p{pct}_full", lo, hi, thresholds[pct])
    for pct in (35, 50):
        run_arm(f"inverse_p{pct}_full", lo, hi, thresholds[pct], inverse=True)

    # OOS arms: backtest only the held-out tail
    run_arm("baseline_oos", split, hi, None)
    for pct in PCTS:
        run_arm(f"gated_p{pct}_oos", split, hi, thresholds[pct])

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
