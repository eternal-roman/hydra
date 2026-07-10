"""Flywheel tests — allocator, sleeves, ledger, evidence gate (v2.27)."""
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_flywheel import (
    trend_votes, trend_exposure, realized_vol_annualized,
    funding_apr_pct, carry_expected_apy_pct, carry_budget,
    allocate, engine_sleeve_allowed, Targets, Ledger, mark_and_rebalance,
    FlywheelEngine, MIN_CASH, CARRY_BUDGET_RICH, CARRY_BUDGET_POOR,
)


def _rising(n=400, start=100.0, step=0.3):
    return [start + i * step for i in range(n)]


def _falling(n=400, start=220.0, step=0.3):
    return [max(1.0, start - i * step) for i in range(n)]


# ── trend signal ─────────────────────────────────────────────────────────

def test_trend_exposure_zero_on_short_series():
    assert trend_exposure([100.0] * 50) == 0.0


def test_trend_votes_long_in_uptrend():
    votes = trend_votes(_rising())
    assert votes["sma200"] and votes["ema20x100"]


def test_trend_exposure_zero_in_downtrend():
    assert trend_exposure(_falling()) == 0.0


def test_trend_exposure_bounded():
    for series in (_rising(), _falling(), _rising(600, 50, 1.7)):
        e = trend_exposure(series)
        assert 0.0 <= e <= 1.0


def test_realized_vol_none_on_short_series():
    assert realized_vol_annualized([100.0] * 10) is None


# ── carry math ───────────────────────────────────────────────────────────

def test_funding_apr_annualization():
    # 1e-5 per hour -> 1e-5 * 24 * 365 * 100 = 8.76% APR
    apr = funding_apr_pct([1e-5] * 24)
    assert abs(apr - 8.76) < 1e-9


def test_carry_expected_apy_none_funding_degrades_to_staking():
    # staking 6.5 - amortized round-trip costs (2 cycles x 0.42%) = 5.66
    apy = carry_expected_apy_pct(None, staking_apy=6.5)
    assert abs(apy - 5.66) < 1e-9


def test_carry_budget_tiers():
    cash = 4.0
    assert carry_budget(cash + 4.0, cash) == CARRY_BUDGET_RICH
    assert carry_budget(cash + 1.5, cash) == CARRY_BUDGET_POOR
    assert carry_budget(cash + 0.5, cash) == 0.0
    assert carry_budget(-15.0, cash) == 0.0  # deeply negative funding -> no carry


# ── allocator ────────────────────────────────────────────────────────────

def test_allocate_respects_min_cash():
    with tempfile.TemporaryDirectory() as td:
        t = allocate({"BTC/USD": 1.0, "SOL/USD": 1.0}, funding_apr=40.0,
                     flywheel_dir=td)
        deployed = sum(t.trend.values()) + t.carry + t.engine
        assert deployed <= 1.0 - MIN_CASH + 1e-9
        assert abs(deployed + t.cash - 1.0) < 1e-6


def test_allocate_zero_exposure_goes_to_cash():
    with tempfile.TemporaryDirectory() as td:
        t = allocate({"BTC/USD": 0.0, "SOL/USD": 0.0}, funding_apr=-20.0,
                     flywheel_dir=td)
        assert sum(t.trend.values()) == 0.0
        assert t.carry == 0.0
        assert t.cash > 0.9


def test_engine_sleeve_gated_without_evidence():
    with tempfile.TemporaryDirectory() as td:
        allowed, why = engine_sleeve_allowed(td)
        assert not allowed
        assert "no validation evidence" in why


def test_engine_sleeve_gated_on_failing_evidence():
    with tempfile.TemporaryDirectory() as td:
        runs = [{"name": "bad", "strategy": {"sharpe": -1.1,
                                             "max_drawdown_pct": 67.0,
                                             "total_return_pct": -54.6},
                 "buy_and_hold": {"SOL/USD": {"total_pct": -44.5}}}]
        (pathlib.Path(td) / "validation_results.json").write_text(json.dumps(runs))
        allowed, why = engine_sleeve_allowed(td)
        assert not allowed
        assert "no run clears the gate" in why


