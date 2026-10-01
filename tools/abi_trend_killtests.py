"""ABI kill-tests for the trend / execution frames (2026-10-01 cycle).

  --k3  Independent trend replication on a different BTC price source
        (monthly BTCUSD 2012-2024 bundled with the `backtesting` PyPI
        package). Registration: research/data/abi/trend_independent_REGISTRATION.md
  --k3b Same source: SMA10 timing x vol target vs vol-targeted buy-and-hold,
        the sleeve gate's criteria. Registration:
        research/data/abi/trend_voltarget_monthly_REGISTRATION.md
  --k2  Post-only fill / adverse-selection measurement on the real 1h OHLC
        in the s3bounce parity fixtures. Registration:
        research/data/abi/postonly_fill_REGISTRATION.md

Stdlib only, except --k3 reads the CSV that ships inside the `backtesting`
package (pip install backtesting==0.6.6); it is not vendored here.

Usage:
    python tools/abi_trend_killtests.py --k2 --k3
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "s3bounce" / "tests" / "fixtures"
OUT_DIR = ROOT / "research" / "data" / "abi"
FEE_PER_SIDE = 0.0026


# ── K3: independent monthly trend replication ───────────────────────────

def _load_monthly_btc() -> List[Tuple[str, float]]:
    try:
        import backtesting  # noqa: F401
        from importlib.resources import files
        raw = files("backtesting").joinpath("test/BTCUSD.csv").read_text()
    except Exception as e:
        raise SystemExit("K3 needs the `backtesting` package for its bundled "
                         f"BTCUSD.csv (pip install backtesting==0.6.6): {e}")
    rows = list(csv.reader(io.StringIO(raw)))
    out = []
    for r in rows[1:]:
        if len(r) >= 5 and r[4]:
            out.append((r[0], float(r[4])))
    return out


def _stats(rets: List[float], periods_per_year: float) -> Dict[str, float]:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1.0 + r
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(1, n - 1)
    sharpe = (mean / math.sqrt(var)) * math.sqrt(periods_per_year) if var > 0 else 0.0
    years = n / periods_per_year
    cagr = (eq ** (1.0 / years) - 1.0) * 100.0 if eq > 0 else -100.0
    return {"total_pct": round((eq - 1.0) * 100.0, 1), "cagr_pct": round(cagr, 2),
            "sharpe": round(sharpe, 3), "max_dd_pct": round(mdd * 100.0, 1),
            "months": n}


def _run_flags(closes: List[float], flags: List[bool]) -> List[float]:
    """flags[t] decided at close t is held over month t+1; fees per switch."""
    rets, pos = [], False
    for t in range(len(closes) - 1):
        want = flags[t]
        r = 0.0
        if want != pos:
            r -= FEE_PER_SIDE
        if want:
            r += closes[t + 1] / closes[t] - 1.0
        rets.append(r)
        pos = want
    return rets


def _ols2(y: List[float], x: List[float]) -> Tuple[float, float, float]:
    """y = b0 + b1 x + b2 x^2 by normal equations (3x3, Cramer's rule)."""
    n = len(y)
    s = [sum(xi ** k for xi in x) for k in range(5)]
    t = [sum(yi * xi ** k for yi, xi in zip(y, x)) for k in range(3)]
    a = [[n, s[1], s[2]], [s[1], s[2], s[3]], [s[2], s[3], s[4]]]

    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    d = det(a)
    coefs = []
    for c in range(3):
        m = [row[:] for row in a]
        for r in range(3):
            m[r][c] = t[r]
        coefs.append(det(m) / d)
    return coefs[0], coefs[1], coefs[2]


def k3() -> dict:
    series = _load_monthly_btc()
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    warm = 12
    sma10 = [None] * len(closes)
    for t in range(9, len(closes)):
        sma10[t] = sum(closes[t - 9: t + 1]) / 10.0
    flags_sma = [sma10[t] is not None and closes[t] > sma10[t] for t in range(len(closes))]
    flags_inv = [sma10[t] is not None and closes[t] <= sma10[t] for t in range(len(closes))]
    flags_ts = [t >= 12 and closes[t] > closes[t - 12] for t in range(len(closes))]
    c = closes[warm:]
    arms = {
        "buy_and_hold": _run_flags(c, [True] * len(c)),
        "sma10": _run_flags(c, flags_sma[warm:]),
        "tsmom12": _run_flags(c, flags_ts[warm:]),
        "inverse_sma10": _run_flags(c, flags_inv[warm:]),
    }
    stats = {k: _stats(v, 12.0) for k, v in arms.items()}
    bh = stats["buy_and_hold"]
    crit = {
        "c1_trend_sharpe_gt_bh": stats["sma10"]["sharpe"] > bh["sharpe"]
        and stats["tsmom12"]["sharpe"] > bh["sharpe"],
        "c2_trend_maxdd_lt_bh": stats["sma10"]["max_dd_pct"] < bh["max_dd_pct"]
        and stats["tsmom12"]["max_dd_pct"] < bh["max_dd_pct"],
        "c3_inverse_sharpe_lt_bh": stats["inverse_sma10"]["sharpe"] < bh["sharpe"],
    }
    under = arms["buy_and_hold"]
    b0, b1, b2 = _ols2(arms["sma10"], under)
    # Sub-period split (first half / second half) — stability readout only.
    half = len(c) // 2
    split = {}
    for name, sl in (("first_half", slice(0, half)), ("second_half", slice(half, None))):
        split[name] = {k: _stats(v[sl], 12.0) for k, v in arms.items()}
    return {
        "registration": "research/data/abi/trend_independent_REGISTRATION.md",
        "source": "backtesting==0.6.6 test/BTCUSD.csv (monthly)",
        "window": [dates[warm], dates[-1]],
        "fee_per_side": FEE_PER_SIDE,
        "stats": stats,
        "subperiods": split,
        "time_in_market": {
            "sma10": round(sum(flags_sma[warm:-1]) / (len(c) - 1), 3),
            "tsmom12": round(sum(flags_ts[warm:-1]) / (len(c) - 1), 3),
        },
        "convexity_ols_sma10_on_underlying": {"b0": round(b0, 5), "b1": round(b1, 4),
                                              "b2": round(b2, 4)},
        "criteria": crit,
        "verdict": "SURVIVES" if all(crit.values()) else "KILLED",
    }


# ── K3b: timing vs vol-targeted buy-and-hold (monthly, independent) ────

K3B_TARGET_VOL = 0.30
K3B_BASE_COST = (25.0 + 10.0) / 10_000.0
K3B_STRESS_COST = (40.0 + 10.0) / 10_000.0
K3B_ARMS = ("bh", "bh_vt", "sma10", "sma10_vt", "tsmom12_vt", "inverse_vt")


def _k3b_targets(closes: List[float], t: int) -> Dict[str, float]:
    """Exposure for month t+1, decided on the close of month t."""
    rets = [closes[k] / closes[k - 1] - 1.0 for k in range(t - 5, t + 1)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(12.0)
    vm = 1.0 if vol <= 0 else max(0.2, min(1.0, K3B_TARGET_VOL / vol))
    above = closes[t] > sum(closes[t - 9: t + 1]) / 10.0
    return {"bh": 1.0, "bh_vt": vm, "sma10": 1.0 if above else 0.0,
            "sma10_vt": vm if above else 0.0,
            "tsmom12_vt": vm if closes[t] > closes[t - 12] else 0.0,
            "inverse_vt": 0.0 if above else vm}


def _k3b_run(closes: List[float], warm: int, arm: str, cost: float,
             cap: float = 1.0, breaker_pct: Optional[float] = None) -> List[float]:
    """Monthly returns of one arm. `cap` scales every target (Hydra's
    max_position_pct); `breaker_pct` sells and stays flat once equity is
    that far below its peak at a month-end (Hydra's sticky breaker)."""
    equity, drifted, out = 1.0, 0.0, []
    peak, halted = 1.0, False
    for t in range(warm, len(closes) - 1):
        target = 0.0 if halted else cap * _k3b_targets(closes, t)[arm]
        start = equity
        equity -= abs(target - drifted) * equity * cost
        r = closes[t + 1] / closes[t] - 1.0
        growth = 1.0 + target * r
        equity *= growth
        drifted = target * (1.0 + r) / growth if growth > 0 else 0.0
        peak = max(peak, equity)
        if (breaker_pct is not None and not halted
                and (peak - equity) / peak * 100.0 >= breaker_pct):
            halted = True
        out.append(equity / start - 1.0)
    return out


def _sharpe_m(rets: List[float]) -> float:
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(1, n - 1)
    return (mean / math.sqrt(var)) * math.sqrt(12.0) if var > 0 else 0.0


def _paired_boot(a: List[float], b: List[float], n_boot: int = 2000,
                 seed: int = 7) -> Dict[str, float]:
    import random
    n = len(a)
    block = max(1, int(round(n ** (1.0 / 3.0))))
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        idx: List[int] = []
        while len(idx) < n:
            s = rng.randrange(n)
            idx.extend((s + k) % n for k in range(block))
        del idx[n:]
        diffs.append(_sharpe_m([a[j] for j in idx]) - _sharpe_m([b[j] for j in idx]))
    diffs.sort()
    return {"p05": round(diffs[int(round(0.05 * (n_boot - 1)))], 4),
            "p95": round(diffs[int(round(0.95 * (n_boot - 1)))], 4),
            "block": block, "n_boot": n_boot}


def k3b() -> dict:
    series = _load_monthly_btc()
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    warm = 12
    base = {a: _k3b_run(closes, warm, a, K3B_BASE_COST) for a in K3B_ARMS}
    stress = {a: _k3b_run(closes, warm, a, K3B_STRESS_COST) for a in ("bh_vt", "sma10_vt")}
    stats = {a: _stats(r, 12.0) for a, r in base.items()}
    k = len(base["bh"]) // 3
    cuts = (slice(0, k), slice(k, 2 * k), slice(2 * k, None))
    thirds = {a: [_stats(base[a][c], 12.0) for c in cuts] for a in K3B_ARMS}
    d, b = stats["sma10_vt"], stats["bh_vt"]
    wins = sum(1 for td, tb in zip(thirds["sma10_vt"], thirds["bh_vt"])
               if td["sharpe"] > tb["sharpe"])
    crit = {
        "C1_sharpe_gt_voltarget_bh": _sharpe_m(base["sma10_vt"]) > _sharpe_m(base["bh_vt"]),
        "C2_wins_2_of_3_thirds": wins >= 2,
        "C3_maxdd_le_0.75x_voltarget_bh": d["max_dd_pct"] <= 0.75 * b["max_dd_pct"],
        "C4_inverse_sharpe_lt_voltarget_bh": _sharpe_m(base["inverse_vt"]) < _sharpe_m(base["bh_vt"]),
        "C5_sharpe_gt_voltarget_bh_at_stress":
            _sharpe_m(stress["sma10_vt"]) > _sharpe_m(stress["bh_vt"]),
    }
    boot = _paired_boot(base["sma10_vt"], base["bh_vt"])
    verdict = "SURVIVES" if all(crit.values()) else "KILLED"
    # Not registered, not gated: what the arms look like inside Hydra's
    # risk frame (competition cap 0.40; sticky 15% breaker at month-ends,
    # which understates intra-month trips).
    post_hoc = {}
    for arm in ("bh_vt", "sma10_vt"):
        for label, kw in (("cap40", {"cap": 0.40}),
                          ("cap40_breaker15", {"cap": 0.40, "breaker_pct": 15.0})):
            post_hoc[f"{arm}_{label}"] = _stats(
                _k3b_run(closes, warm, arm, K3B_BASE_COST, **kw), 12.0)
    return {
        "registration": "research/data/abi/trend_voltarget_monthly_REGISTRATION.md",
        "source": "backtesting==0.6.6 test/BTCUSD.csv (monthly)",
        "window": [dates[warm + 1], dates[-1]],
        "months": len(base["bh"]),
        "costs_per_side": {"base": K3B_BASE_COST, "stress": K3B_STRESS_COST},
        "stats": stats,
        "thirds": thirds,
        "stress": {a: _stats(r, 12.0) for a, r in stress.items()},
        "time_in_market": {a: round(sum(1 for t in range(warm, len(closes) - 1)
                                        if _k3b_targets(closes, t)[a] > 0) / len(base[a]), 3)
                           for a in ("sma10_vt", "tsmom12_vt", "inverse_vt")},
        "avg_exposure": {a: round(sum(_k3b_targets(closes, t)[a]
                                      for t in range(warm, len(closes) - 1)) / len(base[a]), 3)
                         for a in K3B_ARMS},
        "bootstrap_sharpe_diff_sma10vt_minus_bhvt": boot,
        "criteria": crit,
        "verdict": verdict,
        "significant": verdict == "SURVIVES" and boot["p05"] > 0,
        "post_hoc_readouts_not_gated": post_hoc,
    }


# ── K2: post-only fill / adverse selection ─────────────────────────────

def _hourly(pair: str) -> List[dict]:
    d = json.loads((FIXTURES / f"parity_{pair}.json").read_text())
    return sorted(d["hourly_sample"], key=lambda b: b["ts"])


def _fill_within(bars, t, k, side) -> bool:
    px = bars[t]["close"]
    for j in range(t + 1, min(len(bars), t + 1 + k)):
        if side == "BUY" and bars[j]["low"] < px:
            return True
        if side == "SELL" and bars[j]["high"] > px:
            return True
    return False


def k2() -> dict:
    out = {"registration": "research/data/abi/postonly_fill_REGISTRATION.md",
           "fill_rule": "through-trade only (BUY low<c_t, SELL high>c_t)",
           "per_pair": {}}
    pooled = {"SELL_down": {"n": 0, "f1": 0, "f4": 0, "f24": 0, "unfilled_fwd": [],
                            "filled_fwd": []},
              "BUY_up": {"n": 0, "f1": 0, "f4": 0, "f24": 0, "unfilled_fwd": [],
                         "filled_fwd": []}}
    for pair in ("BTC_USD", "ETH_USD", "ZEC_USD"):
        bars = _hourly(pair)
        loc = {"SELL_down": {"n": 0, "f1": 0}, "BUY_up": {"n": 0, "f1": 0}}
        for t in range(24, len(bars) - 24):
            prior = sum(b["close"] for b in bars[t - 24: t]) / 24.0
            c = bars[t]["close"]
            fwd = bars[t + 24]["close"] / c - 1.0
            for key, side, cond in (("SELL_down", "SELL", c < prior),
                                    ("BUY_up", "BUY", c > prior)):
                if not cond:
                    continue
                f1 = _fill_within(bars, t, 1, side)
                p = pooled[key]
                p["n"] += 1
                p["f1"] += f1
                p["f4"] += _fill_within(bars, t, 4, side)
                p["f24"] += _fill_within(bars, t, 24, side)
                (p["filled_fwd"] if f1 else p["unfilled_fwd"]).append(fwd)
                loc[key]["n"] += 1
                loc[key]["f1"] += f1
        out["per_pair"][pair] = {k: {"n": v["n"],
                                     "p_fill_1bar": round(v["f1"] / v["n"], 3) if v["n"] else None}
                                 for k, v in loc.items()}

    def summ(p):
        n = p["n"]
        mean = (lambda xs: round(sum(xs) / len(xs) * 100.0, 3) if xs else None)
        return {"n": n,
                "p_fill_1bar": round(p["f1"] / n, 3),
                "p_fill_4bars": round(p["f4"] / n, 3),
                "p_fill_24bars": round(p["f24"] / n, 3),
                "fwd24_mean_pct_if_filled_1bar": mean(p["filled_fwd"]),
                "fwd24_mean_pct_if_unfilled_1bar": mean(p["unfilled_fwd"]),
                "n_unfilled_1bar": len(p["unfilled_fwd"])}

    s, b = summ(pooled["SELL_down"]), summ(pooled["BUY_up"])
    out["pooled"] = {"SELL_in_downtrend": s, "BUY_in_uptrend": b}
    crit = {"c1_sell_fill_1bar_lt_0.80": s["p_fill_1bar"] < 0.80,
            "c2_unfilled_sell_fwd24_negative": (s["fwd24_mean_pct_if_unfilled_1bar"] or 0.0) < 0.0}
    out["criteria"] = crit
    out["verdict"] = "SURVIVES" if all(crit.values()) else "KILLED"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="ABI 2026-10-01 trend/execution kill-tests")
    ap.add_argument("--k2", action="store_true")
    ap.add_argument("--k3", action="store_true")
    ap.add_argument("--k3b", action="store_true")
    args = ap.parse_args()
    if not (args.k2 or args.k3 or args.k3b):
        ap.error("pick --k2, --k3 and/or --k3b")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.k3:
        r = k3()
        (OUT_DIR / "trend_independent_killtest.json").write_text(json.dumps(r, indent=2) + "\n")
        print("K3", r["verdict"], json.dumps(r["stats"]), json.dumps(r["criteria"]))
    if args.k3b:
        r = k3b()
        (OUT_DIR / "trend_voltarget_monthly_killtest.json").write_text(
            json.dumps(r, indent=2) + "\n")
        print("K3b", r["verdict"], json.dumps({k: v["sharpe"] for k, v in r["stats"].items()}),
              json.dumps(r["criteria"]), json.dumps(r["bootstrap_sharpe_diff_sma10vt_minus_bhvt"]))
    if args.k2:
        r = k2()
        (OUT_DIR / "postonly_fill_killtest.json").write_text(json.dumps(r, indent=2) + "\n")
        print("K2", r["verdict"], json.dumps(r["pooled"]), json.dumps(r["criteria"]))


if __name__ == "__main__":
    sys.exit(main())
