"""HYDRA Flywheel — paper capital allocator (evidence-gated engine sleeve).

Replaces "one strategy trades everything" with sleeves. Realized paper
profits compound into the allocation base daily. This module is CLI-only
and is NOT wired into HydraAgent capital routing.

Sleeves
-------
trend   Vol-targeted, long-or-flat, daily trend ensemble on BTC/USD and
        SOL/USD (SMA200 + EMA20x100 majority, Donchian-55 minority —
        stateful turtle channel, same logic as tools/trend_backtest.py).
        Signal-driven: allocation follows live votes × vol target.
        Offline research evidence: tools/trend_backtest.py →
        .hydra-flywheel/trend_results.json (not read by allocate()).

carry   Delta-neutral SOL carry model: long spot + equal-notional short.
        Hedge venue depends on jurisdiction (verified 2026-06-09 / docs):
        - US: Kraken Derivatives US offers CME-style dated cash-settled
          Micro Solana (MSL, 25 SOL/contract). Dated contracts have NO
          funding — true carry is basis roll; paper ledger still accrues
          funding as a climate proxy (co-moves with basis richness).
        - Non-US: PF_SOLUSD perp funding-rate carry.
        Staking: Kraken offers bonded and flexible SOL staking where
        available; US/state eligibility varies. Prefer bonded for multi-
        roll committed capital (2–4d unbond; rewards stop while unbonding).
        Net bonded default ~5% after 25% commission on <$1M AUM tier
        (gross ~6.5–7%). Paper yield = staking + funding proxy − costs.
        Offline research: tools/carry_backtest.py → carry_results.json
        (not read by allocate()).

engine  Legacy 15m regime engine (HydraEngine). ONLY sleeve that is
        evidence-gated: hard 0% until validation_results.json shows a
        run with sharpe >= 0.8 AND max_drawdown_pct <= 35 AND return >
        its B&H benchmark. Even then allocate() never auto-funds engine
        — gate unlocks eligibility only; operator assigns capital later.
        Note: tools/flywheel_validation.py currently replays at 60m
        grain as a proxy; clearing the gate is not proof for live 15m.

cash    Residual at CASH_APY_PCT (Kraken USD/USDC rewards assumption).

Execution
---------
Paper only. `tick()` marks the ledger fee-true and persists
.hydra-flywheel/state.json atomically. NO live order path. Graduating
to live requires (a) authenticated futures client for the hedge leg
and (b) explicit operator sign-off — SPOT-ONLY is not amended.
`apply_targets()` is the future live seam; today it only records targets.

CLI
---
    python hydra_flywheel.py --report
    python hydra_flywheel.py --tick
    python hydra_flywheel.py --replay
    python hydra_flywheel.py --replay --start-ts 1700000000

Stdlib only (engine-side purity rule applies here too).
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

# ── constants ────────────────────────────────────────────────────────────

FLYWHEEL_DIR = ".hydra-flywheel"
STATE_FILE = "state.json"

ASSETS = ("BTC/USD", "SOL/USD")

SPOT_FEE_BPS = 16.0          # Kraken base maker tier, per side
PERP_FEE_BPS = 5.0           # Kraken Futures taker, per side (conservative)
CASH_APY_PCT = 4.0           # USD/USDC rewards assumption; CLI-overridable
# Net of Kraken's commission (25% bonded tier under $1M AUM on ~6.5-7%
# gross). US staking (relaunched 2025, ~39 states) is BONDED ONLY: 2-4 day
# unbond during which rewards stop and the asset cannot be sold — so the
# carry sleeve treats staking as an optional enhancement on capital
# committed across multiple roll cycles, never as the liquid leg. Set
# --staking-apy 0 to model unstaked basis-carry only.
SOL_STAKING_APY_PCT = 5.0    # CLI-overridable

VOL_TARGET = 0.30            # 30% annualized per trend asset
VOL_LOOKBACK_DAYS = 30
TREND_VARIANT_WEIGHTS = {"sma200": 0.4, "ema20x100": 0.4, "don55": 0.2}

# Allocation policy (fractions of total equity)
TREND_BUDGET = 0.50          # split across ASSETS, scaled by trend exposure
CARRY_BUDGET_RICH = 0.35     # funding climate rich
CARRY_BUDGET_POOR = 0.15     # staking yield alone still clears cash hurdle
MIN_CASH = 0.10              # never fully deployed
REBALANCE_BAND = 0.05        # skip rebalance if |target-current| under this

# Evidence gate thresholds for the legacy engine sleeve
ENGINE_GATE = {"min_sharpe": 0.8, "max_drawdown_pct": 35.0}

SECONDS_PER_DAY = 86_400
TRAILING_FUNDING_HOURS = 7 * 24


# ── trend signal (pure functions) ────────────────────────────────────────

def _sma(xs: List[float], n: int) -> Optional[float]:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def _ema_last(xs: List[float], n: int) -> float:
    k = 2.0 / (n + 1)
    e = xs[0]
    for x in xs[1:]:
        e += k * (x - e)
    return e


def realized_vol_annualized(closes: List[float],
                            lookback: int = VOL_LOOKBACK_DAYS) -> Optional[float]:
    """Annualized close-to-close vol over the trailing `lookback` days."""
    if len(closes) < lookback + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - lookback, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.0)


def _don55_stateful(closes: List[float]) -> bool:
    """Stateful turtle Donchian: enter on 55d high breakout, exit on 20d low.

    Matches tools/trend_backtest.signal_stream('don55') final flag so paper
    allocation and offline evidence share the same regime occupancy.
    """
    in_pos = False
    for i in range(len(closes)):
        if i >= 55:
            hi55 = max(closes[i - 55:i])
            lo20 = min(closes[i - 20:i])
            if not in_pos and closes[i] > hi55:
                in_pos = True
            elif in_pos and closes[i] < lo20:
                in_pos = False
    return in_pos


def trend_votes(closes: List[float]) -> Dict[str, bool]:
    """Long/flat vote per ensemble variant on a daily close series."""
    votes: Dict[str, bool] = {}
    sma200 = _sma(closes, 200)
    votes["sma200"] = sma200 is not None and closes[-1] > sma200
    if len(closes) >= 100:
        votes["ema20x100"] = _ema_last(closes, 20) > _ema_last(closes, 100)
    else:
        votes["ema20x100"] = False
    votes["don55"] = _don55_stateful(closes) if len(closes) >= 56 else False
    return votes


def trend_exposure(closes: List[float]) -> float:
    """Desired exposure in [0, 1]: weighted ensemble vote x vol-target scalar.

    Never levered: the vol-target scalar is capped at 1.0.
    """
    if len(closes) < 100:
        return 0.0
    votes = trend_votes(closes)
    vote = sum(TREND_VARIANT_WEIGHTS[v] for v, on in votes.items() if on)
    if vote <= 0.0:
        return 0.0
    rv = realized_vol_annualized(closes)
    scalar = 1.0 if not rv or rv <= 0 else min(1.0, VOL_TARGET / rv)
    return round(vote * scalar, 4)


# ── carry monitor (pure functions) ───────────────────────────────────────

def funding_apr_pct(hourly_relative_rates: List[float]) -> Optional[float]:
    """Trailing-mean hourly relative funding rate, annualized, in percent."""
    if not hourly_relative_rates:
        return None
    mean = sum(hourly_relative_rates) / len(hourly_relative_rates)
    return mean * 24 * 365 * 100.0


def carry_expected_apy_pct(funding_apr: Optional[float],
                           staking_apy: float = SOL_STAKING_APY_PCT,
                           cycles_per_year: float = 2.0) -> float:
    """Expected APY of the hedged staked-SOL carry.

    staking + funding received - amortized round-trip costs. Funding may be
    negative (shorts pay); the position still earns staking. `None` funding
    (no data) is treated as 0, not as an error — the sleeve degrades to
    staking-only economics rather than flying blind into a rich estimate.
    """
    # Percent points per cycle: entry+exit of both legs (spot+perp bps).
    rt_cost_pct = 2 * (SPOT_FEE_BPS + PERP_FEE_BPS) / 100.0
    f = funding_apr if funding_apr is not None else 0.0
    return staking_apy + f - rt_cost_pct * cycles_per_year


def carry_budget(expected_apy_pct: float,
                 cash_apy_pct: float = CASH_APY_PCT) -> float:
    """Size the carry sleeve by what it is expected to EARN vs parked cash.

    >= cash + 4pts  -> rich budget (funding climate is paying)
    >= cash + 1pt   -> small budget (staking alone clears the hurdle)
    otherwise       -> 0 (a sleeve that can't beat cash gets no capital;
                          this also zeroes carry when funding is deeply
                          negative and the short perp leg would bleed)
    """
    if expected_apy_pct >= cash_apy_pct + 4.0:
        return CARRY_BUDGET_RICH
    if expected_apy_pct >= cash_apy_pct + 1.0:
        return CARRY_BUDGET_POOR
    return 0.0


# ── evidence gate ────────────────────────────────────────────────────────

def engine_sleeve_allowed(flywheel_dir: str = FLYWHEEL_DIR) -> Tuple[bool, str]:
    """The legacy 15m engine gets capital ONLY if its real-data validation
    evidence clears the gate. Reads validation_results.json produced by
    tools/flywheel_validation.py. Missing file = no evidence = 0%."""
    path = os.path.join(flywheel_dir, "validation_results.json")
    try:
        runs = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return False, "no validation evidence on disk"
    if not isinstance(runs, list) or not runs:
        return False, "no validation evidence on disk"
    for run in runs:
        if not isinstance(run, dict):
            continue
        s = run.get("strategy") or {}
        if not isinstance(s, dict):
            continue
        bh = run.get("buy_and_hold") or {}
        if not isinstance(bh, dict):
            bh = {}
        bench_best = max(
            (v.get("total_pct", 0.0) for v in bh.values() if isinstance(v, dict)),
            default=0.0)
        if (s.get("sharpe", 0.0) >= ENGINE_GATE["min_sharpe"]
                and s.get("max_drawdown_pct", 100.0) <= ENGINE_GATE["max_drawdown_pct"]
                and s.get("total_return_pct", -1e9) > bench_best):
            return True, f"passed via run '{run.get('name')}'"
    return False, ("validation evidence exists but no run clears the gate "
                   f"(need sharpe>={ENGINE_GATE['min_sharpe']}, "
                   f"maxDD<={ENGINE_GATE['max_drawdown_pct']}%, return > B&H)")


# ── allocator ────────────────────────────────────────────────────────────

@dataclass
class Targets:
    """Desired fraction of total equity per sleeve (sums to <= 1.0)."""
    trend: Dict[str, float] = field(default_factory=dict)  # per asset
    carry: float = 0.0
    engine: float = 0.0
    cash: float = 1.0
    notes: List[str] = field(default_factory=list)


def allocate(trend_exp: Dict[str, float],
             funding_apr: Optional[float],
             flywheel_dir: str = FLYWHEEL_DIR,
             cash_apy_pct: float = CASH_APY_PCT,
             staking_apy_pct: float = SOL_STAKING_APY_PCT) -> Targets:
    t = Targets()
    per_asset_budget = TREND_BUDGET / max(1, len(trend_exp))
    for asset, exp in trend_exp.items():
        t.trend[asset] = round(per_asset_budget * max(0.0, min(1.0, exp)), 4)

    apy = carry_expected_apy_pct(funding_apr, staking_apy_pct)
    t.carry = carry_budget(apy, cash_apy_pct)
    apr_str = f"{funding_apr:.1f}%" if funding_apr is not None else "n/a"
    t.notes.append(f"carry expected APY {apy:.1f}% (7d funding APR {apr_str})")

    allowed, why = engine_sleeve_allowed(flywheel_dir)
    t.engine = 0.0
    t.notes.append(f"engine sleeve: {'ALLOWED' if allowed else 'GATED 0%'} - {why}")
    # Even when allowed, capital for the engine sleeve must be assigned by
    # the operator; the gate only unlocks eligibility, never auto-funds.

    deployed = sum(t.trend.values()) + t.carry + t.engine
    max_deploy = 1.0 - MIN_CASH
    if deployed > max_deploy:
        scale = max_deploy / deployed
        t.trend = {a: round(w * scale, 4) for a, w in t.trend.items()}
        t.carry = round(t.carry * scale, 4)
        t.notes.append(f"scaled all sleeves by {scale:.3f} to keep "
                       f"{MIN_CASH:.0%} min cash")
        deployed = max_deploy
    t.cash = round(1.0 - deployed, 4)
    return t


# ── paper ledger ─────────────────────────────────────────────────────────

@dataclass
class Ledger:
    """Fee-true paper ledger. Units of quote currency (USD)."""
    equity: float = 30_000.0
    trend_units: Dict[str, float] = field(default_factory=dict)   # asset -> units held
    carry_notional: float = 0.0
    fees_paid: float = 0.0
    funding_collected: float = 0.0
    staking_collected: float = 0.0
    cash_interest: float = 0.0
    peak_equity: float = 30_000.0
    max_drawdown_pct: float = 0.0
    days: int = 0
    last_tick_day: Optional[int] = None  # UTC day index; guards double-tick


def mark_and_rebalance(ledger: Ledger,
                       targets: Targets,
                       closes: Dict[str, float],
                       prev_closes: Dict[str, float],
                       funding_day_relative: float,
                       cash_apy_pct: float = CASH_APY_PCT,
                       staking_apy_pct: float = SOL_STAKING_APY_PCT) -> Ledger:
    """One daily paper tick: mark positions, accrue yields, rebalance toward
    targets with fees charged on turnover. Mutates and returns `ledger`."""
    # 1) mark trend positions at today's closes
    for asset, units in ledger.trend_units.items():
        prev, now = prev_closes.get(asset), closes.get(asset)
        if prev and now and units:
            ledger.equity += units * (now - prev)

    # 2) accrue carry yields on hedged notional (price-neutral by construction)
    if ledger.carry_notional > 0:
        daily_staking = ledger.carry_notional * (staking_apy_pct / 100.0) / 365.0
        daily_funding = ledger.carry_notional * funding_day_relative
        ledger.staking_collected += daily_staking
        ledger.funding_collected += daily_funding
        ledger.equity += daily_staking + daily_funding

    # 3) cash interest on the un-deployed residual
    deployed = (sum(abs(u) * closes.get(a, 0.0)
                    for a, u in ledger.trend_units.items())
                + ledger.carry_notional)
    cash = max(0.0, ledger.equity - deployed)
    interest = cash * (cash_apy_pct / 100.0) / 365.0
    ledger.cash_interest += interest
    ledger.equity += interest

    # 4) rebalance toward targets (band to avoid fee churn)
    for asset, weight in targets.trend.items():
        px = closes.get(asset)
        if not px:
            continue
        want_value = weight * ledger.equity
        have_value = ledger.trend_units.get(asset, 0.0) * px
        if ledger.equity > 0 and abs(want_value - have_value) / ledger.equity > REBALANCE_BAND:
            turnover = abs(want_value - have_value)
            fee = turnover * SPOT_FEE_BPS / 10_000.0
            ledger.fees_paid += fee
            ledger.equity -= fee
            ledger.trend_units[asset] = want_value / px

    want_carry = targets.carry * ledger.equity
    if ledger.equity > 0 and abs(want_carry - ledger.carry_notional) / ledger.equity > REBALANCE_BAND:
        turnover = abs(want_carry - ledger.carry_notional)
        fee = turnover * (SPOT_FEE_BPS + PERP_FEE_BPS) / 10_000.0  # both legs
        ledger.fees_paid += fee
        ledger.equity -= fee
        ledger.carry_notional = want_carry

    # 5) drawdown bookkeeping
    ledger.days += 1
    ledger.peak_equity = max(ledger.peak_equity, ledger.equity)
    if ledger.peak_equity > 0:
        dd = (ledger.peak_equity - ledger.equity) / ledger.peak_equity * 100.0
        ledger.max_drawdown_pct = max(ledger.max_drawdown_pct, dd)
    return ledger


# ── engine wrapper: state, data access, CLI ──────────────────────────────

class FlywheelEngine:
    """Glues signals, allocator, and ledger to the canonical data stores.

    Paper-only: see module docstring. `apply_targets()` is the future live
    seam and currently just records targets in state.
    """

    def __init__(self, root: str = ".", initial_equity: float = 30_000.0,
                 cash_apy_pct: float = CASH_APY_PCT,
                 staking_apy_pct: float = SOL_STAKING_APY_PCT):
        self.root = root
        self.dir = os.path.join(root, FLYWHEEL_DIR)
        os.makedirs(self.dir, exist_ok=True)
        self.cash_apy_pct = cash_apy_pct
        self.staking_apy_pct = staking_apy_pct
        self._daily_cache: Dict[str, List[Tuple[int, float]]] = {}
        self._last_targets: Optional[Targets] = None
        self.ledger = self._load_state() or Ledger(
            equity=initial_equity, peak_equity=initial_equity)

    def _history_db_path(self) -> str:
        """Canonical OHLC store — honors HYDRA_HISTORY_DB like agent/tools."""
        override = os.environ.get("HYDRA_HISTORY_DB")
        if override:
            return override
        return os.path.join(self.root, "hydra_history.sqlite")

    # -- persistence (atomic, same pattern as the agent snapshot) --

    def _state_path(self) -> str:
        return os.path.join(self.dir, STATE_FILE)

    def _load_state(self) -> Optional[Ledger]:
        try:
            raw = json.loads(open(self._state_path(), encoding="utf-8").read())
            led = raw["ledger"]
            # Tolerate older state files missing last_tick_day.
            known = set(Ledger.__dataclass_fields__)
            filtered = {k: v for k, v in led.items() if k in known}
            return Ledger(**filtered)
        except OSError:
            return None  # no state yet — fresh start is the normal case
        except (ValueError, KeyError, TypeError) as e:
            # Corrupt state must not silently erase the paper track record.
            print(f"[FLYWHEEL][WARN] {self._state_path()} unreadable ({e}); "
                  "starting a fresh ledger", file=sys.stderr)
            return None

    def save_state(self, targets: Optional[Targets] = None) -> None:
        payload = {"ledger": asdict(self.ledger),
                   "targets": asdict(targets) if targets else None,
                   "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self._state_path())

    def apply_targets(self, targets: Targets) -> Targets:
        """Future live seam: today paper-only — record targets, place nothing.

        Deliberately has no Kraken/order path. Live graduation must add an
        authenticated futures client + explicit opt-in outside this method's
        current body.
        """
        self._last_targets = targets
        return targets

    # -- data access --

    def daily_closes(self, pair: str, end_ts: Optional[int] = None) -> List[float]:
        """Daily close series (last 60m close per UTC day), ascending.
        Series are cached per pair; end_ts slices the cache (used by replay
        so no tick ever sees a future candle)."""
        if pair not in self._daily_cache:
            db = sqlite3.connect(self._history_db_path())
            try:
                days: Dict[int, float] = {}
                for ts, close in db.execute(
                        "select ts, close from ohlc where pair=? and "
                        "grain_sec=3600 order by ts", (pair,)):
                    days[ts // SECONDS_PER_DAY] = close
                self._daily_cache[pair] = sorted(days.items())
            finally:
                db.close()
        series = self._daily_cache[pair]
        if end_ts is None:
            return [c for _, c in series]
        end_day = end_ts // SECONDS_PER_DAY
        return [c for d, c in series if d <= end_day]

    def funding_rates(self, symbol: str = "PF_SOLUSD",
                      end_ts: Optional[int] = None) -> List[Tuple[int, float]]:
        """Hourly (epoch_ts, relative_rate), ascending, optionally clipped to
        end_ts so replays never see future funding."""
        path = os.path.join(self.dir, f"funding_{symbol}.json")
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            return []
        rates = []
        for r in data.get("rates", []):
            try:
                ts = calendar.timegm(time.strptime(
                    r["timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
                rates.append((ts, float(r["relativeFundingRate"])))
            except (KeyError, ValueError):
                continue
        rates.sort(key=lambda x: x[0])
        if end_ts is not None:
            rates = [r for r in rates if r[0] <= end_ts]
        return rates

    # -- one daily tick --

    def compute_targets(self, end_ts: Optional[int] = None) -> Targets:
        exp = {a: trend_exposure(self.daily_closes(a, end_ts)) for a in ASSETS}
        rates = [r for _, r in
                 self.funding_rates(end_ts=end_ts)][-TRAILING_FUNDING_HOURS:]
        apr = funding_apr_pct(rates) if rates else None
        return allocate(exp, apr, self.dir,
                        self.cash_apy_pct, self.staking_apy_pct)

    def tick(self, end_ts: Optional[int] = None, persist: bool = True) -> Targets:
        targets = self.compute_targets(end_ts)
        self.apply_targets(targets)
        # Double-tick guard: same UTC day must not re-mark / re-accrue.
        day_idx = (end_ts if end_ts is not None else int(time.time())) // SECONDS_PER_DAY
        if self.ledger.last_tick_day is not None and self.ledger.last_tick_day == day_idx:
            if persist:
                self.save_state(targets)
            return targets
        closes, prevs = {}, {}
        for a in ASSETS:
            series = self.daily_closes(a, end_ts)
            if len(series) >= 2:
                closes[a], prevs[a] = series[-1], series[-2]
        rates = [r for _, r in self.funding_rates(end_ts=end_ts)][-24:]
        funding_day = sum(rates) if rates else 0.0
        mark_and_rebalance(self.ledger, targets, closes, prevs, funding_day,
                           self.cash_apy_pct, self.staking_apy_pct)
        self.ledger.last_tick_day = day_idx
        if persist:
            self.save_state(targets)
        return targets

    def report(self) -> str:
        targets = self.compute_targets()
        led = self.ledger
        lines = [
            "HYDRA Flywheel - paper ledger",
            f"  equity            ${led.equity:,.2f}",
            f"  max drawdown      {led.max_drawdown_pct:.2f}%",
            f"  fees paid         ${led.fees_paid:,.2f}",
            f"  funding collected ${led.funding_collected:,.2f}",
            f"  staking collected ${led.staking_collected:,.2f}",
            f"  cash interest     ${led.cash_interest:,.2f}",
            f"  days ticked       {led.days}",
            "targets:",
            f"  trend  {targets.trend}",
            f"  carry  {targets.carry:.2%}   engine {targets.engine:.2%}   "
            f"cash {targets.cash:.2%}",
        ]
        lines += [f"  note: {n}" for n in targets.notes]
        return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────

def _replay(engine: FlywheelEngine, start_ts: Optional[int]) -> None:
    """Paper-replay daily ticks across the full sqlite history overlap.
    Always starts from a fresh ledger — never continues saved live state."""
    initial = engine.ledger.equity or 30_000.0
    engine.ledger = Ledger(equity=initial, peak_equity=initial)
    db_path = engine._history_db_path()
    db = sqlite3.connect(db_path)
    lo, hi = db.execute(
        "select max(lo), min(hi) from (select min(ts) lo, max(ts) hi "
        "from ohlc where grain_sec=3600 and pair in (?,?) group by pair)",
        ASSETS).fetchone()
    db.close()
    if lo is None or hi is None:
        raise SystemExit(f"no 60m candles for {ASSETS} in {db_path} "
                         "— run tools/refresh_history.py first")
    lo = max(lo, start_ts or lo)
    day = (lo // SECONDS_PER_DAY + 210) * SECONDS_PER_DAY  # warmup for SMA200
    n = 0
    while day <= hi:
        engine.tick(end_ts=day, persist=False)  # replay never touches state.json
        day += SECONDS_PER_DAY
        n += 1
        if n % 365 == 0:
            print(f"  ...{n} days, equity ${engine.ledger.equity:,.0f}")
    led = engine.ledger
    years = max(n / 365.0, 1e-9)
    cagr = ((led.equity / initial) ** (1.0 / years) - 1.0) * 100.0 \
        if led.equity > 0 else -100.0
    print(f"replayed {n} daily ticks ({years:.2f}y)  "
          f"CAGR {cagr:+.2f}%  maxDD {led.max_drawdown_pct:.1f}%")
    print(engine.report())


def main() -> None:
    ap = argparse.ArgumentParser(description="HYDRA Flywheel (paper-first)")
    ap.add_argument("--tick", action="store_true", help="run one daily paper tick")
    ap.add_argument("--report", action="store_true", help="print targets + ledger")
    ap.add_argument("--replay", action="store_true",
                    help="paper-replay daily ticks over stored history")
    ap.add_argument("--start-ts", type=int, default=None)
    ap.add_argument("--equity", type=float, default=30_000.0)
    ap.add_argument("--cash-apy", type=float, default=CASH_APY_PCT)
    ap.add_argument("--staking-apy", type=float, default=SOL_STAKING_APY_PCT)
    args = ap.parse_args()

    engine = FlywheelEngine(initial_equity=args.equity,
                            cash_apy_pct=args.cash_apy,
                            staking_apy_pct=args.staking_apy)
    if args.replay:
        _replay(engine, args.start_ts)
    elif args.tick:
        targets = engine.tick()
        print(engine.report())
        _ = targets
    else:
        print(engine.report())


if __name__ == "__main__":
    main()
