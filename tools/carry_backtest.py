"""Flywheel Phase 0 — backtest delta-neutral funding carry on REAL funding data.

Strategy under test: long spot + short the equivalent Kraken Futures perp
(PF_SOLUSD / PF_XBTUSD), equal notional. Price risk nets to ~zero; the
position collects the hourly funding payment whenever funding is positive
(longs pay shorts) and pays it when negative.

Data: .hydra-flywheel/funding_PF_*.json — real hourly funding from the Kraken
Futures public API (`relativeFundingRate` = fraction of mark price per hour).

Cost model (conservative, per round trip, on notional):
    spot maker 16 bps in + 16 bps out          (Kraken base maker tier)
    perp  taker  5 bps in +  5 bps out         (assume taker both legs)
    total 42 bps per cycle
Capital model: deploy_frac of equity is the carry notional (rest is margin
buffer for the short perp leg). Funding accrues on notional; equity compounds.

Honesty constraints reported alongside results: 1y of data only; basis
drift between spot and perp is not modeled (delta-neutral assumption);
liquidation risk on the perp leg is mitigated by the margin buffer, not
modeled explicitly.

Usage: python tools/carry_backtest.py
Output: stdout table + .hydra-flywheel/carry_results.json
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".hydra-flywheel"

HOURS_PER_YEAR = 24 * 365
ROUND_TRIP_COST_BPS = 16 + 16 + 5 + 5  # spot maker x2 + perp taker x2
TRAIL_HOURS = 24  # trailing window for the entry/exit signal


def load_rates(symbol: str):
    path = OUT_DIR / f"funding_{symbol}.json"
    data = json.loads(path.read_text())
    rates = [(r["timestamp"], float(r["relativeFundingRate"])) for r in data["rates"]]
    rates.sort(key=lambda x: x[0])
    return rates


def trailing_apr_pct(window):
    """Trailing-mean hourly relative rate, annualized, in percent."""
    if not window:
        return 0.0
    return (sum(window) / len(window)) * HOURS_PER_YEAR * 100.0


def simulate(rates, enter_apr, exit_apr, deploy_frac):
    """Hysteresis carry: enter when trailing APR >= enter_apr, exit when
    trailing APR < exit_apr. Returns equity curve stats. Equity normalized
    to 1.0; funding accrues on notional = deploy_frac * equity_at_entry,
    re-based each entry (compounding between cycles)."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    in_pos = False
    notional = 0.0
    round_trips = 0
    hours_in = 0
    cost = ROUND_TRIP_COST_BPS / 10_000.0
    window = []
    for _, rate in rates:
        window.append(rate)
        if len(window) > TRAIL_HOURS:
            window.pop(0)
        apr = trailing_apr_pct(window)
        if in_pos:
            equity += rate * notional          # short receives positive funding
            hours_in += 1
            if apr < exit_apr and len(window) == TRAIL_HOURS:
                equity -= cost * notional / 2.0  # exit half of round trip
                in_pos = False
                round_trips += 1
        else:
            if apr >= enter_apr and len(window) == TRAIL_HOURS:
                notional = deploy_frac * equity
                equity -= cost * notional / 2.0  # entry half of round trip
                in_pos = True
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    if in_pos:  # mark final exit cost for fairness
        equity -= cost * notional / 2.0
        round_trips += 1
    years = len(rates) / HOURS_PER_YEAR
    total_pct = (equity - 1.0) * 100.0
    annual_pct = ((equity) ** (1.0 / years) - 1.0) * 100.0 if equity > 0 else -100.0
    return {
        "enter_apr": enter_apr, "exit_apr": exit_apr, "deploy_frac": deploy_frac,
        "total_return_pct": round(total_pct, 2),
        "annualized_pct": round(annual_pct, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "round_trips": round_trips,
        "pct_time_in_market": round(100.0 * hours_in / len(rates), 1),
        "years": round(years, 2),
    }


def funding_climate(rates):
    vals = [r for _, r in rates]
    pos = sum(1 for v in vals if v > 0)
    gross_always_in_apr = (sum(vals) / len(vals)) * HOURS_PER_YEAR * 100.0
    return {
        "hours": len(vals),
        "pct_hours_positive": round(100.0 * pos / len(vals), 1),
        "gross_always_in_apr_pct": round(gross_always_in_apr, 2),
        "worst_hour_bps": round(min(vals) * 10_000, 2),
        "best_hour_bps": round(max(vals) * 10_000, 2),
    }


def main():
    sweep = [
        # (enter_apr%, exit_apr%)
        (-1000.0, -1001.0),  # always-in baseline (never exits)
        (0.0, -2.0),
        (3.0, 0.0),
        (5.0, 0.0),
        (10.0, 3.0),
    ]
    results = {}
    for symbol in ("PF_SOLUSD", "PF_XBTUSD"):
        rates = load_rates(symbol)
        climate = funding_climate(rates)
        runs = []
        for enter, exit_ in sweep:
            for deploy in (0.65, 1.0):
                runs.append(simulate(rates, enter, exit_, deploy))
        results[symbol] = {"climate": climate, "runs": runs}
        print(f"\n=== {symbol} ===")
        print(f"  climate: {climate}")
        hdr = (f"  {'enter':>7} {'exit':>6} {'deploy':>6} {'total%':>8} "
               f"{'annual%':>8} {'maxDD%':>7} {'trips':>5} {'in-mkt%':>7}")
        print(hdr)
        for r in runs:
            print(f"  {r['enter_apr']:>7.1f} {r['exit_apr']:>6.1f} "
                  f"{r['deploy_frac']:>6.2f} {r['total_return_pct']:>8.2f} "
                  f"{r['annualized_pct']:>8.2f} {r['max_drawdown_pct']:>7.2f} "
                  f"{r['round_trips']:>5d} {r['pct_time_in_market']:>7.1f}")

    # 50/50 SOL+BTC portfolio, best-practice config (enter 3, exit 0, 0.65)
    sol = simulate(load_rates("PF_SOLUSD"), 3.0, 0.0, 0.65)
    btc = simulate(load_rates("PF_XBTUSD"), 3.0, 0.0, 0.65)
    port_annual = (sol["annualized_pct"] + btc["annualized_pct"]) / 2.0
    results["portfolio_50_50"] = {"sol": sol, "btc": btc,
                                  "annualized_pct": round(port_annual, 2)}
    print(f"\n50/50 SOL+BTC carry portfolio (enter 3% APR, exit 0%, deploy 0.65): "
          f"~{port_annual:.2f}%/yr")

    out = OUT_DIR / "carry_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