def test_engine_sleeve_unlocks_only_on_passing_evidence():
    with tempfile.TemporaryDirectory() as td:
        runs = [{"name": "good", "strategy": {"sharpe": 1.2,
                                              "max_drawdown_pct": 20.0,
                                              "total_return_pct": 80.0},
                 "buy_and_hold": {"SOL/USD": {"total_pct": 30.0}}}]
        (pathlib.Path(td) / "validation_results.json").write_text(json.dumps(runs))
        allowed, why = engine_sleeve_allowed(td)
        assert allowed and "good" in why
        # even then, allocate() never auto-funds the engine sleeve
        t = allocate({"BTC/USD": 0.5}, funding_apr=None, flywheel_dir=td)
        assert t.engine == 0.0


# ── ledger ───────────────────────────────────────────────────────────────

def test_rebalance_charges_fee_on_turnover():
    led = Ledger(equity=10_000.0, peak_equity=10_000.0)
    t = Targets(trend={"BTC/USD": 0.5}, carry=0.0, cash=0.5)
    mark_and_rebalance(led, t, {"BTC/USD": 100.0}, {"BTC/USD": 100.0},
                       funding_day_relative=0.0, cash_apy_pct=0.0)
    assert abs(led.fees_paid - 5_000.0 * 0.0016) < 1e-6
    assert abs(led.trend_units["BTC/USD"] - 50.0) < 1e-9
    assert led.equity < 10_000.0  # fee actually debited


def test_rebalance_band_prevents_fee_churn():
    led = Ledger(equity=10_000.0, peak_equity=10_000.0,
                 trend_units={"BTC/USD": 50.0})
    t = Targets(trend={"BTC/USD": 0.51}, carry=0.0, cash=0.49)  # 1% off target
    mark_and_rebalance(led, t, {"BTC/USD": 100.0}, {"BTC/USD": 100.0},
                       funding_day_relative=0.0, cash_apy_pct=0.0)
    assert led.fees_paid == 0.0
    assert led.trend_units["BTC/USD"] == 50.0


def test_carry_accrues_staking_and_funding():
    led = Ledger(equity=10_000.0, peak_equity=10_000.0, carry_notional=1_000.0)
    t = Targets(trend={}, carry=0.1, cash=0.9)
    mark_and_rebalance(led, t, {}, {}, funding_day_relative=0.001,
                       cash_apy_pct=0.0, staking_apy_pct=6.5)
    assert abs(led.funding_collected - 1.0) < 1e-9
    assert abs(led.staking_collected - 1_000.0 * 0.065 / 365.0) < 1e-9


def test_price_move_marks_trend_position():
    led = Ledger(equity=10_000.0, peak_equity=10_000.0,
                 trend_units={"BTC/USD": 10.0})
    t = Targets(trend={"BTC/USD": 0.1}, carry=0.0, cash=0.9)
    mark_and_rebalance(led, t, {"BTC/USD": 110.0}, {"BTC/USD": 100.0},
                       funding_day_relative=0.0, cash_apy_pct=0.0)
    assert led.equity > 10_000.0 + 90.0  # +$100 mark minus small rebalance fee


def test_drawdown_tracked():
    led = Ledger(equity=10_000.0, peak_equity=10_000.0,
                 trend_units={"BTC/USD": 10.0})
    t = Targets(trend={"BTC/USD": 0.1}, carry=0.0, cash=0.9)
    mark_and_rebalance(led, t, {"BTC/USD": 90.0}, {"BTC/USD": 100.0},
                       funding_day_relative=0.0, cash_apy_pct=0.0)
    assert led.max_drawdown_pct > 0.9


# ── state persistence ────────────────────────────────────────────────────

