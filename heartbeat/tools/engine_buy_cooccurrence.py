"""Diagnostic: engine BUY co-occurrence with S3 gated days (ABI stub 2).

Registration: evidence/bakeoffs/engine_buy_cooccurrence_REGISTRATION.md
NOT a strategy gate — forbids false claims when n_BUY is tiny.

Usage (repo root):
  python heartbeat/tools/engine_buy_cooccurrence.py
  python heartbeat/tools/engine_buy_cooccurrence.py --days 365 --max-ticks 4000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HYDRA_ROOT))
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))

from hydra_backtest import BacktestConfig, BacktestRunner  # noqa: E402

OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "engine_buy_cooccurrence.json"
DB_DEFAULT = HYDRA_ROOT / "hydra_history.sqlite"
PAIRS = ("BTC/USD", "ETH/USD")
GRAIN = 3600


def _sqlite_bounds(db: Path, pair: str) -> tuple[int, int] | None:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT MIN(ts), MAX(ts) FROM ohlc WHERE pair=? AND grain_sec=?",
            (pair, GRAIN),
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def _seed_s3_universe(db: Path):
    """Seed full S3 universe — breadth fails open-gate if members missing."""
    from bounce_geometry_study import candles_from_sqlite
    sys.path.insert(0, str(HYDRA_ROOT / "s3bounce"))
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


def _count_s3_gated_days(db: Path, pair: str, start_ts: int, end_ts: int,
                         strat=None, daily_by=None) -> set[int]:
    """Days (UTC day index) with S3 gated entryable_b1 using frozen artifact."""
    try:
        if strat is None or daily_by is None:
            strat, daily_by = _seed_s3_universe(db)
    except Exception as e:
        print(f"S3 path unavailable ({e}); s3_gated_days=0", file=sys.stderr)
        return set()

    daily = daily_by.get(pair)
    if not daily or pair not in getattr(strat, "universe", ()):
        return set()
    gated_days: set[int] = set()
    for i, c in enumerate(daily):
        ts = int(c.open_ts)
        if ts < start_ts or ts > end_ts:
            continue
        now = daily[i + 1].open_ts if i + 1 < len(daily) else ts + 86400
        try:
            sig = strat.evaluate(pair, now)
        except Exception:
            continue
        if getattr(sig, "stage", None) == "entryable_b1" and getattr(sig, "gated", False):
            gated_days.add(ts // 86400)
    return gated_days


def _run_engine(pair: str, db: Path, start_ts: int, end_ts: int,
                max_ticks: int) -> dict:
    cfg = BacktestConfig(
        name=f"cooc_{pair.replace('/', '_')}",
        pairs=(pair,),
        candle_interval=60,
        mode="competition",
        data_source="sqlite",
        data_source_params_json=json.dumps({
            "db_path": str(db),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "grain_sec": GRAIN,
        }),
        max_ticks=max_ticks,
        coordinator_enabled=False,
        random_seed=42,
    )
    result = BacktestRunner(cfg).run()
    buy_fills = 0
    buy_signals = 0
    buy_days: set[int] = set()
    for t in (result.trade_log or []):
        action = (t.get("action") or t.get("side") or "").upper()
        if action in ("BUY", "buy"):
            buy_fills += 1
            ts = t.get("timestamp") or t.get("ts") or t.get("fill_ts")
            if ts:
                buy_days.add(int(float(ts)) // 86400)
    for pair_key, slog in (result.signal_log or {}).items():
        for s in slog or []:
            if (s.get("action") or "").upper() == "BUY":
                buy_signals += 1
    return {
        "status": result.status,
        "buy_fills": buy_fills,
        "buy_signals": buy_signals,
        "buy_days": sorted(buy_days),
        "total_trades": getattr(result.metrics, "total_trades", None),
        "n_ticks": getattr(result, "ticks_processed", None)
        or getattr(result, "candles_processed", None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--max-ticks", type=int, default=5000)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    db = Path(args.db)
    if not db.is_file():
        print(f"missing db {db}", file=sys.stderr)
        return 2

    now = int(time.time())
    window_start = now - args.days * 86400
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registration": "engine_buy_cooccurrence_REGISTRATION.md",
        "days": args.days,
        "pairs": {},
        "pooled": {},
    }
    total_fills = 0
    total_cooc = 0
    total_s3 = 0

    try:
        s3_strat, s3_daily = _seed_s3_universe(db)
        print(f"S3 universe seeded: {sorted(s3_daily)}")
    except Exception as e:
        print(f"S3 seed failed ({e})", file=sys.stderr)
        s3_strat, s3_daily = None, None

    for pair in PAIRS:
        bounds = _sqlite_bounds(db, pair)
        if not bounds:
            report["pairs"][pair] = {"error": "no_ohlc"}
            continue
        start_ts = max(window_start, bounds[0])
        end_ts = min(now, bounds[1])
        eng = _run_engine(pair, db, start_ts, end_ts, args.max_ticks)
        s3_days = _count_s3_gated_days(
            db, pair, start_ts, end_ts, strat=s3_strat, daily_by=s3_daily)
        buy_days = set(eng.get("buy_days") or [])
        cooc = sorted(buy_days & s3_days)
        eng_out = {
            **{k: v for k, v in eng.items() if k != "buy_days"},
            "n_buy_days": len(buy_days),
            "n_s3_gated_days": len(s3_days),
            "n_cooccurrence_days": len(cooc),
            "cooccurrence_days": cooc[:50],
            "window": {"start_ts": start_ts, "end_ts": end_ts},
        }
        report["pairs"][pair] = eng_out
        total_fills += int(eng.get("buy_fills") or 0)
        total_cooc += len(cooc)
        total_s3 += len(s3_days)
        print(f"{pair}: buy_fills={eng.get('buy_fills')} "
              f"s3_days={len(s3_days)} cooc_days={len(cooc)}")

    forbid = total_fills < 20
    report["pooled"] = {
        "buy_fills": total_fills,
        "s3_gated_days": total_s3,
        "cooccurrence_days": total_cooc,
        "verdict": (
            "FORBID_ENGINE_HB_CLAIMS"
            if forbid else
            ("ORTHOGONAL_BOOKS" if total_cooc == 0 else "COOCCURRENCE_PRESENT")
        ),
        "n_buy_threshold": 20,
        "thesis": (
            "n_BUY < 20 → cannot claim heartbeat/S3 improves engine entries"
            if forbid else
            "engine path has enough fills for a future engine confirmer bakeoff"
        ),
    }
    print("pooled", report["pooled"]["verdict"], "fills", total_fills)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
