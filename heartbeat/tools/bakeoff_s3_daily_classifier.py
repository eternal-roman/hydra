"""Bakeoff 1 — S3: daily candle-only bounce classifier (pre-registered).

Frozen design (heartbeat/evidence/ABI_FUNNEL_2026-07-18.md §6, S3):
  * assets BTC/USD, ETH/USD, ZEC/USD; DAILY bars resampled from the 1h
    `ohlc` table of hydra_history.sqlite;
  * setups + oracle labels from paper_bounce_sim.causal_setups on daily
    candles (labeler geometry, entry candidates only);
  * FROZEN feature list, computed at the bounce-confirm bar (causal —
    nothing after the entry-decision bar):
      clv            ((close-low)-(high-close))/(high-low) of the bounce candle
      range_atr      (high-low)/ATR of the bounce candle (ATR = the setup's
                     robust ATR14 from causal_setups — the only ATR the
                     harness defines; stated per the registration's
                     "state what you used" clause)
      vol_z          bounce-candle volume z-score vs the previous 20 bars
      shock_recency  bars since last daily |return| > 2*stdev(last 20
                     returns), evaluated at the bounce bar (its own return
                     counts), capped at 10; 10 when no shock/short history
      breadth        how many of {BTC,ETH,ZEC} made a fresh 20d low within
                     the last 3 daily bars at the SETUP date
      retest         1 if a prior low within the previous 30 bars came
                     within 0.25*ATR of the setup low
  * standardization uses TRAIN-fold mean/std only;
  * walk-forward: expanding yearly folds — train on all events RESOLVED
    (label bar's close) strictly before Jan 1 of year Y, test on year-Y
    events (year of the bounce bar), Y = 2016..2026. ETH/ZEC folds
    require >= 40 training events (registered); BTC folds require >= 10
    (fit sanity — BTC's pre-2016 daily history is thin, stated honestly);
  * logistic regression: intercept + fixed L2 = 1.0 on the feature
    weights (intercept unpenalized), full-batch gradient descent,
    lr = 0.1, 3000 iterations, zero init, deterministic — no sweeping;
  * metric 1: walk-forward AUC — each test event scored by the fold
    model's predicted probability (features at the bounce-confirm bar,
    the single frozen checkpoint; the registration's "bounce+3" names
    the heartbeat checkpoint this replaces), AUC vs oracle labels,
    pooled across test folds per asset and overall;
  * metric 2: gated P&L — entry b1 (close of bounce+1), exit target
    3.3*ATR / stop at setup low (touch, fill min(close, L0)) / 200-bar
    horizon, fees 26 bps/side; gate = predicted prob >= p75 of the TRAIN
    fold's predicted probs; arms gated vs ungated vs INVERSE (<= p25).

PROMOTE IFF (all): pooled walk-forward AUC >= 0.60 AND gated expectancy
> 0 net of fees on >= 2 of 3 assets AND inverse gate worse than ungated.

Secondary (exploratory, does NOT affect the verdict): gated arm rerun
with (a) close-confirmed stop (exit at close when close < L0) and
(b) ensemble-flip exit (daily ensemble < 0.6, exit_layer_lab semantics).

Usage (from heartbeat/):
    PYTHONPATH=src python tools/bakeoff_s3_daily_classifier.py
"""

from __future__ import annotations

import calendar
import datetime as _dt
import json
import statistics
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config            # noqa: E402
from heartbeat.engine.posterior import sigmoid      # noqa: E402
from heartbeat.eval.metrics import roc_auc          # noqa: E402

import paper_bounce_sim as sim                      # noqa: E402
from bounce_geometry_study import candles_from_sqlite  # noqa: E402
from exit_layer_lab import daily_scores, score_at   # noqa: E402

DB = str(HYDRA_ROOT / "hydra_history.sqlite")
ASSETS = ["BTC/USD", "ETH/USD", "ZEC/USD"]
FEATURES = ["clv", "range_atr", "vol_z", "shock_recency", "breadth", "retest"]
YEARS = list(range(2016, 2027))
MIN_TRAIN = {"BTC/USD": 10, "ETH/USD": 40, "ZEC/USD": 40}
L2, LR, ITERS = 1.0, 0.1, 3000
OUT = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "s3_daily_classifier.json"


# -- feature machinery (shared with bakeoff_freshness_gate) -------------------

