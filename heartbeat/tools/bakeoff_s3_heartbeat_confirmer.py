"""Bakeoff: S3 X1 book × heartbeat confirmer (pre-registered F2).

Registration MUST be committed before interpreting results:
  evidence/bakeoffs/s3_heartbeat_confirmer_REGISTRATION.md

Uses overlapping real tape (default last 90d) + frozen s3bounce artifact
+ calibrated weights. SELLs/exits = X1 only. NO order path.

Usage (from heartbeat/ or repo root):
  PYTHONPATH=src python tools/bakeoff_s3_heartbeat_confirmer.py
  python heartbeat/tools/bakeoff_s3_heartbeat_confirmer.py --days 90
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))
sys.path.insert(0, str(HYDRA_ROOT))
sys.path.insert(0, str(HYDRA_ROOT / "s3bounce"))

from heartbeat.config import load_config  # noqa: E402
from heartbeat.engine.pipeline import run_tape  # noqa: E402
from heartbeat.store import Store  # noqa: E402
from heartbeat.weights_io import apply_weights_to_config, find_weights  # noqa: E402

OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "s3_heartbeat_confirmer.json"
DB_DEFAULT = HYDRA_ROOT / "hydra_history.sqlite"
GATE_ASSETS = ("BTC/USD", "ETH/USD")
TF = "1h"
THETA = 0.50
FEE_BPS = 26.0


def _p_up_series(pair: str, cfg: dict, ts_start: float, ts_end: float) -> dict[int, float]:
    """Replay tape slice → map candle open_ts (int) → p_up."""
    store = Store(str(HEARTBEAT_ROOT / cfg["store"]["root"]))
    found = find_weights(pair, TF, store_root=cfg["store"]["root"],
                         package_root=HEARTBEAT_ROOT)
    if found:
        apply_weights_to_config(cfg, found[0])
    trades = store.read_tape(pair, TF, ts_start=ts_start, ts_end=ts_end)
    if not trades:
        return {}
    rows = run_tape(cfg, pair, TF, trades)
    out: dict[int, float] = {}
    for r in rows:
        if r.get("tainted"):
            continue
        ts = r.get("ts")
        p = r.get("p_up")
        if ts is None or p is None:
            continue
        # rows are candle-close snapshots; ts is close-ish — key by open
        # if present else floor to hour
        open_ts = int(r.get("open_ts") or (int(ts) // 3600) * 3600)
        out[open_ts] = float(p)
    return out


def _lookup_p_up(series: dict[int, float], entry_ts: float) -> float | None:
    """Nearest hour open at or before entry_ts within 2h."""
    if not series:
        return None
    t = int(entry_ts)
    hour = (t // 3600) * 3600
    for delta in (0, -3600, 3600, -7200):
        k = hour + delta
        if k in series:
            return series[k]
    # closest key
    best = min(series.keys(), key=lambda k: abs(k - t))
    if abs(best - t) <= 7200:
        return series[best]
    return None


def _x1_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "avg_pct": None, "stop_rate": None, "win_rate": None}
    rets = [float(t["ret"]) for t in trades]
    stops = sum(1 for t in trades if str(t.get("reason", "")).startswith("stop"))
    wins = sum(1 for r in rets if r > 0)
    return {
        "n": len(trades),
        "avg_pct": round(100.0 * sum(rets) / len(rets), 4),
        "stop_rate": round(stops / len(trades), 4),
        "win_rate": round(wins / len(trades), 4),
        "sum_pct": round(100.0 * sum(rets), 4),
    }


def _seed_universe(db: Path):
    """Breadth requires full universe seeded or gate never opens."""
    from bounce_geometry_study import candles_from_sqlite
    from s3bounce import S3Strategy, load_artifact
    strat = S3Strategy(load_artifact())
    daily_by = {}
    for asset in strat.universe:
        daily = candles_from_sqlite(str(db), asset, 24)
        if not daily:
            continue
        rows = [{
            "ts": c.open_ts, "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": getattr(c, "volume", 0.0),
        } for c in daily]
        strat.seed(asset, rows)
        daily_by[asset] = daily
    return strat, daily_by


def _simulate_x1_on_entries(pair: str, db: Path, start_ts: float, end_ts: float,
                            strat=None, daily_by=None):
    """Return list of trade dicts with entry_ts and ret using S3 gate + X1."""
    import paper_bounce_sim as sim
    if strat is None or daily_by is None:
        strat, daily_by = _seed_universe(db)
    daily = daily_by.get(pair)
    if not daily or pair not in strat.universe:
        return []

    # Collect gated entries with setup geometry from strategy internals
    entries = []
    for i, c in enumerate(daily):
        ts = float(c.open_ts)
        if ts < start_ts or ts > end_ts:
            continue
        now = float(daily[i + 1].open_ts) if i + 1 < len(daily) else ts + 86400
        try:
            sig = strat.evaluate(pair, now)
        except Exception:
            continue
        if sig.stage != "entryable_b1" or not sig.gated:
            continue
        s = sig.setup
        if s is None:
            continue
        entries.append({
            "entry_ts": float(getattr(daily[sig.entry_idx], "open_ts", ts)),
            "entry_idx": sig.entry_idx,
            "low_idx": s.low_idx,
            "low_px": s.low_px,
            "atr": s.atr,
            "score": sig.score,
        })

    trades = []
    in_pos = -1
    TARGET_ATR = getattr(sim, "TARGET_ATR", 3.3)
    HORIZON = getattr(sim, "HORIZON", 200)
    fee = FEE_BPS / 10000.0
    for e in entries:
        ei = e["entry_idx"]
        if ei <= in_pos or ei >= len(daily):
            continue
        entry_px = daily[ei].close
        L0 = e["low_px"]
        tgt = L0 + TARGET_ATR * e["atr"]
        exit_px = reason = None
        k_exit = ei
        for k in range(ei + 1, len(daily)):
            c = daily[k]
            if c.close < L0:
                exit_px, reason, k_exit = c.close, "stop_close", k
                break
            if c.high >= tgt:
                exit_px, reason, k_exit = tgt, "target", k
                break
            if k - e["low_idx"] > HORIZON:
                exit_px, reason, k_exit = c.close, "time", k
                break
        if exit_px is None:
            continue
        ret = (exit_px / entry_px - 1.0) - 2 * fee
        trades.append({
            "pair": pair,
            "entry_ts": e["entry_ts"],
            "entry_px": entry_px,
            "exit_px": exit_px,
            "ret": ret,
            "reason": reason,
            "score": e["score"],
        })
        in_pos = k_exit
    return trades


def _verdict(stats: dict) -> dict:
    a, b, c, d = stats["A_s3_only"], stats["B_s3_plus_hb"], stats["C_inverse"], stats["D_random50"]
    if a["n"] < 15:
        return {"decision": "INCONCLUSIVE", "why": "C6 power n(A)<15", "n_A": a["n"]}
    # coverage handled outside
    checks = {}
    if a["stop_rate"] is None or b["stop_rate"] is None:
        return {"decision": "INCONCLUSIVE", "why": "empty_arm"}
    checks["C1"] = b["stop_rate"] <= a["stop_rate"] - 0.10
    checks["C2"] = (b["avg_pct"] is not None and a["avg_pct"] is not None
                    and b["avg_pct"] >= a["avg_pct"] - 0.30)
    checks["C3"] = (c["stop_rate"] is None or c["n"] == 0
                    or c["stop_rate"] >= a["stop_rate"])
    checks["C4"] = (
        b["stop_rate"] <= (d["stop_rate"] or 1.0) - 0.05
        or (b["avg_pct"] is not None and d["avg_pct"] is not None
            and b["avg_pct"] >= d["avg_pct"])
    )
    if all(checks.values()):
        return {"decision": "PASS_SHADOW_FILTER", "checks": checks}
    if not checks["C1"] or not checks["C3"]:
        return {"decision": "FAIL", "checks": checks}
    return {"decision": "INCONCLUSIVE", "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--theta", type=float, default=THETA)
    args = ap.parse_args()
    db = Path(args.db)
    cfg = load_config(str(HEARTBEAT_ROOT / "config" / "default.yaml"))
    end = time.time()
    start = end - args.days * 86400

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registration": "s3_heartbeat_confirmer_REGISTRATION.md",
        "window_days": args.days,
        "theta": args.theta,
        "fee_bps_side": FEE_BPS,
        "pairs": {},
        "pooled": {},
    }
    all_A, all_B, all_C, all_D = [], [], [], []
    covered = 0
    total_A = 0

    print("seeding S3 universe (breadth)...")
    try:
        s3_strat, s3_daily = _seed_universe(db)
        print(f"  seeded {sorted(s3_daily)}")
    except Exception as e:
        print(f"S3 seed failed: {e}", file=sys.stderr)
        s3_strat, s3_daily = None, None

    for pair in GATE_ASSETS:
        print(f"== {pair}: replay tape + S3 X1 ==")
        series = _p_up_series(pair, dict(cfg), start, end)
        print(f"  posterior candles: {len(series)}")
        trades = _simulate_x1_on_entries(
            pair, db, start, end, strat=s3_strat, daily_by=s3_daily)
        print(f"  X1 trades (S3 gated): {len(trades)}")
        A, B, C, D = [], [], [], []
        rng = random.Random(42)
        for t in trades:
            total_A += 1
            A.append(t)
            p = _lookup_p_up(series, t["entry_ts"])
            if p is not None:
                covered += 1
            # B: keep if fail-open (missing) OR p_up >= theta
            if p is None or p >= args.theta:
                B.append(t)
            # C: inverse — keep only when confirmer is confident against
            if p is not None and p < args.theta:
                C.append(t)
            if rng.random() < 0.5:
                D.append(t)
        st = {
            "A_s3_only": _x1_stats(A),
            "B_s3_plus_hb": _x1_stats(B),
            "C_inverse": _x1_stats(C),
            "D_random50": _x1_stats(D),
            "posterior_candles": len(series),
            "n_trades": len(trades),
        }
        report["pairs"][pair] = st
        all_A += A
        all_B += B
        all_C += C
        all_D += D
        print(" ", st)

    cov = (covered / total_A) if total_A else 0.0
    pooled_stats = {
        "A_s3_only": _x1_stats(all_A),
        "B_s3_plus_hb": _x1_stats(all_B),
        "C_inverse": _x1_stats(all_C),
        "D_random50": _x1_stats(all_D),
        "coverage_ok_frac": round(cov, 4),
    }
    if cov < 0.50:
        verdict = {"decision": "INCONCLUSIVE", "why": "C5 coverage < 0.50",
                   "coverage": cov}
    else:
        verdict = _verdict(pooled_stats)
    pooled_stats["verdict"] = verdict
    report["pooled"] = pooled_stats
    print("VERDICT", verdict)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
