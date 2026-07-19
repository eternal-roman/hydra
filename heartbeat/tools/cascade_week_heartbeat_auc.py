"""Measurement: cascade-week vs quiet-week heartbeat AUC (F3).

Registration: evidence/bakeoffs/cascade_week_heartbeat_REGISTRATION.md
Uses real tape + calibrated weights; no trading decisions.

Usage:
  PYTHONPATH=src python tools/cascade_week_heartbeat_auc.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))

from heartbeat.config import load_config  # noqa: E402
from heartbeat.engine.pipeline import run_tape  # noqa: E402
from heartbeat.eval.labeler import extract_events  # noqa: E402
from heartbeat.eval.metrics import roc_auc  # noqa: E402
from heartbeat.store import Store  # noqa: E402
from heartbeat.weights_io import apply_weights_to_config, find_weights  # noqa: E402
from heartbeat.engine.candle import candles_from_trades  # noqa: E402

OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "cascade_week_heartbeat.json"
ASSETS = ("BTC/USD", "ETH/USD", "SOL/USD")
TF = "1h"


def _replay(pair: str, cfg: dict, days: int):
    store = Store(str(HEARTBEAT_ROOT / cfg["store"]["root"]))
    found = find_weights(pair, TF, store_root=cfg["store"]["root"],
                         package_root=HEARTBEAT_ROOT)
    local = dict(cfg)
    if found:
        apply_weights_to_config(local, found[0])
    end = time.time()
    start = end - days * 86400
    trades = store.read_tape(pair, TF, ts_start=start, ts_end=end)
    if len(trades) < 100:
        return None, None, None
    rows = run_tape(local, pair, TF, trades)
    candles = candles_from_trades(trades, TF, include_final=True)
    return candles, rows, trades


def _lows_20d(candles) -> list[float]:
    """Timestamps of 20-day (480h) lows on 1h series."""
    if not candles or len(candles) < 480:
        return []
    out = []
    for i in range(480, len(candles)):
        window = candles[i - 480:i + 1]
        low_i = min(window, key=lambda c: c.low)
        if low_i is candles[i] or (low_i.low == candles[i].low and low_i.open_ts == candles[i].open_ts):
            out.append(float(candles[i].open_ts))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    cfg = load_config(str(HEARTBEAT_ROOT / "config" / "default.yaml"))

    lows_by_pair = {}
    events_by_pair = {}
    for pair in ASSETS:
        print(f"replay {pair}...")
        candles, rows, _ = _replay(pair, cfg, args.days)
        if not candles or not rows:
            print(f"  skip {pair}")
            continue
        lows_by_pair[pair] = _lows_20d(candles)
        p_up = [r["p_up"] for r in rows]
        events = extract_events(pair, TF, candles, p_up, cfg)
        events_by_pair[pair] = events
        print(f"  events={len(events)} lows20d={len(lows_by_pair[pair])}")

    def is_cascade(ts: float) -> bool:
        hits = 0
        for pair, lows in lows_by_pair.items():
            if any(abs(ts - L) <= 72 * 3600 for L in lows):
                hits += 1
        return hits >= 2

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registration": "cascade_week_heartbeat_REGISTRATION.md",
        "days": args.days,
        "pairs": {},
        "pooled": {},
    }
    pool_c_pos, pool_c_neg = [], []
    pool_q_pos, pool_q_neg = [], []

    for pair, events in events_by_pair.items():
        c_pos, c_neg, q_pos, q_neg = [], [], [], []
        for e in events:
            p_at = e.p_at or {}
            p3 = p_at.get("bounce+3")
            if p3 is None or e.label not in ("reversal", "fake"):
                continue
            y = 1 if e.label == "reversal" else 0
            ts = float(e.low_ts)
            if is_cascade(ts):
                (c_pos if y else c_neg).append(float(p3))
            else:
                (q_pos if y else q_neg).append(float(p3))

        auc_c = roc_auc(c_pos, c_neg)
        auc_q = roc_auc(q_pos, q_neg)
        report["pairs"][pair] = {
            "cascade_n": len(c_pos) + len(c_neg),
            "quiet_n": len(q_pos) + len(q_neg),
            "auc_cascade": round(auc_c, 4) if auc_c is not None else None,
            "auc_quiet": round(auc_q, 4) if auc_q is not None else None,
        }
        pool_c_pos += c_pos
        pool_c_neg += c_neg
        pool_q_pos += q_pos
        pool_q_neg += q_neg
        print(pair, report["pairs"][pair])

    auc_c = roc_auc(pool_c_pos, pool_c_neg)
    auc_q = roc_auc(pool_q_pos, pool_q_neg)
    decision = "NO_BLACKOUT"
    if (auc_c is not None and auc_q is not None
            and auc_c <= 0.55 and auc_q >= 0.70):
        decision = "DISPLAY_CASCADE_SUSPECT"
    report["pooled"] = {
        "auc_cascade": round(auc_c, 4) if auc_c is not None else None,
        "auc_quiet": round(auc_q, 4) if auc_q is not None else None,
        "n_cascade": len(pool_c_pos) + len(pool_c_neg),
        "n_quiet": len(pool_q_pos) + len(pool_q_neg),
        "decision": decision,
    }
    print("pooled", report["pooled"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
