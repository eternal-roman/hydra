"""Bakeoff A (S1) — close-confirmed exits replace touch-stops.

Pre-registered in `heartbeat/evidence/ABI_FUNNEL_STOPS_2026-07-18.md` §6 (S1,
F3/N2). Run EXACTLY as frozen; no sweeps. FAIL is a successful outcome.

Entries (identical across all arms): harness b1 — `paper_bounce_sim.causal_setups`
with entry at the close of bounce+1, setups already RESOLVED by then skipped via
`entry_index` (sorted by entry bar; one position per book, `in_pos_until`).

Datasets: BTC/ETH/ZEC 1d (resampled from 1h via `candles_from_sqlite`) + BTC 1h.
Folds: calendar year of ENTRY, 2016→2026 (2013–15 additionally for BTC where
data exists). Frozen params: target 3.3*ATR, horizon 200 bars, fees 26 bps/side.

Arms (only the stop-side exit differs; target/horizon identical, stop checked
before target within a bar — the conservative A0/A2 ordering from
`exit_layer_lab.py`):
  (i)   touch     trigger low < L0, fill min(close, L0)      [baseline A0]
  (ii)  mech      trigger low < L0, fill that bar's CLOSE    [mechanical control]
  (iii) confirm   trigger AND fill on close < L0             [close-confirm, A2]
  (inv) confirm_late  arm (iii) but the exit executes one bar LATE — at the
        close of the bar AFTER the confirming close (stale confirmation must
        give the gain back).

PROMOTE IFF (each reported separately):
  C1  pooled avg%/trade (iii) − (i) ≥ +0.3
  C2  (iii) > (ii) on ≥60% of folds (avg%/trade, folds where both arms traded)
  C3  (iii) > (i) per-asset (pooled avg%/trade) on ≥3 of 4 datasets
  INV confirm_late must give back the (iii)−(i) gain (reported, qualitative)

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_close_confirm.py
Writes: heartbeat/evidence/bakeoffs/close_confirm_exits.json
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config          # noqa: E402
import paper_bounce_sim as sim                    # noqa: E402
from bounce_geometry_study import candles_from_sqlite  # noqa: E402

DB = str(HYDRA_ROOT / "hydra_history.sqlite")
FEE = 0.0026                     # 26 bps per side, frozen
TGT = 3.3                        # * ATR, frozen
HORIZON = 200                    # bars past the setup low, frozen
ARMS = ("touch", "mech", "confirm", "confirm_late")
DATASETS = [("BTC/USD", 24), ("ETH/USD", 24), ("ZEC/USD", 24), ("BTC/USD", 1)]


def b1_entries(candles, setups):
    """Harness b1 entry list: (entry_idx, setup), resolved setups skipped,
    sorted by entry bar so the one-position book is well-defined."""
    ent = []
    for s in setups:
        e = sim.entry_index(candles, s, 1)
        if e is not None:
            ent.append((e, s))
    ent.sort(key=lambda t: t[0])
    return ent


def run_arm(candles, entries, arm):
    """One-position paper book over the full tape; returns closed trades."""
    trades = []
    in_pos_until = -1
    n = len(candles)
    for e, s in entries:
        if e <= in_pos_until:
            continue
        L0, a = s["low_px"], s["atr"]
        entry_px = candles[e].close
        tgt_px = L0 + TGT * a
        exit_px = k_exit = reason = None
        pending_late = False
        for k in range(e + 1, n):
            c = candles[k]
            if pending_late:                       # stale confirmation fill
                exit_px, k_exit, reason = c.close, k, "stop_late"
                break
            if arm == "touch" and c.low < L0:
                exit_px, k_exit, reason = min(c.close, L0), k, "stop"
                break
            if arm == "mech" and c.low < L0:
                exit_px, k_exit, reason = c.close, k, "stop"
                break
            if arm in ("confirm", "confirm_late") and c.close < L0:
                if arm == "confirm":
                    exit_px, k_exit, reason = c.close, k, "stop"
                    break
                pending_late = True
                continue
            if c.high >= tgt_px:
                exit_px, k_exit, reason = tgt_px, k, "target"
                break
            if k - s["low_idx"] > HORIZON:
                exit_px, k_exit, reason = c.close, k, "time"
                break
        if exit_px is None:                        # tape ended in-position
            exit_px, k_exit = candles[-1].close, n - 1
            reason = "stop_late_eod" if pending_late else "eod"
        ret = exit_px / entry_px - 1.0 - 2 * FEE
        trades.append({"ret": ret, "reason": reason, "hold": k_exit - e,
                       "entry_ts": int(candles[e].open_ts)})
        in_pos_until = k_exit
    return trades


def fold_of(trade):
    return _dt.datetime.fromtimestamp(trade["entry_ts"], _dt.UTC).year


def stats(trades):
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    stop_n = sum(v for k, v in reasons.items() if k.startswith("stop"))
    return {"n": len(rets),
            "avg_ret_pct": round(100 * sum(rets) / len(rets), 3),
            "total_ret_pct": round(100 * (eq - 1), 2),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 3),
            "stop_frac": round(stop_n / len(rets), 2),
            "avg_hold": round(sum(t["hold"] for t in trades) / len(trades), 1),
            "exit_reasons": reasons}


def main() -> int:
    cfg = load_config(None)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "design": "ABI_FUNNEL_STOPS_2026-07-18.md §6 S1 (frozen)",
              "frozen": {"fee_per_side": FEE, "target_atr": TGT,
                         "horizon": HORIZON, "entries": "b1"},
              "datasets": {}}
    pooled = {arm: [] for arm in ARMS}            # trades in registered folds
    per_ds_avg = {}                               # dataset -> arm -> avg%
    fold_rows = []                                # (dataset, year, arm avgs)

    for pair, bh in DATASETS:
        key = f"{pair}@{bh}h"
        candles = candles_from_sqlite(DB, pair, bh)
        setups = sim.causal_setups(candles, cfg)
        entries = b1_entries(candles, setups)
        min_year = 2013 if pair == "BTC/USD" else 2016
        ds = {"n_candles": len(candles), "n_setups": len(setups),
              "n_b1_entries": len(entries), "fold_min_year": min_year,
              "arms": {}}
        arm_trades = {}
        for arm in ARMS:
            tr = run_arm(candles, entries, arm)
            tr = [t for t in tr if fold_of(t) >= min_year]   # registered folds
            arm_trades[arm] = tr
            pooled[arm].extend(tr)
            folds = {}
            for t in tr:
                folds.setdefault(fold_of(t), []).append(t)
            ds["arms"][arm] = {"pooled": stats(tr),
                               "folds": {str(y): stats(v)
                                         for y, v in sorted(folds.items())}}
        per_ds_avg[key] = {arm: ds["arms"][arm]["pooled"].get("avg_ret_pct")
                           for arm in ARMS}
        years = sorted({fold_of(t) for arm in ARMS for t in arm_trades[arm]})
        for y in years:
            row = {"dataset": key, "year": y}
            for arm in ARMS:
                ts = [t for t in arm_trades[arm] if fold_of(t) == y]
                row[arm] = (round(100 * sum(t["ret"] for t in ts) / len(ts), 3)
                            if ts else None)
                row[f"n_{arm}"] = len(ts)
            fold_rows.append(row)
        report["datasets"][key] = ds
        print(f"== {key}: {len(entries)} b1 entries; pooled avg%/trade: "
              + ", ".join(f"{a}={per_ds_avg[key][a]}" for a in ARMS))

    # ---- criteria ----
    def avg(trs):
        return 100 * sum(t["ret"] for t in trs) / len(trs) if trs else None

    p_i, p_ii, p_iii, p_inv = (avg(pooled[a]) for a in ARMS)
    c1_delta = p_iii - p_i
    c1 = c1_delta >= 0.3

    comp = [r for r in fold_rows
            if r["confirm"] is not None and r["mech"] is not None]
    c2_wins = sum(1 for r in comp if r["confirm"] > r["mech"])
    c2_frac = c2_wins / len(comp) if comp else 0.0
    c2 = c2_frac >= 0.6

    c3_assets = {k: (v["confirm"] is not None and v["touch"] is not None
                     and v["confirm"] > v["touch"])
                 for k, v in per_ds_avg.items()}
    c3_n = sum(c3_assets.values())
    c3 = c3_n >= 3

    inv_gain_kept = ((p_inv - p_i) / c1_delta) if c1_delta else None
    inverse_ok = p_inv < p_iii and (c1_delta <= 0 or p_inv - p_i < 0.5 * c1_delta)

    report["pooled_avg_ret_pct"] = {a: round(v, 4) for a, v in
                                    zip(ARMS, (p_i, p_ii, p_iii, p_inv))}
    report["pooled_n"] = {a: len(pooled[a]) for a in ARMS}
    report["fold_table"] = fold_rows
    report["criteria"] = {
        "C1_pooled_iii_minus_i": {"value_pct_per_trade": round(c1_delta, 4),
                                  "threshold": 0.3, "pass": c1},
        "C2_iii_gt_ii_folds": {"wins": c2_wins, "total": len(comp),
                               "frac": round(c2_frac, 3), "threshold": 0.6,
                               "pass": c2},
        "C3_iii_gt_i_per_asset": {"by_dataset": c3_assets, "n_pass": c3_n,
                                  "threshold": 3, "pass": c3},
        "INVERSE_stale_confirmation": {
            "confirm_late_pooled_avg": round(p_inv, 4),
            "gain_fraction_kept_by_late": round(inv_gain_kept, 3)
            if inv_gain_kept is not None else None,
            "gives_gain_back": inverse_ok},
    }
    promote = c1 and c2 and c3 and inverse_ok
    report["verdict"] = "PROMOTE" if promote else "FAIL"

    out = HEARTBEAT_ROOT.parent / "research" / "data" / "s3" / "killed" / "close_confirm_exits.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["criteria"], indent=2))
    print("VERDICT:", report["verdict"])
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
