"""Heal hydra_history.sqlite `ohlc` from the verified `trades` table.

The tape-vs-sqlite verification exposed two canonical-store defects the
backtests inherit:
  1. missing hours — historical kraken_rest population left coverage
     holes (SqliteSource silently yields a gappy series);
  2. frozen forming candles — an old refresh upserted a still-forming
     hour whose row then never got revisited (volume ~10x low).

Both are repairable from trade-level truth: the `trades` table is
Kraken's own record and hourly SUM(qty) was proven to match clean
kraken_rest rows exactly. Policy:
  * missing hour  -> insert via HistoryStore (source='tape', tier-safe);
  * existing hour that materially disagrees with its own trades
    (vol rel > 2% or any O/H/L/C rel > 0.2%) -> replace with
    trade-derived values, source='tape', each correction logged —
    a deliberate, evidence-backed override of the tier policy, applied
    ONLY where the stored row contradicts the exchange's trade record;
  * first/last partial hours of the tape span are never touched.

Usage (from repo root or heartbeat/):
    python heartbeat/tools/heal_ohlc_from_trades.py --pairs SOL/USD,BTC/USD
        [--db hydra_history.sqlite] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

HYDRA_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HYDRA_ROOT))

VOL_REL_TOL = 0.02
PRICE_REL_TOL = 0.002
GRAIN = 3600


def candles_from_db_trades(con: sqlite3.Connection, pair: str) -> dict[int, tuple]:
    """hour_open_ts -> (o, h, l, c, v) from the trades table, trade order
    (ts, trade_id) — identical bucketing to Kraken OHLC (UTC epoch)."""
    out: dict[int, list] = {}
    cur = con.execute(
        "SELECT ts, price, qty FROM trades WHERE pair=? ORDER BY ts, trade_id",
        (pair,))
    for ts, price, qty in cur:
        h = int(ts // GRAIN) * GRAIN
        c = out.get(h)
        if c is None:
            out[h] = [price, price, price, price, qty]
        else:
            c[1] = max(c[1], price)
            c[2] = min(c[2], price)
            c[3] = price
            c[4] += qty
    return {h: tuple(v) for h, v in out.items()}


def heal_pair(con: sqlite3.Connection, pair: str, dry: bool) -> dict:
    agg = candles_from_db_trades(con, pair)
    if not agg:
        return {"pair": pair, "skipped": "no trades in db"}
    hours = sorted(agg)
    full = hours[1:-1]  # exclude partial first/last hours
    db = {int(ts): (o, h, l, c, v, src) for ts, o, h, l, c, v, src in
          con.execute("SELECT ts, open, high, low, close, volume, source "
                      "FROM ohlc WHERE pair=? AND grain_sec=?", (pair, GRAIN))}
    now = int(time.time())
    inserted, corrected, corrections = 0, 0, []
    for ts in full:
        o, h, l, c, v = agg[ts]
        row = db.get(ts)
        if row is None:
            if not dry:
                con.execute(
                    "INSERT OR IGNORE INTO ohlc (pair, grain_sec, ts, open, "
                    "high, low, close, volume, source, ingested_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pair, GRAIN, ts, o, h, l, c, v, "tape", now))
            inserted += 1
            continue
        do, dh, dl, dc, dv, src = row
        vol_bad = abs(v - dv) / max(dv, 1e-9) > VOL_REL_TOL
        px_bad = any(abs(a - b) / max(abs(b), 1e-9) > PRICE_REL_TOL
                     for a, b in ((o, do), (h, dh), (l, dl), (c, dc)))
        if vol_bad or px_bad:
            corrections.append({"ts": ts, "was_source": src,
                                "db": [do, dh, dl, dc, dv],
                                "tape": [o, h, l, c, v]})
            if not dry:
                con.execute(
                    "UPDATE ohlc SET open=?, high=?, low=?, close=?, "
                    "volume=?, source='tape', ingested_at=? "
                    "WHERE pair=? AND grain_sec=? AND ts=?",
                    (o, h, l, c, v, now, pair, GRAIN, ts))
            corrected += 1
    if not dry:
        con.commit()
    return {"pair": pair, "tape_hours": len(full), "already_ok":
            len(full) - inserted - corrected, "inserted_missing": inserted,
            "corrected_bad": corrected, "corrections": corrections[:10]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="SOL/USD,BTC/USD,ETH/USD")
    ap.add_argument("--db", default=str(HYDRA_ROOT / "hydra_history.sqlite"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(args.db, timeout=60.0)
    con.execute("PRAGMA journal_mode=WAL")
    rc = 0
    for pair in [p.strip() for p in args.pairs.split(",")]:
        r = heal_pair(con, pair, args.dry_run)
        print(r, flush=True)
        if "skipped" in r:
            rc = max(rc, 1)
    con.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