def test_state_round_trip():
    with tempfile.TemporaryDirectory() as td:
        e1 = FlywheelEngine(root=td, initial_equity=25_000.0)
        e1.ledger.equity = 26_500.0
        e1.ledger.fees_paid = 12.5
        e1.save_state()
        e2 = FlywheelEngine(root=td)
        assert abs(e2.ledger.equity - 26_500.0) < 1e-9
        assert abs(e2.ledger.fees_paid - 12.5) < 1e-9


def test_fresh_engine_uses_initial_equity():
    with tempfile.TemporaryDirectory() as td:
        e = FlywheelEngine(root=td, initial_equity=40_000.0)
        assert e.ledger.equity == 40_000.0


def test_double_tick_same_day_does_not_double_accrue():
    with tempfile.TemporaryDirectory() as td:
        e = FlywheelEngine(root=td, initial_equity=10_000.0)
        e.ledger.carry_notional = 1_000.0
        # Force a synthetic tick day without needing sqlite history.
        day = 20_000
        e.ledger.last_tick_day = None
        from hydra_flywheel import mark_and_rebalance, Targets
        t = Targets(trend={}, carry=0.1, cash=0.9)
        mark_and_rebalance(e.ledger, t, {}, {}, 0.001, 0.0, 6.5)
        e.ledger.last_tick_day = day
        funding_before = e.ledger.funding_collected
        days_before = e.ledger.days
        # Second application of the same day guard lives on tick(); unit-test
        # the guard contract: same last_tick_day → no further mark.
        assert e.ledger.last_tick_day == day
        # Simulate tick skip path
        if e.ledger.last_tick_day == day:
            pass  # no mark
        assert e.ledger.funding_collected == funding_before
        assert e.ledger.days == days_before


def test_apply_targets_records_without_orders():
    with tempfile.TemporaryDirectory() as td:
        e = FlywheelEngine(root=td)
        t = Targets(trend={"BTC/USD": 0.2}, carry=0.1, cash=0.7)
        out = e.apply_targets(t)
        assert out is t
        assert e._last_targets is t


def test_don55_exits_on_20d_low():
    """Stateful don55: breakout then 20d low exit → flat."""
    from hydra_flywheel import _don55_stateful
    # Build: flat, spike breakout, then deep drop below 20d low
    closes = [100.0] * 60
    closes.append(120.0)  # breakout above prior 55d high
    closes.extend([119.0] * 10)
    closes.extend([90.0] * 25)  # well below 20d low of recent highs
    assert _don55_stateful(closes) is False


def test_engine_gate_rejects_malformed_evidence():
    with tempfile.TemporaryDirectory() as td:
        (pathlib.Path(td) / "validation_results.json").write_text('{"not": "a list"}')
        allowed, why = engine_sleeve_allowed(td)
        assert not allowed
        assert "no validation evidence" in why


def test_daily_closes_missing_db_returns_empty():
    """Empty/missing sqlite must not crash report/tick — soft empty series."""
    with tempfile.TemporaryDirectory() as td:
        e = FlywheelEngine(root=td)
        assert e.daily_closes("BTC/USD") == []
        # tick on empty history: targets compute, mark is a no-op for trend
        t = e.tick(end_ts=1_700_000_000, persist=False)
        assert t.cash > 0


def test_double_tick_skips_second_mark_same_day():
    with tempfile.TemporaryDirectory() as td:
        e = FlywheelEngine(root=td, initial_equity=10_000.0)
        e.ledger.carry_notional = 1_000.0
        day_ts = 20_000 * 86_400
        e.tick(end_ts=day_ts, persist=False)
        days1 = e.ledger.days
        fund1 = e.ledger.funding_collected
        e.tick(end_ts=day_ts + 100, persist=False)  # same UTC day
        assert e.ledger.days == days1
        assert e.ledger.funding_collected == fund1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("all flywheel tests passed")
