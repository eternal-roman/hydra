"""HYDRA Flywheel — evidence-gated capital allocator (paper-first).

The flywheel replaces "one strategy trades everything" with a portfolio of
sleeves, each of which must EARN its capital by showing positive after-fee,
out-of-sample evidence on real market data before a single dollar routes to
it. Realized profits compound into the allocation base daily.

Sleeves
-------
trend   Vol-targeted, long-or-flat, daily-timeframe trend ensemble on
        BTC/USD and SOL/USD (SMA200 + EMA20x100 majority, Donchian-55
        minority). Validated on 11y BTC / 4.9y SOL real candles:
        Sharpe 1.36-1.50 (BTC, full period, vol-targeted) with ~30% max
        drawdown vs 84% for buy-and-hold; preserved capital (+/-0-13%)
        through the 2025-26 bear that cost B&H 38-50%.
        Evidence: .hydra-flywheel/trend_results.json (tools/trend_backtest.py).

carry   Delta-neutral SOL carry: long spot + equal-notional short hedge.
        Hedge venue depends on jurisdiction (verified 2026-06-09):
        - US accounts: Kraken Derivatives US lists CME-style dated,
          cash-settled contracts incl. Micro Solana (MSL, 25 SOL/contract,
          margin roughly 25-35% of notional — a $10k book needs ~2-3
          contracts and ~$3-4k margin; questionnaire + disclosures, no
          ECP / account minimum. ECP $10M applies to SPOT MARGIN, not
          futures). Dated contracts have NO funding — carry = selling a
          rich basis and rolling monthly/quarterly, so entries key off
          annualized basis instead of funding APR (they co-move; the
          funding history is the climate proxy).
        - Non-US: PF_SOLUSD perp, funding-rate carry as classically built.
        Yield = (optional bonded staking on long-horizon slices) + basis/
        funding received - costs. The Jun'25-Jun'26 climate averaged only
        0.9% APR gross on SOL, so the sleeve sizes UP only when the
        trailing climate is rich; expected APY below the cash hurdle = 0%.
        Hedge granularity note: 25 SOL/contract quantizes the US book.
        Evidence: .hydra-flywheel/carry_results.json (tools/carry_backtest.py).

engine  The legacy 15m regime engine (HydraEngine). HARD-GATED at 0%:
        real-data validation (tools/flywheel_validation.py) showed it
        underperforms buy-and-hold with negative Sharpe on 1y windows and
        a 94% max drawdown on the full SOL history. It stays at 0% until
        .hydra-flywheel/validation_results.json shows a run with
        sharpe >= 0.8 AND max_drawdown_pct <= 35 AND total return beating
        its own buy-and-hold benchmark. The gate reads the evidence file —
        no human override flag exists on purpose.

cash    Residual. Modeled at CASH_APY_PCT (Kraken USD/USDC rewards tier);
        set --cash-apy 0 if balances are not enrolled.

Execution
---------
Paper only, by design, in this version. `FlywheelEngine.tick()` consumes
daily closes (and optionally a funding snapshot), marks the ledger
fee-true (16 bps per spot side, 5 bps per perp side), and persists
.hydra-flywheel/state.json atomically. There is deliberately NO live
order path in this module: graduating the flywheel to live capital
requires (a) the carry hedge leg's authenticated futures client, which
does not exist in this codebase yet, and (b) the operator's explicit
sign-off — see CLAUDE.md SPOT-ONLY invariant, which this module does not
amend. The clean seam for that future work is `apply_targets()`.

CLI
---
    python hydra_flywheel.py --report            # current targets + ledger
    python hydra_flywheel.py --tick              # one daily paper tick
    python hydra_flywheel.py --replay            # paper-replay full history
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
CARRY_RICH_APR_PCT = 5.0     # trailing 7d funding APR threshold
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


def trend_votes(closes: List[float]) -> Dict[str, bool]:
    """Long/flat vote per ensemble variant on a daily close series."""
    votes: Dict[str, bool] = {}
    sma200 = _sma(closes, 200)
    votes["sma200"] = sma200 is not None and closes[-1] > sma200
    if len(closes) >= 100:
        votes["ema20x100"] = _ema_last(closes, 20) > _ema_last(closes, 100)
    else:
        votes["ema20x100"] = False
    if len(closes) >= 56:
        hi55 = max(closes[-56:-1])
        lo20 = min(closes[-21:-1])
        # stateless approximation of the breakout channel: long while price
        # is closer to the 55d high regime than the 20d-low exit
        votes["don55"] = closes[-1] > hi55 or (closes[-1] > lo20 and
                                               closes[-1] > _sma(closes, 55))
    else:
        votes["don55"] = False
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
    rt_cost_pct = 2 * (SPOT_FEE_BPS + PERP_FEE_BPS) / 100.0 / 100.0 * 100.0
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
    for run in runs:
        s = run.get("strategy", {})
        bh = run.get("buy_and_hold", {})
        bench_best = max((v.get("total_pct", 0.0) for v in bh.values()),
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
        self.ledger = self._load_state() or Ledger(
            equity=initial_equity, peak_equity=initial_equity)

    # -- persistence (atomic, same pattern as the agent snapshot) --

    def _state_path(self) -> str:
        return os.path.join(self.dir, STATE_FILE)

    def _load_state(self) -> Optional[Ledger]:
        try:
            raw = json.loads(open(self._state_path(), encoding="utf-8").read())
            return Ledger(**raw["ledger"])
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

    # -- data access --

    def daily_closes(self, pair: str, end_ts: Optional[int] = None) -> List[float]:
        """Daily close series (last 60m close per UTC day), ascending.
        Series are cached per pair; end_ts slices the cache (used by replay
        so no tick ever sees a future candle)."""
        if pair not in self._daily_cache:
            db = sqlite3.connect(os.path.join(self.root, "hydra_history.sqlite"))
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
        closes, prevs = {}, {}
        for a in ASSETS:
            series = self.daily_closes(a, end_ts)
            if len(series) >= 2:
                closes[a], prevs[a] = series[-1], series[-2]
        rates = [r for _, r in self.funding_rates(end_ts=end_ts)][-24:]
        funding_day = sum(rates) if rates else 0.0
        mark_and_rebalance(self.ledger, targets, closes, prevs, funding_day,
                           self.cash_apy_pct, self.staking_apy_pct)
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
    db = sqlite3.connect(os.path.join(engine.root, "hydra_history.sqlite"))
    lo, hi = db.execute(
        "select max(lo), min(hi) from (select min(ts) lo, max(ts) hi "
        "from ohlc where grain_sec=3600 and pair in (?,?) group by pair)",
        ASSETS).fetchone()
    db.close()
    if lo is None or hi is None:
        raise SystemExit(f"no 60m candles for {ASSETS} in hydra_history.sqlite "
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
