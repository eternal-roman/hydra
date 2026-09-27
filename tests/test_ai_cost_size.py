"""BUY size tracks whether expected profit covers the decision's API cost."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine, Signal, SignalAction, Strategy


def _engine():
    e = HydraEngine(initial_balance=1_000.0, asset="SOL/USD", hold_through=False)
    e.prices.append(100.0)
    e.candle_interval = 15
    return e


def _buy():
    return Signal(
        SignalAction.BUY, 0.80, "test", Strategy.MOMENTUM,
        indicators={"price": 100.0, "atr_pct": 2.0},
    )


def test_factor_math():
    f = HydraEngine.ai_cost_size_factor
    assert f(None, 5) == 1.0
    assert f(10, 0) == 1.0
    assert f(4, 10) == 0.0
    assert abs(f(10, 2.5) - 0.75) < 1e-9


def test_cost_scales_the_buy_and_a_larger_cost_skips_it():
    os.environ["HYDRA_TREND_CONVICTION_SIZING"] = "0"
    try:
        full = _engine()._maybe_execute(_buy())
        assert full is not None
        # 2 x atr_pct, scored on the order that would actually be sent.
        profit = full.amount * full.price * (4.0 / 100.0)
        half_cost = profit / 2.0
        e = _engine()
        scaled = e._maybe_execute(_buy(), decision_cost_usd=half_cost)
        assert scaled is not None
        assert abs(scaled.amount - full.amount * 0.5) / full.amount < 0.02
        skipped = _engine()._maybe_execute(_buy(), decision_cost_usd=profit + 1.0)
        assert skipped is None
    finally:
        os.environ.pop("HYDRA_TREND_CONVICTION_SIZING", None)


def test_sell_ignores_decision_cost():
    e = _engine()
    e.position.size = 1.0
    e.position.avg_entry = 90.0
    trade = e._maybe_execute(
        Signal(SignalAction.SELL, 0.9, "exit", Strategy.MOMENTUM,
               indicators={"price": 100.0, "atr_pct": 0.1}),
        decision_cost_usd=10_000.0,
    )
    assert trade is not None and trade.action == "SELL"
