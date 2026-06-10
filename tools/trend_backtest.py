"""Flywheel Phase 0 — validate slow trend-following on REAL daily data.

Candidate directional sleeve: long-or-flat time-series momentum on a daily
timeframe (resampled from the canonical 60m store), volatility-targeted
sizing, friction-aware (16 bps per side). While in cash, capital earns a
parking yield (Kraken USD/USDC rewards ~4% APY, configurable).

Variants tested per pair:
    sma200      long when close > 200d SMA
    sma100      long when close > 100d SMA
    don55       long on 55d-high breakout, exit on 20d-low (turtle-style)
    ema20x100   long when EMA20 > EMA100
Each variant runs with and without vol targeting (target 30% annualized,
sized = min(1, target/realized30d), i.e. never levered).

Usage: python tools/trend_backtest.py
Output: stdout table + .hydra-flywheel/trend_results.json
"""
import json
import math
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = str(ROOT / "hydra_history.sqlite")
OUT_DIR = ROOT / ".hydra-flywheel"

FEE_BPS_PER_SIDE = 16.0
CASH_APY_PCT = 4.0          # USD/USDC parking yield while flat
VOL_TARGET = 0.30           # 30% annualized
VOL_LOOKBACK = 30           # days


def daily_closes(pair: str):
    """Resample 60m closes to daily (last close of each UTC day)."""
    db = sqlite3.connect(DB)
    rows = db.execute(
        "select ts, close from ohlc where pair=? and grain_sec=3600 order by ts",
        (pair,)).fetchall()
    db.close()
    days = {}
    for ts, close in rows:
        days[ts // 86400] = close  # last write per day wins (ordered by ts)
    keys = sorted(days)
    return [days[k] for k in keys]


def sma(xs, n, i):
    if i + 1 < n:
        return None
    return sum(xs[i + 1 - n:i + 1]) / n


def ema_series(xs, n):
    out, k = [], 2.0 / (n + 1)
    e = xs[0]
    for x in xs:
        e += k * (x - e)
        out.append(e)
    return out


def realized_vol(closes, i, lookback=VOL_LOOKBACK):
    if i < lookback:
        return None
    rets = [math.log(closes[j] / closes[j - 1])
            for j in range(i - lookback + 1, i + 1)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.0)


def signal_stream(closes, variant):
    """Yield desired exposure flag (True=long, False=flat) per day index."""
    n = len(closes)
    if variant in ("sma200", "sma100"):
        period = 200 if variant == "sma200" else 100
        return [(sma(closes, period, i) is not None and
                 closes[i] > sma(closes, period, i)) for i in range(n)]
    if variant == "don55":
        flags, in_pos = [], False
        for i in range(n):
            if i >= 55:
                hi55 = max(closes[i - 55:i])
                lo20 = min(closes[i - 20:i])
                if not in_pos and closes[i] > hi55:
                    in_pos = True
                elif in_pos and closes[i] < lo20:
                    in_pos = False
            flags.append(in_pos)
        return flags
    if variant == "ema20x100":
        e20, e100 = ema_series(closes, 20), ema_series(closes, 100)
        return [i >= 100 and e20[i] > e100[i] for i in range(n)]
    raise ValueError(variant)


def run(closes, variant, vol_target_on):
    flags = signal_stream(closes, variant)
    fee = FEE_BPS_PER_SIDE / 10_000.0
    cash_daily = (1.0 + CASH_APY_PCT / 100.0) ** (1.0 / 365.0) - 1.0
    equity, peak, max_dd = 1.0, 1.0, 0.0
    exposure = 0.0
    switches = 0
    daily_rets = []
    for i in range(1, len(closes)):
        want = 0.0
        if flags[i - 1]:                      # decide on yesterday's signal
            want = 1.0
            if vol_target_on:
                rv = realized_vol(closes, i - 1)
                if rv and rv > 0:
                    want = min(1.0, VOL_TARGET / rv)
        ret = closes[i] / closes[i - 1] - 1.0
        day_pnl = exposure * ret + (1.0 - exposure) * cash_daily
        turn = abs(want - exposure)
        if turn > 0.01:
            day_pnl -= turn * fee
            switches += 1
        equity *= (1.0 + day_pnl)
        daily_rets.append(day_pnl)
        exposure = want
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    years = len(daily_rets) / 365.0
    cagr = (equity ** (1.0 / years) - 1.0) * 100.0 if equity > 0 else -100.0
    mean = sum(daily_rets) / len(daily_rets)
    var = sum((r - mean) ** 2 for r in daily_rets) / max(1, len(daily_rets) - 1)
    sharpe = (mean / math.sqrt(var)) * math.sqrt(365.0) if var > 0 else 0.0
    return {"variant": variant, "vol_target": vol_target_on,
            "total_pct": round((equity - 1.0) * 100.0, 1),
            "cagr_pct": round(cagr, 2), "sharpe": round(sharpe, 2),
            "max_dd_pct": round(max_dd * 100.0, 1),
            "switches_per_year": round(switches / years, 1),
            "years": round(years, 2)}


def bench(closes):
    years = (len(closes) - 1) / 365.0
    total = closes[-1] / closes[0] - 1.0
    cagr = ((closes[-1] / closes[0]) ** (1.0 / years) - 1.0) * 100.0
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    sharpe = (mean / math.sqrt(var)) * math.sqrt(365.0) if var > 0 else 0.0
    peak, max_dd, eq = closes[0], 0.0, closes[0]
    for c in closes:
        peak = max(peak, c)
        max_dd = max(max_dd, (peak - c) / peak)
    return {"total_pct": round(total * 100.0, 1), "cagr_pct": round(cagr, 2),
            "sharpe": round(sharpe, 2), "max_dd_pct": round(max_dd * 100.0, 1),
            "years": round(years, 2)}


def main():
    results = {}
    for pair in ("BTC/USD", "SOL/USD"):
        closes = daily_closes(pair)
        # skip the earliest illiquid era for BTC (pre-2015 mtgox-era data)
        if pair == "BTC/USD":
            closes = closes[-(365 * 11):]
        b = bench(closes)
        runs = []
        for variant in ("sma200", "sma100", "don55", "ema20x100"):
            for vt in (False, True):
                runs.append(run(closes, variant, vt))
        results[pair] = {"days": len(closes), "buy_and_hold": b, "runs": runs}
        print(f"\n=== {pair} ({len(closes)} days, {b['years']}y) ===")
        print(f"  B&H: total {b['total_pct']:+.1f}%  CAGR {b['cagr_pct']:+.2f}%  "
              f"sharpe {b['sharpe']:.2f}  maxDD {b['max_dd_pct']:.1f}%")
        print(f"  {'variant':>10} {'volT':>5} {'total%':>10} {'CAGR%':>8} "
              f"{'sharpe':>7} {'maxDD%':>7} {'sw/yr':>6}")
        for r in runs:
            print(f"  {r['variant']:>10} {str(r['vol_target']):>5} "
                  f"{r['total_pct']:>10.1f} {r['cagr_pct']:>8.2f} "
                  f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>7.1f} "
                  f"{r['switches_per_year']:>6.1f}")

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "trend_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