def shock_flags(candles) -> list[bool]:
    """Per-bar flag: |daily return| > 2*stdev of the previous 20 returns.
    Causal: the sigma window ends at the prior bar."""
    n = len(candles)
    rets = [0.0] * n
    for i in range(1, n):
        prev = candles[i - 1].close
        rets[i] = candles[i].close / prev - 1.0 if prev else 0.0
    flags = [False] * n
    for i in range(21, n):          # need 20 prior returns (bars 1..)
        window = rets[i - 20:i]
        sd = statistics.pstdev(window)
        flags[i] = sd > 0 and abs(rets[i]) > 2.0 * sd
    return flags


def recency_at(flags, idx: int, cap: int = 10) -> int:
    """Bars since the last shock at-or-before idx, capped (cap = none seen)."""
    for back in range(cap):
        j = idx - back
        if j < 0:
            break
        if flags[j]:
            return back
    return cap


def fresh_low_days(candles) -> set[int]:
    """UTC day buckets where the bar's low undercut the prior 20 bars' lows."""
    out = set()
    lows = [c.low for c in candles]
    for i in range(20, len(candles)):
        if lows[i] < min(lows[i - 20:i]):
            out.add(int(candles[i].open_ts) // 86400)
    return out


def build_features(candles, setups, flags, low_days_by_pair) -> None:
    """Attach x (feature dict) and resolve_ts to each setup in place."""
    vols = [c.volume for c in candles]
    for s in setups:
        b, i = s["bounce_idx"], s["low_idx"]
        c = candles[b]
        rng = c.high - c.low
        clv = ((c.close - c.low) - (c.high - c.close)) / rng if rng > 0 else 0.0
        range_atr = rng / s["atr"]
        if b >= 20:
            w = vols[b - 20:b]
            mu, sd = statistics.mean(w), statistics.pstdev(w)
            vol_z = (vols[b] - mu) / sd if sd > 0 else 0.0
        else:
            vol_z = 0.0
        day = int(candles[i].open_ts) // 86400
        breadth = sum(1 for days in low_days_by_pair.values()
                      if any(d in days for d in (day - 2, day - 1, day)))
        retest = int(any(abs(candles[j].low - s["low_px"]) <= 0.25 * s["atr"]
                         for j in range(max(0, i - 30), i)))
        s["x"] = {"clv": clv, "range_atr": range_atr, "vol_z": vol_z,
                  "shock_recency": float(recency_at(flags, b)),
                  "breadth": float(breadth), "retest": float(retest)}
        # resolution bar (labeler scan replicated) -> close_ts, strict no-leak
        s["resolve_ts"] = None
        tgt = s["low_px"] + sim.TARGET_ATR * s["atr"]
        for j in range(b, min(len(candles), i + 1 + sim.HORIZON)):
            if candles[j].low < s["low_px"] or candles[j].high >= tgt:
                s["resolve_ts"] = candles[j].close_ts
                break


# -- deterministic logistic (intercept + fixed L2 on weights) -----------------

def fit_logistic(X, y, l2=L2, lr=LR, iters=ITERS):
    n, k = len(X), len(X[0])
    w, b = [0.0] * k, 0.0
    for _ in range(iters):
        gw, gb = [0.0] * k, 0.0
        for xi, yi in zip(X, y):
            err = sigmoid(b + sum(w[j] * xi[j] for j in range(k))) - yi
            gb += err
            for j in range(k):
                gw[j] += err * xi[j]
        b -= lr * gb / n
        for j in range(k):
            w[j] -= lr * (gw[j] / n + l2 * w[j] / n)
    return b, w


def standardizer(train_rows):
    mu, sd = [], []
    for j in range(len(FEATURES)):
        col = [r[j] for r in train_rows]
        mu.append(statistics.mean(col))
        s = statistics.pstdev(col)
        sd.append(s if s > 1e-12 else 1.0)
    return mu, sd


def zrow(row, mu, sd):
    return [(row[j] - mu[j]) / sd[j] for j in range(len(row))]


# -- exploratory exit variants ------------------------------------------------

def simulate_variant(candles, pool, variant, scores=None,
                     lo_ts=None, hi_ts=None) -> list[dict]:
    """b1 entries on a pre-filtered pool; exit per variant:
    'close_stop' = exit at close when close < L0 (else target 3.3 / horizon);
    'flip' = exit at close when daily ensemble < 0.6 (no stop/target,
    horizon retained). Fees/sequencing identical to sim.simulate."""
    trades, in_pos = [], -1
    for s in pool:
        e = sim.entry_index(candles, s, 1)
        if e is None:
            continue
        ts = candles[e].open_ts
        if (lo_ts is not None and ts < lo_ts) or (hi_ts is not None and ts > hi_ts):
            continue
        if e <= in_pos:
            continue
        entry = candles[e].close
        L0, tgt = s["low_px"], s["low_px"] + sim.TARGET_ATR * s["atr"]
        exit_px = reason = k_exit = None
        for k in range(e + 1, len(candles)):
            c = candles[k]
            if variant == "close_stop":
                if c.close < L0:
                    exit_px, reason = c.close, "stop_close"
                elif c.high >= tgt:
                    exit_px, reason = tgt, "target"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            else:  # flip
                sc = score_at(scores, c.open_ts, 24)
                if sc is not None and sc < 0.6:
                    exit_px, reason = c.close, "flip"
                elif k - s["low_idx"] > sim.HORIZON:
                    exit_px, reason = c.close, "time"
            if exit_px is not None:
                k_exit = k
                break
        if exit_px is None:
            k_exit, exit_px, reason = len(candles) - 1, candles[-1].close, "eod"
        trades.append({"entry_ts": ts, "ret": exit_px / entry - 1.0 - 2 * sim.FEE,
                       "reason": reason, "hold": k_exit - e})
        in_pos = k_exit
    return trades


# -- per-asset walk-forward ---------------------------------------------------

def year_ts(y: int) -> int:
    return calendar.timegm((y, 1, 1, 0, 0, 0))


def run_asset(pair: str, cfg, low_days_by_pair, candles_by_pair) -> dict:
    candles = candles_by_pair[pair]
    setups = sim.causal_setups(candles, cfg)
    flags = shock_flags(candles)
    build_features(candles, setups, flags, low_days_by_pair)
    labeled = [s for s in setups if s["label"] is not None]
    scores = daily_scores(pair)

    res = {"n_daily_candles": len(candles), "n_setups": len(setups),
           "n_labeled": len(labeled), "folds": [],
           "pooled": {}, "final_fold_model": None}
    auc_pool = []                       # (prob, label01)
    trades = {a: [] for a in ("ungated", "gated", "inverse",
                              "gated_close_stop", "gated_flip")}

    for y in YEARS:
        cut = year_ts(y)
        train = [s for s in labeled
                 if s["resolve_ts"] is not None and s["resolve_ts"] < cut]
        test = [s for s in labeled
                if _dt.datetime.fromtimestamp(candles[s["bounce_idx"]].open_ts,
                                              _dt.UTC).year == y]
        fold = {"year": y, "train_n": len(train), "test_n": len(test)}
        ys = [1 if s["label"] == "reversal" else 0 for s in train]
        if len(train) < MIN_TRAIN[pair] or len(set(ys)) < 2:
            fold["skipped"] = "insufficient_train"
            res["folds"].append(fold)
            continue
        mu, sd = standardizer([[s["x"][f] for f in FEATURES] for s in train])
        X = [zrow([s["x"][f] for f in FEATURES], mu, sd) for s in train]
        b0, w = fit_logistic(X, ys)

        def prob(s):
            return sigmoid(b0 + sum(wj * xj for wj, xj in
                                    zip(w, zrow([s["x"][f] for f in FEATURES],
                                                mu, sd))))

        train_p = sorted(prob(s) for s in train)
        thr_hi, thr_lo = sim.pct(train_p, 0.75), sim.pct(train_p, 0.25)
        fold["thr_p75"], fold["thr_p25"] = round(thr_hi, 4), round(thr_lo, 4)

        pos = [prob(s) for s in test if s["label"] == "reversal"]
        neg = [prob(s) for s in test if s["label"] == "fake"]
        auc = roc_auc(pos, neg)
        fold["auc"] = round(auc, 4) if auc is not None else None
        auc_pool += [(p, 1) for p in pos] + [(p, 0) for p in neg]

        # gated P&L on ALL year-Y setups (label not needed to trade)
        lo, hi = cut, year_ts(y + 1) - 1
        pools = {"ungated": setups,
                 "gated": [s for s in setups if prob(s) >= thr_hi],
                 "inverse": [s for s in setups if prob(s) <= thr_lo]}
        fold["pnl"] = {}
        for arm, pool in pools.items():
            tr = sim.simulate(candles, {}, pool, 1, None, None, True,
                              lo_ts=lo, hi_ts=hi)
            fold["pnl"][arm] = sim.stats(tr)
            trades[arm] += tr
        trades["gated_close_stop"] += simulate_variant(
            candles, pools["gated"], "close_stop", lo_ts=lo, hi_ts=hi)
        trades["gated_flip"] += simulate_variant(
            candles, pools["gated"], "flip", scores=scores, lo_ts=lo, hi_ts=hi)
        res["folds"].append(fold)
        res["final_fold_model"] = {
            "year": y, "train_n": len(train), "intercept": round(b0, 4),
            "weights_std_space": {f: round(wj, 4) for f, wj in zip(FEATURES, w)},
            "feature_means": {f: round(m, 4) for f, m in zip(FEATURES, mu)},
            "feature_stds": {f: round(s_, 4) for f, s_ in zip(FEATURES, sd)}}

    pos = [p for p, l in auc_pool if l == 1]
    neg = [p for p, l in auc_pool if l == 0]
    pooled_auc = roc_auc(pos, neg)
    res["pooled"]["auc"] = round(pooled_auc, 4) if pooled_auc is not None else None
    res["pooled"]["auc_n_events"] = len(auc_pool)
    for arm, tr in trades.items():
        res["pooled"][arm] = sim.stats(tr)
    res["_auc_pool"] = auc_pool          # stripped before writing
    res["_ret_sums"] = {arm: (len(tr), sum(t["ret"] for t in tr))
                        for arm, tr in trades.items()}
    return res


def main() -> int:
    cfg = load_config(None)
    candles_by_pair = {p: candles_from_sqlite(DB, p, 24) for p in ASSETS}
    low_days_by_pair = {p: fresh_low_days(c) for p, c in candles_by_pair.items()}

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "design": {
                  "registration": "heartbeat/evidence/ABI_FUNNEL_2026-07-18.md §6 S3",
                  "bars": "daily (resampled from 1h sqlite)",
                  "features_frozen": FEATURES,
                  "feature_checkpoint": "bounce-confirm bar (breadth at setup date)",
                  "range_atr_denominator": "setup robust ATR14 from causal_setups",
                  "auc_note": "events scored once from bounce-bar features; "
                              "'bounce+3' in the registration names the heartbeat "
                              "checkpoint this single frozen checkpoint replaces",
                  "logistic": {"l2_on_weights": L2, "intercept": "unpenalized",
                               "lr": LR, "iters": ITERS, "init": "zeros"},
                  "min_train_events": MIN_TRAIN,
                  "folds": "expanding yearly, train = events resolved (close of "
                           "resolution bar) before Jan 1 of Y, test = year-Y "
                           "bounce bars, Y=2016..2026",
                  "pnl": "entry b1 close, exit tgt 3.3*ATR / touch-stop at L0 "
                         "(fill min(close,L0)) / 200-bar horizon, 26 bps/side",
                  "gate": "prob >= p75 of TRAIN predicted probs; inverse <= p25"},
              "fee_per_side": sim.FEE,
              "assets": {}, "verdict": {}, "caveats": [
                  "ZEC/USD 1h history has a hard gap 2026-01-01..2026-04-20; "
                  "the ZEC 2026 fold spans it (ATR/returns distorted at the "
                  "boundary bar).",
                  "BTC pre-2016 daily training history is thin; BTC folds run "
                  "with >=10 resolved training events (stated, not swept).",
                  "Yearly P&L folds simulate independently; a trade entered in "
                  "late year Y holds past Jan 1 (same convention as "
                  "bounce_geometry_study).",
                  "POWER: gated arms hold ~19-30 trades per asset over 7-11 "
                  "test years (~2-4/yr). The registered criteria carry no "
                  "trade-count floor, so the verdict is computed as "
                  "registered, but per the bakeoff skill this sample is "
                  "thin — treat any PROMOTE as provisional pending a "
                  "shadow/paper confirmation window.",
                  "Arm pools sequence one-position-at-a-time; a filtered "
                  "pool can therefore log MORE trades than its superset in "
                  "a year (skipped entries free the slot) — harness "
                  "convention, not a bug."]}

    overall_pool = []
    ret_sums = {}
    for pair in ASSETS:
        r = run_asset(pair, cfg, low_days_by_pair, candles_by_pair)
        overall_pool += r.pop("_auc_pool")
        ret_sums[pair] = r.pop("_ret_sums")
        report["assets"][pair] = r
        print(f"\n== {pair}: {r['n_setups']} setups ({r['n_labeled']} labeled), "
              f"pooled AUC {r['pooled']['auc']} on {r['pooled']['auc_n_events']} events")
        for f in r["folds"]:
            if "skipped" in f:
                print(f"  {f['year']}: skipped ({f['train_n']} train)")
            else:
                g, u, i = (f["pnl"][a] for a in ("gated", "ungated", "inverse"))
                print(f"  {f['year']}: auc {f['auc']}  train {f['train_n']} "
                      f"test {f['test_n']}  gated n={g['n']} "
                      f"tot={g.get('total_ret_pct')}  ungated n={u['n']} "
                      f"tot={u.get('total_ret_pct')}  inverse n={i['n']} "
                      f"tot={i.get('total_ret_pct')}")
        for arm in ("ungated", "gated", "inverse", "gated_close_stop", "gated_flip"):
            print(f"  pooled {arm:>18}: {r['pooled'][arm]}")

    pos = [p for p, l in overall_pool if l == 1]
    neg = [p for p, l in overall_pool if l == 0]
    oa = roc_auc(pos, neg)
    report["pooled_auc_overall"] = round(oa, 4) if oa is not None else None
    report["pooled_auc_overall_n"] = len(overall_pool)

    # -- verdict against each registered criterion ---------------------------
    def avg(pair, arm):
        s = report["assets"][pair]["pooled"][arm]
        return s.get("avg_ret_pct") if s.get("n") else None

    def pooled_avg(arm):
        n = sum(ret_sums[p][arm][0] for p in ASSETS)
        tot = sum(ret_sums[p][arm][1] for p in ASSETS)
        return round(tot / n * 100, 3) if n else None

    gated_pos = {p: (avg(p, "gated") is not None and avg(p, "gated") > 0)
                 for p in ASSETS}
    inv_worse = {p: (avg(p, "inverse") is not None and avg(p, "ungated") is not None
                     and avg(p, "inverse") < avg(p, "ungated")) for p in ASSETS}
    c1 = report["pooled_auc_overall"] is not None and report["pooled_auc_overall"] >= 0.60
    c2 = sum(gated_pos.values()) >= 2
    # criterion 3 aggregation: the registration gives a per-asset carve-out
    # only for c2, so the primary reading is the pooled cross-asset
    # comparison; the majority and strict per-asset readings are recorded
    # with equal prominence because they do not all agree.
    inv_pooled, ung_pooled = pooled_avg("inverse"), pooled_avg("ungated")
    c3_pooled = (inv_pooled is not None and ung_pooled is not None
                 and inv_pooled < ung_pooled)
    c3_majority = sum(inv_worse.values()) >= 2
    c3_strict = all(inv_worse.values())
    c3 = c3_pooled
    report["verdict"] = {
        "criterion_1_pooled_auc": {"value": report["pooled_auc_overall"],
                                   "threshold": ">= 0.60", "pass": c1,
                                   "per_asset": {p: report["assets"][p]["pooled"]["auc"]
                                                 for p in ASSETS}},
        "criterion_2_gated_expectancy_pos": {
            "value": {p: avg(p, "gated") for p in ASSETS},
            "threshold": "> 0 %/trade net of fees on >= 2 of 3 assets",
            "per_asset_pass": gated_pos, "pass": c2},
        "criterion_3_inverse_worse_than_ungated": {
            "value": {p: {"inverse": avg(p, "inverse"), "ungated": avg(p, "ungated")}
                      for p in ASSETS},
            "pooled_cross_asset": {"inverse_avg_pct": inv_pooled,
                                   "ungated_avg_pct": ung_pooled},
            "threshold": "inverse avg/trade < ungated avg/trade",
            "per_asset_pass": inv_worse,
            "pass_pooled_reading": c3_pooled,
            "pass_majority_reading": c3_majority,
            "pass_strict_all_assets_reading": c3_strict,
            "primary_reading": "pooled", "pass": c3},
        "PROMOTE": bool(c1 and c2 and c3),
        "PROMOTE_under_strict_c3_reading": bool(c1 and c2 and c3_strict)}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(f"\noverall pooled AUC: {report['pooled_auc_overall']} "
          f"({len(overall_pool)} events)")
    print(f"VERDICT: {'PROMOTE' if report['verdict']['PROMOTE'] else 'FAIL'} "
          f"(c1={c1} c2={c2} c3={c3})")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
