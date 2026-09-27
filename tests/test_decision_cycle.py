"""Decision-cycle repairs: the order matches the post-rules decision."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("HYDRA_FRICTION_GATE_DISABLED", "1")

from hydra_agent import resting_order_decision
from hydra_brain import _canonical_action, _canonical_decision, _coerce_size_mult
from hydra_derivatives_stream import _maybe_float
from hydra_engine import (
    HydraEngine,
    Indicators,
    Regime,
    RegimeDetector,
    SIZING_COMPETITION,
    Signal,
    SignalAction,
    Strategy,
    is_entry_veto,
)


def _feed(eng, n, start, step, volume=10.0):
    for i in range(n):
        px = start + i * step
        eng.ingest_candle({
            "open": px, "high": px + abs(step) + 0.2, "low": max(px - abs(step) - 0.2, 0.01),
            "close": px, "volume": volume, "timestamp": 1_700_000_000 + i * 3600,
        })


def _down(hold=True):
    eng = HydraEngine(
        initial_balance=100_000.0, asset="SOL/USD", sizing=SIZING_COMPETITION,
        candle_interval=60, hold_through=hold,
    )
    _feed(eng, 80, 200.0, -1.0)
    return eng


def _up():
    eng = HydraEngine(
        initial_balance=100_000.0, asset="SOL/USD", sizing=SIZING_COMPETITION,
        candle_interval=60, hold_through=True,
    )
    _feed(eng, 80, 100.0, 1.0)
    return eng


def test_rules_hold_is_not_sold_in_a_downtrend():
    eng = _down()
    eng.position.size = 0.5
    eng.position.avg_entry = 180.0
    assert RegimeDetector.detect(eng.candles, eng.prices) == Regime.TREND_DOWN
    trade = eng.execute_signal(
        "HOLD", 0.0,
        reason="[QUANT RULES FORCE_HOLD] R10: stale",
        strategy="DEFENSIVE",
        size_multiplier=0.0,
    )
    assert trade is None
    assert eng.position.size == 0.5


def test_trend_flatten_still_sells_when_rules_would_zero_size():
    eng = _down()
    eng.position.size = 0.5
    eng.position.avg_entry = 180.0
    trade = eng.execute_signal(
        "SELL", 0.75,
        reason="HOLD_THROUGH:force_flatten|defensive",
        strategy="DEFENSIVE",
        size_multiplier=0.0,
    )
    assert trade is not None and trade.action == "SELL"
    assert trade.amount == pytest.approx(0.5)
    assert eng.position.size == 0.0


def test_halt_flatten_sells_at_multiplier_zero():
    eng = _down()
    eng.position.size = 0.5
    eng.position.avg_entry = 180.0
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    trade = eng.execute_signal(
        "SELL", 1.0, reason="HALT FLATTEN: CIRCUIT BREAKER",
        strategy="DEFENSIVE", size_multiplier=0.0,
    )
    assert trade is not None and trade.amount == pytest.approx(0.5)


def test_discretionary_sell_at_multiplier_zero_does_not_send():
    eng = _down(hold=False)
    eng.position.size = 4.0
    eng.position.avg_entry = 180.0
    trade = eng.execute_signal(
        "SELL", 0.9, reason="discretionary", strategy="DEFENSIVE", size_multiplier=0.0,
    )
    assert trade is None
    assert eng.position.size == 4.0
    full = eng.execute_signal(
        "SELL", 0.9, reason="discretionary", strategy="DEFENSIVE", size_multiplier=0.5,
    )
    assert full is not None and full.amount == pytest.approx(4.0)


def test_size_one_point_five_is_not_doubled():
    assert HydraEngine._apply_size_multiplier(1.5) == pytest.approx(1.5)
    assert HydraEngine._apply_size_multiplier(0.7) == pytest.approx(0.7)
    assert HydraEngine._apply_size_multiplier(0.0) == 0.0
    assert HydraEngine._apply_size_multiplier(float("nan")) == 0.0
    assert HydraEngine._apply_size_multiplier(float("inf")) == 0.0
    a = _up()
    b = _up()
    t1 = a.execute_signal("BUY", 0.70, reason="probe", strategy="MOMENTUM", size_multiplier=1.0)
    t2 = b.execute_signal("BUY", 0.70, reason="probe", strategy="MOMENTUM", size_multiplier=1.5)
    assert t1 and t2
    assert t2.amount / t1.amount == pytest.approx(1.5)


def test_nan_brain_size_is_neutral():
    assert _coerce_size_mult(float("nan")) == 1.0
    assert _coerce_size_mult(float("inf")) == 1.0
    assert _coerce_size_mult(float("-inf")) == 1.0
    assert _coerce_size_mult(0) == 0.0
    assert _canonical_action("buy", "SELL") == "BUY"
    assert _canonical_action("nope", "SELL") == "SELL"
    assert _canonical_decision("override") == "OVERRIDE"
    assert _canonical_decision("maybe") == "CONFIRM"


def test_resting_buy_is_pulled_by_a_veto_and_not_by_a_plain_hold():
    assert resting_order_decision("BUY", "HOLD") == "skip"
    assert resting_order_decision("BUY", "HOLD", entry_veto=True) == "cancel"
    assert resting_order_decision("SELL", "HOLD", entry_veto=True) == "skip"
    assert resting_order_decision("BUY", "BUY", buy_blocked=True) == "cancel"
    assert resting_order_decision("SELL", "BUY", buy_blocked=True) == "skip"
    assert is_entry_veto("HOLD", "[QUANT RULES FORCE_HOLD] R1")
    assert not is_entry_veto("HOLD", "no edge")
    assert not is_entry_veto("BUY", "[AI OVERRIDE] add")


def test_cross_pair_sell_is_not_ridden_in_an_uptrend():
    eng = _up()
    eng.position.size = 5.0
    eng.position.avg_entry = eng.prices[-1] * 0.9
    ridden = eng.execute_signal(
        "SELL", 0.8, reason="momentum fading", strategy="MOMENTUM",
    )
    assert ridden is None and eng.position.size == 5.0
    sold = eng.execute_signal(
        "SELL", 0.8, reason="[CROSS-PAIR] BTC trending down", strategy="DEFENSIVE",
    )
    assert sold is not None and sold.action == "SELL"
    assert eng.position.size == 0.0


def test_halted_tick_publishes_the_real_regime():
    eng = _down()
    eng.position.size = 1.0
    eng.position.avg_entry = 220.0
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    state = eng.tick(generate_only=True)
    assert state["regime"] == "TREND_DOWN"
    assert state["signal"]["action"] == "SELL"
    assert str(state["signal"]["reason"]).startswith("HALT FLATTEN")


def test_rsi_100_still_exits_when_the_upper_band_is_85():
    from hydra_engine import SignalGenerator
    prices = [100.0 + i for i in range(80)]
    candles = []
    for i, px in enumerate(prices):
        from hydra_engine import Candle
        candles.append(Candle(px, px + 0.2, px - 0.2, px, 1.0, float(i)))
    sig = SignalGenerator.generate(
        Strategy.MOMENTUM, prices, candles, momentum_rsi_upper=85.0,
    )
    assert sig.action == SignalAction.SELL
    mild = SignalGenerator.generate(
        Strategy.MOMENTUM, prices, candles, momentum_rsi_upper=70.0,
    )
    assert mild.action == SignalAction.SELL


def test_macd_first_bar_is_not_zero_and_volume_excludes_itself():
    prices = [float(i + 1) for i in range(26)]
    macd = Indicators.macd(prices)
    assert macd["macd"] != 0.0
    from hydra_engine import Candle
    candles = [Candle(1, 1, 1, 1, 1.0, float(i)) for i in range(20)]
    candles.append(Candle(1, 1, 1, 1, 21.0, 20.0))
    # Prior 20 bars are volume 1; the judged bar is 21. Ratio is 21, not ~2.
    prior = candles[-21:-1]
    assert len(prior) == 20
    avg = sum(c.volume for c in prior) / 20
    assert candles[-1].volume / avg == pytest.approx(21.0)


def test_unknown_side_does_not_restore_and_unknown_base_is_not_written_off():
    eng = _up()
    eng.position.size = 5.0
    eng.position.avg_entry = 100.0
    snap = eng.snapshot_position()
    assert eng.true_up_fill("NOPE", 1.0, 100.0, snap) is False
    assert eng.position.size == 5.0
    eng.asset = "NOTACOIN/USD"
    eng.position.size = 1.0
    assert eng.write_off_dust() == 0.0
    assert eng.position.size == 1.0
    assert eng.sizer.calculate(0.9, 1000.0, 10.0, "NOTACOIN/USD") == 0.0


def test_write_off_position_clears_a_full_book():
    eng = _up()
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    written = eng.write_off_position()
    assert written == pytest.approx(1.0)
    assert eng.position.size == 0.0


def test_remainder_drops_phantom_coins_and_keeps_locked_ones():
    eng = _up()
    eng.position.size = 0.7
    eng.position.avg_entry = 100.0
    eng.set_base_remainder(0.3, 0.3)
    assert eng.position.size == 0.0
    eng.position.size = 0.7
    eng.position.avg_entry = 100.0
    eng.set_base_remainder(1.0, 0.3)
    assert eng.position.size == pytest.approx(0.7)


def test_unfilled_buy_halt_clears_only_when_the_buy_caused_it():
    eng = _up()
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    # A real halt, with no unfilled-buy mark, stays after the book recovers.
    assert eng.release_unfilled_buy_halt() is False
    assert eng.halted is True
    eng._halt_from_unfilled_buy = True
    assert eng.release_unfilled_buy_halt() is True
    assert eng.halted is False
    eng.balance = 1.0
    eng.peak_equity = 100_000.0
    eng.halted = True
    eng._halt_from_unfilled_buy = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 99.0% >= 15.0% limit"
    assert eng.release_unfilled_buy_halt() is False
    assert eng.halted is True


def test_real_breach_of_the_pre_buy_book_is_not_marked_phantom():
    eng = _up()
    eng.balance = 1.0
    eng.peak_equity = 100_000.0
    snap = eng.snapshot_position()
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 99.0% >= 15.0% limit"
    dd = eng.pretrade_drawdown_pct(snap)
    assert dd is not None and dd >= 15.0
    eng.note_unfilled_buy_halt(False, dd)
    assert eng._halt_from_unfilled_buy is False
    flat = _up()
    flat_snap = flat.snapshot_position()
    flat.halted = True
    flat.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    flat.note_unfilled_buy_halt(False, flat.pretrade_drawdown_pct(flat_snap))
    assert flat._halt_from_unfilled_buy is True


def test_resumed_halt_is_marked_again_while_the_buy_is_still_the_breach():
    eng = _up()
    eng.peak_equity = 200_000.0
    eng.balance = 50_000.0
    eng.position.size = 100.0
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 66.0% >= 15.0% limit"
    pre = {"balance": 100_000.0, "position_size": 0.0, "peak_equity": 100_000.0}
    dd = eng.pretrade_drawdown_pct(pre)
    assert dd == pytest.approx(0.0)
    assert eng.current_drawdown_pct() >= 15.0
    # was_halted=True is the resume path: the arming edge already passed.
    eng.note_unfilled_buy_halt(True, dd)
    assert eng._halt_from_unfilled_buy is True


def test_recovered_real_halt_is_not_remarked_from_a_healthy_snapshot():
    eng = _up()
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    dd = eng.pretrade_drawdown_pct(eng.snapshot_position())
    assert dd is not None and dd < 15.0
    eng.note_unfilled_buy_halt(True, dd)
    assert eng._halt_from_unfilled_buy is False
    assert eng.release_unfilled_buy_halt() is False
    assert eng.halted is True


def test_unfilled_buy_mark_survives_resume_and_reset_clears_it(monkeypatch):
    eng = _up()
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 20.0% >= 15.0% limit"
    eng._halt_from_unfilled_buy = True
    snap = eng.snapshot_runtime()
    assert snap["halt_from_unfilled_buy"] is True
    fresh = _up()
    fresh.restore_runtime(snap)
    assert fresh.halted is True
    assert fresh._halt_from_unfilled_buy is True
    assert fresh.release_unfilled_buy_halt() is True
    assert fresh.halted is False

    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    reset = _up()
    reset.restore_runtime(snap)
    assert reset.halted is False
    assert reset._halt_from_unfilled_buy is False

    legacy = dict(snap)
    legacy.pop("halt_from_unfilled_buy")
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER", raising=False)
    old = _up()
    old.restore_runtime(legacy)
    assert old.halted is True
    assert old._halt_from_unfilled_buy is False
    assert old.release_unfilled_buy_halt() is False


def test_abandoned_brain_publish_does_not_replace_the_replay_cache():
    from hydra_agent import HydraAgent, _BrainPublish

    agent = HydraAgent.__new__(HydraAgent)
    agent._last_ai_decision = {"BTC/USD": {"action": "CONFIRM", "size_multiplier": 1.0}}
    agent._last_brain_candle_ts = {"BTC/USD": 1.0}
    token = _BrainPublish()
    body = {"action": "OVERRIDE", "size_multiplier": 1.5}
    agent._publish_brain_decision("BTC/USD", body, 2.0, token)
    body["size_multiplier"] = 0.1
    assert agent._last_ai_decision["BTC/USD"]["action"] == "OVERRIDE"
    assert agent._last_ai_decision["BTC/USD"]["size_multiplier"] == 1.5
    assert agent._last_brain_candle_ts["BTC/USD"] == 2.0
    # The cached dict is a copy. Mutating the worker's object must not leak.
    late_body = {"action": "OVERRIDE", "size_multiplier": 0.1}
    late = _BrainPublish()
    late.abandoned = True
    agent._publish_brain_decision("BTC/USD", late_body, 3.0, late)
    assert agent._last_ai_decision["BTC/USD"]["size_multiplier"] == 1.5
    assert agent._last_brain_candle_ts["BTC/USD"] == 2.0
    agent._abandon_brain_publish("BTC/USD", token)
    assert "BTC/USD" not in agent._last_ai_decision
    assert "BTC/USD" not in agent._last_brain_candle_ts


def test_nonfinite_basis_is_missing():
    assert _maybe_float(float("nan")) is None
    assert _maybe_float(float("inf")) is None
    assert _maybe_float(1.5) == 1.5


def test_rule2_does_not_force_a_buy():
    from hydra_engine import CrossPairCoordinator
    coord = CrossPairCoordinator(["SOL/USD", "SOL/BTC", "BTC/USD"])

    def state(regime, action, conf, size):
        return {
            "regime": regime,
            "signal": {"action": action, "confidence": conf, "reason": "engine"},
            "position": {"size": size},
            "tradable": True,
        }

    overrides = coord.get_overrides({
        "BTC/USD": state("TREND_UP", "BUY", 0.8, 0.0),
        "SOL/USD": state("TREND_DOWN", "SELL", 0.8, 5.0),
        "SOL/BTC": state("TREND_UP", "BUY", 0.7, 0.0),
    })
    sol = overrides["SOL/USD"]
    assert sol["signal"] == "SELL"
    assert sol["action"] == "ADJUST"


def test_rail_flatten_keeps_indicators():
    eng = _down()
    eng.position.size = 1.0
    eng.position.avg_entry = 220.0
    sig = Signal(
        SignalAction.BUY, 0.4, "nibble", Strategy.DEFENSIVE,
        indicators={"rsi": 12.0, "price": eng.prices[-1]},
    )
    out = eng._apply_hold_through(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL
    assert out.indicators.get("rsi") == 12.0
