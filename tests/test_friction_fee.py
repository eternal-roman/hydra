"""Friction expectancy gate + fee-true accounting tests (v2.27).

Gate: a BUY whose strategy-implied expected move cannot clear
2 x round-trip friction is skipped (entries only; exits never gated;
fails open on missing indicators). Fee: confirmed fills debit
lifecycle.fee_quote from engine balance exactly once.
"""
import os
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import HydraEngine, Signal, SignalAction, Strategy
from hydra_agent import HydraAgent


HURDLE = HydraEngine.FRICTION_HURDLE_MULT * HydraEngine.ROUND_TRIP_FRICTION_PCT


def _engine(balance=1_000.0):
    e = HydraEngine(initial_balance=balance, asset="SOL/USD")
    e.prices.append(100.0)
    return e


def _buy(strategy, indicators, confidence=0.80):
    return Signal(SignalAction.BUY, confidence, "test", strategy,
                  indicators=indicators)


def setup_function(_fn=None):
    os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)
    os.environ.pop("HYDRA_FEE_DEDUCTION_DISABLED", None)


# ── friction gate ────────────────────────────────────────────────────────

def test_thin_mean_reversion_buy_is_skipped():
    e = _engine()
    # bb middle only 0.3% above price — under the 0.84% hurdle
    sig = _buy(Strategy.MEAN_REVERSION,
               {"price": 100.0, "bb_middle": 100.3, "atr_pct": 2.0})
    assert e._maybe_execute(sig) is None
    assert e.friction_skips == 1
    assert e.balance == 1_000.0  # nothing committed


def test_wide_mean_reversion_buy_executes():
    e = _engine()
    sig = _buy(Strategy.MEAN_REVERSION,
               {"price": 100.0, "bb_middle": 103.0, "atr_pct": 2.0})
    trade = e._maybe_execute(sig)
    assert trade is not None and trade.action == "BUY"
    assert e.friction_skips == 0


def test_low_atr_momentum_buy_is_skipped():
    e = _engine()
    sig = _buy(Strategy.MOMENTUM, {"price": 100.0, "atr_pct": 0.2})
    assert e._maybe_execute(sig) is None  # 2 x 0.2 = 0.4 < hurdle
    assert e.friction_skips == 1


def test_high_atr_momentum_buy_executes():
    e = _engine()
    sig = _buy(Strategy.MOMENTUM, {"price": 100.0, "atr_pct": 1.0})
    assert e._maybe_execute(sig) is not None


def test_gate_fails_open_on_missing_indicators():
    e = _engine()
    sig = _buy(Strategy.MOMENTUM, {})
    assert e._maybe_execute(sig) is not None


def test_sells_are_never_gated():
    e = _engine()
    e.position.size = 5.0
    e.position.avg_entry = 90.0
    # expected move far under the hurdle — must still exit
    sig = Signal(SignalAction.SELL, 0.80, "test", Strategy.MEAN_REVERSION,
                 indicators={"price": 100.0, "bb_middle": 100.1,
                             "atr_pct": 0.05})
    trade = e._maybe_execute(sig)
    assert trade is not None and trade.action == "SELL"
    assert e.friction_skips == 0


def test_kill_switch_disables_gate():
    os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"
    try:
        e = _engine()
        sig = _buy(Strategy.MOMENTUM, {"price": 100.0, "atr_pct": 0.2})
        assert e._maybe_execute(sig) is not None
        assert e.friction_skips == 0
    finally:
        del os.environ["HYDRA_FRICTION_GATE_DISABLED"]


def test_expected_move_mean_reversion_targets_bb_middle():
    e = _engine()
    sig = _buy(Strategy.GRID, {"price": 100.0, "bb_middle": 102.0})
    assert abs(e._expected_move_pct(sig, 100.0) - 2.0) < 1e-9


def test_expected_move_trend_uses_2x_atr():
    e = _engine()
    sig = _buy(Strategy.DEFENSIVE, {"price": 100.0, "atr_pct": 0.7})
    assert abs(e._expected_move_pct(sig, 100.0) - 1.4) < 1e-9


def test_execute_signal_path_is_gated():
    """Regression (v2.27): execute_signal() builds Signals WITHOUT indicators
    (the brain/coordinator path used by the live agent and the backtest), so
    the gate must recompute expected move from engine history — the first
    implementation failed open here and 0 skips fired on real 15m data."""
    from hydra_engine import Candle
    e = _engine()
    e.prices.clear()
    for i in range(60):  # dead-calm tape: ATR ~0.05% of price
        px = 100.0 + 0.01 * (i % 2)
        e.candles.append(Candle(px, px + 0.05, px - 0.05, px, 10.0, float(i)))
        e.prices.append(px)
    assert e.execute_signal("BUY", 0.9, "brain says buy", "MOMENTUM") is None
    assert e.friction_skips == 1
    # exits still ungated on the same path
    e.position.size = 1.0
    e.position.avg_entry = 90.0
    assert e.execute_signal("SELL", 0.9, "exit", "MOMENTUM") is not None


# ── fee-true accounting ──────────────────────────────────────────────────

def _entry(fee):
    return {"lifecycle": {"state": "FILLED", "fee_quote": fee}}


def _deduct(engine, entry):
    # _deduct_fill_fee never touches self — invoke with a dummy receiver
    HydraAgent._deduct_fill_fee(SimpleNamespace(), engine, entry)


def test_fee_debited_once():
    eng = SimpleNamespace(balance=100.0)
    entry = _entry(0.16)
    _deduct(eng, entry)
    assert abs(eng.balance - 99.84) < 1e-9
    assert entry["lifecycle"]["fee_applied"] is True
    _deduct(eng, entry)  # idempotent: revisiting must not double-debit
    assert abs(eng.balance - 99.84) < 1e-9


def test_fee_zero_or_malformed_is_noop():
    eng = SimpleNamespace(balance=100.0)
    _deduct(eng, _entry(0.0))
    _deduct(eng, _entry("not-a-number"))
    _deduct(eng, {"lifecycle": None})
    _deduct(eng, {})
    _deduct(None, _entry(0.5))
    assert eng.balance == 100.0


def test_fee_kill_switch():
    os.environ["HYDRA_FEE_DEDUCTION_DISABLED"] = "1"
    try:
        eng = SimpleNamespace(balance=100.0)
        _deduct(eng, _entry(0.16))
        assert eng.balance == 100.0
    finally:
        del os.environ["HYDRA_FEE_DEDUCTION_DISABLED"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function()
            fn()
            print(f"  ok {name}")
    print("all friction/fee tests passed")
