"""Pre-registered evidence gate for the daily trend sleeve (HYDRA_TREND_SLEEVE).

Registration (fixed before any real-data run):
    research/data/trend_sleeve_REGISTRATION.md
Output:
    research/data/trend_sleeve_gate.json

Daily arms A-E run on completed daily closes. The sleeve's score and vol
multiplier come from a real HydraEngine fed two bars per UTC day (an opening
print that completes the previous day, then the day's close), so the
ensemble, the Donchian state machine, the 420-day close window and the
21-day vol multiplier are the engine's own code, not a re-implementation.

--engine adds arms F/G: BacktestRunner over the same window on the 1h tape,
the current engine against HYDRA_TREND_SLEEVE=1 (same fills, same fees).

Usage:
    python tools/trend_sleeve_gate.py
    python tools/trend_sleeve_gate.py --engine
    python tools/trend_sleeve_gate.py --calibrate 40   # PASS rate under nulls
    python tools/trend_sleeve_gate.py --db hydra_history.sqlite --pairs BTC/USD,ETH/USD
    python tools/trend_sleeve_gate.py --csv BTC/USD=btc_daily.csv

Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine, SIZING_COMPETITION, trend_target_vol_setting  # noqa: E402

REGISTRATION = "research/data/trend_sleeve_REGISTRATION.md"
DEFAULT_OUT = ROOT / "research" / "data" / "trend_sleeve_gate.json"
DEFAULT_CALIBRATION_OUT = ROOT / "research" / "data" / "trend_sleeve_gate_calibration.json"
DEFAULT_DB = ROOT / "hydra_history.sqlite"
DEFAULT_PAIRS = ("BTC/USD", "ETH/USD", "ZEC/USD")

DAY = 86400
CAP = float(SIZING_COMPETITION["max_position_pct"])
LONG_AT = HydraEngine.TREND_SCORE_LONG
BASE_COST = (25.0 + 10.0) / 10_000.0     # fee + slippage per side
STRESS_COST = (40.0 + 10.0) / 10_000.0
SENSITIVITY_CASH_APY = 0.04
REBALANCE_DAYS = 30
MIN_EVAL_YEARS = 5.0
DD_RATIO = 0.75
BOOT_N = 2000
BOOT_SEED = 7
ENGINE_BALANCE = 10_000.0
ENGINE_FEE_BPS = 25.0

ARMS = ("bh_cap", "bh_voltarget", "sleeve_novt", "sleeve", "sleeve_entry_only", "inverse")
RESIZE_DAYS = HydraEngine.SLEEVE_RESIZE_DAYS
RESIZE_TOL = HydraEngine.SLEEVE_RESIZE_TOL


# ── data ────────────────────────────────────────────────────────────────

def load_daily_sqlite(db_path: str, pair: str) -> List[Tuple[int, float]]:
    """(utc_day, close) per day from the history store.

    Grain 86400 rows are used as stored; grain 3600 rows are resampled to
    the last close of each UTC day. Whichever covers more days wins.
    """
    best: Dict[int, float] = {}
    con = sqlite3.connect(db_path)
    try:
        for grain in (86400, 3600):
            try:
                rows = con.execute(
                    "SELECT ts, close FROM ohlc WHERE pair=? AND grain_sec=? ORDER BY ts",
                    (pair, grain)).fetchall()
            except sqlite3.Error as e:
                raise SystemExit(f"{db_path}: not a Hydra history store ({e})")
            days: Dict[int, float] = {}
            for ts, close in rows:
                if close is not None and float(close) > 0:
                    days[int(ts) // DAY] = float(close)
            if len(days) > len(best):
                best = days
    finally:
        con.close()
    return sorted(best.items())


def has_hourly(db_path: str, pair: str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT COUNT(*) FROM ohlc WHERE pair=? AND grain_sec=3600",
                          (pair,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return bool(row and row[0])


def _parse_time(raw: str) -> float:
    text = raw.strip()
    try:
        ts = float(text)
    except ValueError:
        day = datetime.fromisoformat(text[:10])
        return day.replace(tzinfo=timezone.utc).timestamp()
    return ts / 1000.0 if ts > 1e11 else ts  # milliseconds


def load_daily_csv(path: str) -> List[Tuple[int, float]]:
    """A date or unix-time column plus a close column, any vendor."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = {name.lower().strip(): name for name in (reader.fieldnames or [])}
        tcol = next((cols[k] for k in ("timestamp", "ts", "time", "date", "datetime",
                                        "open_time") if k in cols), None)
        ccol = next((cols[k] for k in ("close", "adj close", "adj_close", "price")
                     if k in cols), None)
        if tcol is None or ccol is None:
            raise SystemExit(f"{path}: need a date/timestamp column and a close column, "
                             f"got {reader.fieldnames}")
        days: Dict[int, float] = {}
        skipped = 0
        for row in reader:
            try:
                ts = _parse_time(row[tcol])
                close = float(row[ccol])
            except (TypeError, ValueError):
                skipped += 1
                continue
            if close > 0:
                days[int(ts // DAY)] = close
    if skipped:
        print(f"[gate] {path}: skipped {skipped} unreadable rows", flush=True)
    return sorted(days.items())


# ── the sleeve, from the engine itself ──────────────────────────────────

def _print(ts: float, price: float) -> dict:
    return {"open": price, "high": price, "low": price, "close": price,
            "volume": 0.0, "timestamp": ts}


def sleeve_path(days: Sequence[int], closes: Sequence[float]
                ) -> List[Tuple[Optional[float], float]]:
    """(score, vol multiplier) known at the START of each day i, from
    completed closes through day i-1, computed by a real HydraEngine."""
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", trend_sleeve=True)
    out: List[Tuple[Optional[float], float]] = []
    for i, (day, close) in enumerate(zip(days, closes)):
        eng.ingest_candle(_print(day * DAY, closes[i - 1] if i else close))
        out.append((eng.sleeve_trend_score(), eng._sleeve_vol_multiplier()))
        eng.ingest_candle(_print(day * DAY + 23 * 3600, close))
    return out


# ── arms ────────────────────────────────────────────────────────────────

def simulate(closes: Sequence[float], path: Sequence[Tuple[Optional[float], float]],
             start: int, arm: str, cost: float, cash_apy: float = 0.0,
             cap: float = CAP, breaker_pct: Optional[float] = None,
             days: Optional[Sequence[int]] = None) -> dict:
    """Daily equity returns of one arm over days [start, n).

    Decide on the close of day i-1, trade at that close plus `cost` per
    side on the traded notional, hold over day i. Idle cash earns
    `cash_apy` in every arm alike.

    The sleeve-shaped arms (sleeve, sleeve_novt, inverse) follow the
    engine's re-size rule: SLEEVE_RESIZE_DAYS after the last sizing the
    position is re-set to its target unless already within
    SLEEVE_RESIZE_TOL of it. `sleeve_entry_only` is the original
    registration's construction: sized at entry, never re-sized.

    `breaker_pct` models the engine's sticky circuit breaker: once equity
    is that far below its peak at a close, the arm sells at that close and
    never buys again (the engine needs HYDRA_RESET_CIRCUIT_BREAKER=1).
    """
    if arm not in ARMS:
        raise ValueError(arm)
    day_of = (lambda i: days[i]) if days is not None else (lambda i: i)
    cash, units = 1.0, 0.0
    cash_daily = (1.0 + cash_apy) ** (1.0 / 365.0) - 1.0
    rets: List[float] = []
    exposure: List[float] = []
    entries = 0
    resizes = 0
    prev_equity = 1.0
    peak = 1.0
    halted_at: Optional[int] = None
    sized_day: Optional[int] = None
    for i in range(start, len(closes)):
        score, vm = path[i]
        px = closes[i - 1]
        if halted_at is not None:
            pass
        elif arm in ("bh_cap", "bh_voltarget"):
            if (i - start) % REBALANCE_DAYS == 0:
                frac = cap * (vm if arm == "bh_voltarget" else 1.0)
                target = (cash + units * px) * frac / px
                delta = target - units
                cash -= delta * px + abs(delta) * px * cost
                if units == 0.0 and target > 0:
                    entries += 1
                units = target
        else:
            warm = score is not None
            want = warm and (score < LONG_AT if arm == "inverse" else score >= LONG_AT)
            frac = cap * (1.0 if arm == "sleeve_novt" else vm)
            if want and units == 0.0:
                units = cash * frac / px
                cash -= units * px * (1.0 + cost)
                entries += 1
                sized_day = day_of(i)
            elif not want and units > 0.0:
                cash += units * px * (1.0 - cost)
                units = 0.0
            elif (want and units > 0.0 and arm != "sleeve_entry_only"
                  and (sized_day is None or day_of(i) - sized_day >= RESIZE_DAYS)):
                target_notional = (cash + units * px) * frac
                current = units * px
                if abs(current - target_notional) > RESIZE_TOL * target_notional:
                    delta = target_notional / px - units
                    cash -= delta * px + abs(delta) * px * cost
                    units += delta
                    resizes += 1
                sized_day = day_of(i)
        if cash > 0:
            cash *= 1.0 + cash_daily
        equity = cash + units * closes[i]
        exposure.append(units * closes[i] / equity if equity > 0 else 0.0)
        peak = max(peak, equity)
        if (breaker_pct is not None and halted_at is None and peak > 0
                and (peak - equity) / peak * 100.0 >= breaker_pct):
            halted_at = i
            if units > 0.0:
                cash += units * closes[i] * (1.0 - cost)
                units = 0.0
                equity = cash
        rets.append(equity / prev_equity - 1.0 if prev_equity > 0 else 0.0)
        prev_equity = equity
    n_days = len(rets)
    return {
        "rets": rets,
        "entries": entries,
        "resizes": resizes,
        "time_in_market": (sum(1 for e in exposure if e > 0) / n_days) if n_days else 0.0,
        "avg_exposure": (sum(exposure) / n_days) if n_days else 0.0,
        "max_exposure": max(exposure) if exposure else 0.0,
        "breaker_day_index": halted_at,
    }


def _sharpe(rets: Sequence[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return (mean / math.sqrt(var)) * math.sqrt(365.0) if var > 0 else 0.0


def stats(rets: Sequence[float]) -> dict:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1.0 + r
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
    years = len(rets) / 365.0
    cagr = (eq ** (1.0 / years) - 1.0) if eq > 0 and years > 0 else -1.0
    return {"total_pct": (eq - 1.0) * 100.0, "cagr_pct": cagr * 100.0,
            "sharpe": _sharpe(rets), "max_dd_pct": mdd * 100.0, "days": len(rets)}


def thirds(rets: Sequence[float]) -> List[List[float]]:
    k = len(rets) // 3
    return [list(rets[:k]), list(rets[k:2 * k]), list(rets[2 * k:])]


def paired_block_bootstrap(a: Sequence[float], b: Sequence[float],
                           n_boot: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """5th/95th percentile of Sharpe(a) - Sharpe(b) under a paired circular
    block bootstrap (block = round(n^(1/3)), same indices for both)."""
    n = len(a)
    if n < 2 or len(b) != n:
        return {"p05": None, "p95": None, "block": None, "n_boot": 0}
    block = max(1, int(round(n ** (1.0 / 3.0))))
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        idx: List[int] = []
        while len(idx) < n:
            s = rng.randrange(n)
            idx.extend((s + k) % n for k in range(block))
        del idx[n:]
        diffs.append(_sharpe([a[j] for j in idx]) - _sharpe([b[j] for j in idx]))
    diffs.sort()
    return {"p05": diffs[int(round(0.05 * (n_boot - 1)))],
            "p95": diffs[int(round(0.95 * (n_boot - 1)))],
            "block": block, "n_boot": n_boot}


# ── per-asset verdict ───────────────────────────────────────────────────

def _rounded(d: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def criteria(base: Dict[str, dict], third_stats: Dict[str, List[dict]],
             stress: Dict[str, dict]) -> Dict[str, bool]:
    d, b, e = base["sleeve"], base["bh_voltarget"], base["inverse"]
    wins = sum(1 for td, tb in zip(third_stats["sleeve"], third_stats["bh_voltarget"])
               if td["sharpe"] > tb["sharpe"])
    return {
        "C1_sharpe_gt_voltarget_bh": d["sharpe"] > b["sharpe"],
        "C2_wins_2_of_3_thirds": wins >= 2,
        "C3_maxdd_le_0.75x_voltarget_bh": d["max_dd_pct"] <= DD_RATIO * b["max_dd_pct"],
        "C4_inverse_sharpe_lt_voltarget_bh": e["sharpe"] < b["sharpe"],
        "C5_sharpe_gt_voltarget_bh_at_stress_cost":
            stress["sleeve"]["sharpe"] > stress["bh_voltarget"]["sharpe"],
    }


def evaluate_asset(days: Sequence[int], closes: Sequence[float],
                   boot_n: int = BOOT_N) -> dict:
    if len(closes) < 3:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "fewer than 3 daily closes"}
    path = sleeve_path(days, closes)
    start = next((i for i, (score, _) in enumerate(path) if score is not None and i >= 1),
                 None)
    if start is None:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "the sleeve never warmed up",
                "days": len(closes)}
    runs = {arm: simulate(closes, path, start, arm, BASE_COST, days=days) for arm in ARMS}
    base = {arm: stats(r["rets"]) for arm, r in runs.items()}
    third_stats = {arm: [stats(t) for t in thirds(r["rets"])] for arm, r in runs.items()}
    stress = {arm: stats(simulate(closes, path, start, arm, STRESS_COST, days=days)["rets"])
              for arm in ("sleeve", "bh_voltarget")}
    apy = {arm: stats(simulate(closes, path, start, arm, BASE_COST,
                               cash_apy=SENSITIVITY_CASH_APY, days=days)["rets"])
           for arm in ARMS}
    # Reported, not gated: the same arms under the engine's sticky 15%
    # breaker. The sleeve sizes once and never trims, so a rally grows its
    # exposure and an ordinary pullback can trip the breaker for good.
    breaker = HydraEngine.CIRCUIT_BREAKER_PCT
    with_cb = {arm: simulate(closes, path, start, arm, BASE_COST, breaker_pct=breaker,
                             days=days)
               for arm in ("sleeve", "sleeve_entry_only", "bh_voltarget")}
    crit = criteria(base, third_stats, stress)
    years = (len(closes) - start) / 365.0
    if years < MIN_EVAL_YEARS:
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "PASS" if all(crit.values()) else "FAIL"
    boot = paired_block_bootstrap(runs["sleeve"]["rets"], runs["bh_voltarget"]["rets"],
                                  n_boot=boot_n)
    return {
        "verdict": verdict,
        "window": {"start_day": _iso(days[start]), "end_day": _iso(days[-1]),
                   "start_ts": days[start] * DAY, "end_ts": days[-1] * DAY + 23 * 3600,
                   "years_evaluated": round(years, 2), "warmup_days": start},
        "arms": {arm: {**_rounded(base[arm]), "entries": runs[arm]["entries"],
                       "resizes": runs[arm]["resizes"],
                       "max_exposure": round(runs[arm]["max_exposure"], 4),
                       "entries_per_year": round(runs[arm]["entries"] / years, 2) if years else 0.0,
                       "time_in_market": round(runs[arm]["time_in_market"], 4),
                       "avg_exposure": round(runs[arm]["avg_exposure"], 4)}
                 for arm in ARMS},
        "thirds": {arm: [_rounded(t) for t in third_stats[arm]] for arm in ARMS},
        "stress_cost": {arm: _rounded(s) for arm, s in stress.items()},
        "cash_apy_4pct_sensitivity": {arm: _rounded(s) for arm, s in apy.items()},
        "engine_breaker_diagnostic": {
            arm: {**_rounded(stats(r["rets"])),
                  "breaker_tripped_on": (_iso(days[r["breaker_day_index"]])
                                         if r["breaker_day_index"] is not None else None)}
            for arm, r in with_cb.items()},
        "bootstrap_sharpe_diff_sleeve_minus_voltarget_bh": _rounded(boot),
        "criteria": crit,
    }


def _iso(day: int) -> str:
    return datetime.fromtimestamp(day * DAY, tz=timezone.utc).strftime("%Y-%m-%d")


# ── engine-integrated arms ──────────────────────────────────────────────

def run_engine_arms(db_path: str, pair: str, start_ts: int, end_ts: int) -> dict:
    """F (current engine) and G (HYDRA_TREND_SLEEVE=1) on the 1h tape."""
    from hydra_backtest import BacktestConfig, BacktestRunner
    out: Dict[str, dict] = {}
    for arm, flag in (("engine_off", None), ("engine_sleeve", "1")):
        prev = os.environ.get("HYDRA_TREND_SLEEVE")
        try:
            if flag is None:
                os.environ.pop("HYDRA_TREND_SLEEVE", None)
            else:
                os.environ["HYDRA_TREND_SLEEVE"] = flag
            cfg = BacktestConfig(
                name=f"trend_sleeve_gate:{arm}:{pair}", pairs=(pair,),
                initial_balance_per_pair=ENGINE_BALANCE, mode="competition",
                coordinator_enabled=False, data_source="sqlite",
                data_source_params_json=json.dumps({
                    "db_path": db_path, "grain_sec": 3600,
                    "start_ts": int(start_ts), "end_ts": int(end_ts)}),
                maker_fee_bps=ENGINE_FEE_BPS, fill_model="realistic",
                max_ticks=max(200_000, int((end_ts - start_ts) / 3600) + 10),
            )
            result = BacktestRunner(cfg).run()
        except Exception as e:  # one arm's crash must not lose the report
            out[arm] = {"status": "failed", "errors": [f"{type(e).__name__}: {e}"]}
            continue
        finally:
            if prev is None:
                os.environ.pop("HYDRA_TREND_SLEEVE", None)
            else:
                os.environ["HYDRA_TREND_SLEEVE"] = prev
        m = result.metrics
        out[arm] = {"status": result.status,
                    "total_return_pct": round(float(m.total_return_pct), 4),
                    "sharpe_per_bar_annualized": round(float(m.sharpe), 4),
                    "max_drawdown_pct": round(float(m.max_drawdown_pct), 4),
                    "round_trips": int(m.total_trades), "fills": int(result.fills),
                    "rejects": int(result.rejects),
                    "candles": int(result.candles_processed),
                    "errors": list(result.errors[:3])}
    return out


def engine_check(daily_total_pct: float, arms: dict) -> Dict[str, bool]:
    f = arms.get("engine_off") or {}
    g = arms.get("engine_sleeve") or {}
    ok = f.get("status") == "complete" and g.get("status") == "complete"
    if not ok:
        return {"complete": False, "G_beats_F": False, "fidelity": False}
    g_total = float(g["total_return_pct"]) / 100.0
    d_total = float(daily_total_pct) / 100.0
    if g_total <= -1.0 or d_total <= -1.0:
        fidelity = False
    else:
        gap = abs(math.log1p(g_total) - math.log1p(d_total))
        fidelity = gap <= max(0.5 * abs(math.log1p(d_total)), 0.05)
    return {"complete": True,
            "G_beats_F": float(g["total_return_pct"]) > float(f["total_return_pct"]),
            "fidelity": fidelity}


def overall_verdict(assets: Dict[str, dict], engine_ran: bool) -> Tuple[str, bool]:
    passing = [p for p, a in assets.items() if a.get("verdict") == "PASS"]
    decided = [p for p, a in assets.items() if a.get("verdict") in ("PASS", "FAIL")]
    if len(passing) < 2:
        return ("INSUFFICIENT_DATA" if len(decided) < 2 else "FAIL"), False
    significant = all(
        (assets[p].get("bootstrap_sharpe_diff_sleeve_minus_voltarget_bh") or {}).get("p05")
        is not None
        and assets[p]["bootstrap_sharpe_diff_sleeve_minus_voltarget_bh"]["p05"] > 0
        for p in passing)
    if not engine_ran:
        return "PASS_DAILY_ONLY", significant
    checks = [assets[p].get("engine_check") or {} for p in passing]
    if not all(c.get("complete") for c in checks):
        return "PASS_DAILY_ONLY", significant
    if not all(c.get("fidelity") for c in checks):
        return "FIDELITY_FAIL", significant
    if not all(c.get("G_beats_F") for c in checks):
        return "FAIL", significant
    return "PASS", significant


# ── calibration against a null ─────────────────────────────────────────

def _null_walk(n: int, seed: int, mu: float, sigma: float = 0.035) -> List[float]:
    rng = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= math.exp(mu + rng.gauss(0.0, sigma))
        out.append(px)
    return out


def _trend_regimes(n: int, seed: int, period: int = 180, mu: float = 0.004,
                   sigma: float = 0.03) -> List[float]:
    rng = random.Random(seed)
    px, out, drift = 100.0, [], mu
    for i in range(n):
        if i % period == 0:
            drift = mu if (i // period) % 2 == 0 else -mu
        px *= math.exp(drift + rng.gauss(0.0, sigma))
        out.append(px)
    return out


def calibrate(seeds: int = 40, n_days: int = 2800) -> dict:
    """Per-asset PASS rate under nulls (no trend by construction) and under
    a trending alternative. The registered 2-of-3 rule then puts the global
    false-positive rate near 3 * p^2 for a per-asset rate p."""
    families = {
        "null_random_walk_bull_drift": lambda seed: _null_walk(n_days, seed, 0.0005),
        "null_random_walk_zero_drift": lambda seed: _null_walk(n_days, seed, 0.0),
        "alt_180d_trend_regimes": lambda seed: _trend_regimes(n_days, seed),
    }
    out: Dict[str, dict] = {}
    for name, make in families.items():
        verdicts = []
        for seed in range(1, seeds + 1):
            closes = make(seed)
            days = list(range(16_000, 16_000 + len(closes)))
            verdicts.append(evaluate_asset(days, closes, boot_n=1)["verdict"])
        rate = verdicts.count("PASS") / float(seeds)
        out[name] = {"pass": verdicts.count("PASS"), "runs": seeds,
                     "per_asset_pass_rate": round(rate, 4),
                     "two_of_three_rate_approx": round(3 * rate ** 2 - 2 * rate ** 3, 4)}
    return {"n_days": n_days, "seeds": seeds, "families": out}


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                       stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except Exception:
        return "unknown"


def main(argv: Optional[List[str]] = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # Same store the agent and tools/refresh_history.py use.
    ap.add_argument("--db", default=os.environ.get("HYDRA_HISTORY_DB") or str(DEFAULT_DB),
                    help="SQLite path (env: HYDRA_HISTORY_DB)")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--csv", action="append", default=[], metavar="PAIR=PATH",
                    help="daily closes from a CSV instead of the store")
    ap.add_argument("--engine", action="store_true",
                    help="also run arms F/G through BacktestRunner on the 1h tape")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--boot", type=int, default=BOOT_N, help=argparse.SUPPRESS)
    ap.add_argument("--calibrate", type=int, default=0, metavar="SEEDS",
                    help="measure the gate's PASS rate on synthetic nulls and "
                         "a trending alternative, then exit")
    args = ap.parse_args(argv)

    if args.calibrate:
        report = {"registration": REGISTRATION, "git_sha": _git_sha(),
                  "calibration": calibrate(seeds=args.calibrate)}
        out = Path(args.out if args.out != str(DEFAULT_OUT) else DEFAULT_CALIBRATION_OUT)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for name, row in report["calibration"]["families"].items():
            print(f"[gate] calibration {name}: {row['pass']}/{row['runs']} PASS")
        print(f"[gate] -> {out}")
        return report

    csv_paths: Dict[str, str] = {}
    for item in args.csv:
        pair, _, path = item.partition("=")
        if not pair or not path:
            raise SystemExit(f"--csv expects PAIR=PATH, got {item!r}")
        csv_paths[pair.strip()] = path.strip()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    for pair in csv_paths:
        if pair not in pairs:
            pairs.append(pair)

    assets: Dict[str, dict] = {}
    engine_ran = False
    for pair in pairs:
        if pair in csv_paths:
            series, source = load_daily_csv(csv_paths[pair]), f"csv:{csv_paths[pair]}"
        elif os.path.exists(args.db):
            series, source = load_daily_sqlite(args.db, pair), f"sqlite:{args.db}"
        else:
            series, source = [], f"missing:{args.db}"
        days = [d for d, _ in series]
        closes = [c for _, c in series]
        print(f"[gate] {pair}: {len(series)} daily closes from {source}", flush=True)
        result = evaluate_asset(days, closes, boot_n=args.boot)
        result["source"] = source
        result["daily_closes"] = len(series)
        if len(days) > 1:
            gaps = sum(1 for a, b in zip(days, days[1:]) if b - a > 1)
            result["calendar_gaps"] = gaps
        if args.engine and result.get("verdict") in ("PASS", "FAIL") and pair not in csv_paths:
            if has_hourly(args.db, pair):
                window = result["window"]
                print(f"[gate] {pair}: engine arms {window['start_day']}..{window['end_day']}",
                      flush=True)
                arms = run_engine_arms(args.db, pair, window["start_ts"], window["end_ts"])
                result["engine_arms"] = arms
                result["engine_check"] = engine_check(result["arms"]["sleeve"]["total_pct"], arms)
                engine_ran = True
            else:
                result["engine_arms"] = {"skipped": "no grain-3600 rows for this pair"}
        assets[pair] = result
        print(f"[gate] {pair}: {result['verdict']} {result.get('criteria', '')}", flush=True)

    verdict, significant = overall_verdict(assets, engine_ran)
    report = {
        "registration": REGISTRATION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": _git_sha(),
        "params": {"cap": CAP, "long_at": LONG_AT, "base_cost_per_side": BASE_COST,
                   "stress_cost_per_side": STRESS_COST, "rebalance_days": REBALANCE_DAYS,
                   "min_eval_years": MIN_EVAL_YEARS, "dd_ratio": DD_RATIO,
                   "resize_days": RESIZE_DAYS, "resize_tol": RESIZE_TOL,
                   # The vol target every engine in this run sized with.
                   "target_vol": trend_target_vol_setting(),
                   "boot_n": args.boot, "boot_seed": BOOT_SEED,
                   "engine_balance": ENGINE_BALANCE, "engine_fee_bps": ENGINE_FEE_BPS},
        "assets": assets,
        "engine_ran": engine_ran,
        "verdict": verdict,
        "significant": significant,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[gate] verdict {verdict}{' (significant)' if significant else ''} -> {out}")
    return report


if __name__ == "__main__":
    main()
