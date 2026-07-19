"""ABI bore: entry-time watermarks of S3 losses + post-target continuation.

Rebuilds the exact X1 gated pools (walk-forward, per fold), then attaches
to every trade all candidate entry-time observables and forward outcomes.
Output: JSON rows to scratchpad for analysis.
"""
import datetime as dt
import json
import sys
from pathlib import Path

HB = Path(r"C:\Users\elamj\Dev\Hydra\heartbeat")
sys.path.insert(0, str(HB / "src"))
sys.path.insert(0, str(HB / "tools"))

from heartbeat.config import load_config
from heartbeat.engine.posterior import sigmoid
import paper_bounce_sim as sim
from bounce_geometry_study import candles_from_sqlite
from exit_layer_lab import daily_scores, score_at
from bakeoff_s3_daily_classifier import (
    DB, ASSETS, FEATURES, YEARS, MIN_TRAIN,
    shock_flags, fresh_low_days, build_features,
    fit_logistic, standardizer, zrow, year_ts)

FEE = sim.FEE


def d(ts):
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


def sma(closes, i, p):
    if i + 1 < p:
        return None
    return sum(closes[i + 1 - p:i + 1]) / p


def x1_trade(candles, s, e, scores):
    """X1 close-stop exit from entry index e; returns exit info + path stats."""
    entry = candles[e].close
    L0 = s["low_px"]
    tgt = L0 + sim.TARGET_ATR * s["atr"]
    exit_px = reason = k_exit = None
    wick_below = False
    for k in range(e + 1, len(candles)):
        c = candles[k]
        if c.low < L0:
            wick_below = True
        if c.close < L0:
            exit_px, reason, k_exit = c.close, "stop_close", k
        elif c.high >= tgt:
            exit_px, reason, k_exit = tgt, "target", k
        elif k - s["low_idx"] > sim.HORIZON:
            exit_px, reason, k_exit = c.close, "time", k
        if exit_px is not None:
            break
    if exit_px is None:
        k_exit, exit_px, reason = len(candles) - 1, candles[-1].close, "eod"
    # forward pure returns and MFE/MAE from entry (60d window)
    fwd = {}
    for K in (10, 20, 40, 60):
        j = min(e + K, len(candles) - 1)
        fwd[K] = candles[j].close / entry - 1 - 2 * FEE
    win = candles[e + 1:min(e + 61, len(candles))]
    mfe = max((c.high / entry - 1 for c in win), default=0.0)
    mae = min((c.low / entry - 1 for c in win), default=0.0)
    # post-exit continuation (target exits only meaningful)
    pext = {}
    for K in (5, 10, 20, 40):
        j = min(k_exit + K, len(candles) - 1)
        pext[K] = candles[j].close / exit_px - 1
    return {"entry_px": entry, "exit_px": exit_px, "reason": reason,
            "hold": k_exit - e, "k_exit": k_exit,
            "ret": exit_px / entry - 1 - 2 * FEE,
            "wick_below": wick_below, "fwd": fwd, "mfe": mfe, "mae": mae,
            "pext": pext}


def main():
    cfg = load_config(None)
    candles_by_pair = {p: candles_from_sqlite(DB, p, 24) for p in ASSETS}
    low_days_by_pair = {p: fresh_low_days(c) for p, c in candles_by_pair.items()}
    rows = []
    for pair in ASSETS:
        candles = candles_by_pair[pair]
        closes = [c.close for c in candles]
        setups = sim.causal_setups(candles, cfg)
        flags = shock_flags(candles)
        build_features(candles, setups, flags, low_days_by_pair)
        labeled = [s for s in setups if s["label"] is not None]
        scores = daily_scores(pair)
        in_pos = -1
        for y in YEARS:
            cut = year_ts(y)
            train = [s for s in labeled
                     if s["resolve_ts"] is not None and s["resolve_ts"] < cut]
            ys = [1 if s["label"] == "reversal" else 0 for s in train]
            if len(train) < MIN_TRAIN[pair] or len(set(ys)) < 2:
                continue
            mu, sd = standardizer([[s["x"][f] for f in FEATURES] for s in train])
            X = [zrow([s["x"][f] for f in FEATURES], mu, sd) for s in train]
            b0, w = fit_logistic(X, ys)

            def prob(s):
                return sigmoid(b0 + sum(wj * xj for wj, xj in
                                        zip(w, zrow([s["x"][f] for f in FEATURES],
                                                    mu, sd))))
            thr = sim.pct(sorted(prob(s) for s in train), 0.75)
            lo, hi = cut, year_ts(y + 1) - 1
            for s in setups:
                p = prob(s)
                if p < thr:
                    continue
                e = sim.entry_index(candles, s, 1)
                if e is None:
                    continue
                ts = candles[e].open_ts
                if ts < lo or ts > hi or e <= in_pos:
                    continue
                t = x1_trade(candles, s, e, scores)
                in_pos = t["k_exit"]
                entry = t["entry_px"]
                ens = score_at(scores, ts, 24)
                ma200 = sma(closes, e, 200)
                ma50 = sma(closes, e, 50)
                hi60 = max(closes[max(0, e - 60):e]) if e else entry
                rows.append({
                    "pair": pair, "entry_date": d(ts), "year": y,
                    **{f: s["x"][f] for f in FEATURES},
                    "prob": p, "thr": thr, "margin": p - thr,
                    "ensemble": ens,
                    "vs_ma200": entry / ma200 - 1 if ma200 else None,
                    "vs_ma50": entry / ma50 - 1 if ma50 else None,
                    "premium_atr": (entry - s["low_px"]) / s["atr"],
                    "atr_pct": s["atr"] / entry * 100,
                    "leg_depth": s["low_px"] / hi60 - 1,
                    "days_low_entry": e - s["low_idx"],
                    "label": s["label"],
                    "reason": t["reason"], "ret": t["ret"], "hold": t["hold"],
                    "wick_below": t["wick_below"],
                    "fwd10": t["fwd"][10], "fwd20": t["fwd"][20],
                    "fwd40": t["fwd"][40], "fwd60": t["fwd"][60],
                    "mfe": t["mfe"], "mae": t["mae"],
                    "pext5": t["pext"][5], "pext10": t["pext"][10],
                    "pext20": t["pext"][20], "pext40": t["pext"][40]})
    out = Path(__file__).with_name("s3_watermarks.json")
    out.write_text(json.dumps(rows, indent=1))
    print(f"wrote {len(rows)} trades -> {out}")


if __name__ == "__main__":
    main()
