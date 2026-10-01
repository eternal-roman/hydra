"""Daily trend sleeve (HYDRA_TREND_SLEEVE=1, default OFF).

The sleeve holds the daily ensemble itself: long while the score on
COMPLETED daily closes is >= 0.6, flat otherwise, vol-targeted at entry.
It never reads the 1h signal generator or the 1h hold-through rails.
"""
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_engine import (  # noqa: E402
    HydraEngine, Strategy, is_protected_flatten_reason,
)

DAY = 86400
START_DAY = 19_000  # 2022-01-08 UTC


def _uptrend(n=300, drift=0.004, amp=0.03):
    """Deterministic uptrend; the oscillation gives ~68% annualized vol."""
    return [100.0 * math.exp(drift * i + amp * math.sin(2.0 * i)) for i in range(n)]


def _then_down(closes, n=140, drift=-0.008, amp=0.03):
    k0, base = len(closes), closes[-1]
    return list(closes) + [
        base * math.exp(drift * (j + 1)
                        + amp * (math.sin(2.0 * (k0 + j + 1)) - math.sin(2.0 * k0)))
        for j in range(n)
    ]


def _engine(daily, **kw):
    kw.setdefault("trend_sleeve", True)
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", **kw)
    eng.seed_daily_closes([{"timestamp": (START_DAY + i) * DAY, "close": c}
                           for i, c in enumerate(daily)])
    return eng


def _bar(eng, day, hour, close, spread=0.002):
    """One 1h bar on seeded-day index `day` (len(daily) = the first unseeded day)."""
    eng.ingest_candle({"open": close, "high": close * (1 + spread),
                       "low": close * (1 - spread), "close": close,
                       "volume": 10.0, "timestamp": (START_DAY + day) * DAY + hour * 3600})


def test_default_off_and_explicit_on_values(monkeypatch):
    monkeypatch.delenv("HYDRA_TREND_SLEEVE", raising=False)
    assert HydraEngine(asset="BTC/USD").trend_sleeve is False
    for raw, expected in (("1", True), ("true", True), (" ON ", True), ("yes", True),
                          ("0", False), ("", False), ("enabled", False)):
        monkeypatch.setenv("HYDRA_TREND_SLEEVE", raw)
        assert HydraEngine(asset="BTC/USD").trend_sleeve is expected, raw
    monkeypatch.setenv("HYDRA_TREND_SLEEVE", "1")
    assert HydraEngine(asset="BTC/USD", trend_sleeve=False).trend_sleeve is False


def test_warming_sleeve_holds_and_never_trades():
    daily = _uptrend(150)
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    state = eng.tick()
    assert eng.sleeve_trend_score() is None
    assert state["signal"]["action"] == "HOLD"
    assert "warming" in state["signal"]["reason"]
    assert eng.position.size == 0.0 and not eng.trades


def test_decides_on_completed_days_only():
    """A crash on the forming day moves the overlay's running score but
    not the sleeve's; the sleeve sees it once the day has closed."""
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    before = eng.sleeve_trend_score()
    assert before == 1.0
    crash = daily[-1] * 0.5
    _bar(eng, len(daily), 5, crash)  # same UTC day, still forming
    assert eng.daily_trend_score() < before  # the running close moves the overlay
    assert eng.sleeve_trend_score() == before
    _bar(eng, len(daily) + 1, 0, crash)  # the crash day is now complete
    assert eng.sleeve_trend_score() < before


def test_enters_long_sized_by_vol_target_and_cap():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    vm = eng._sleeve_vol_multiplier()
    assert 0.2 <= vm < 1.0  # ~68% annualized vol vs a 30% target
    state = eng.tick()
    trade = eng.trades[-1]
    assert trade.action == "BUY"
    assert trade.strategy == Strategy.TREND.value
    assert trade.reason.startswith("TREND_SLEEVE:enter")
    assert state["strategy"] == "TREND"
    expected_value = 1000.0 * eng.sizer.max_position_pct * vm
    assert trade.value == pytest.approx(expected_value, rel=1e-9)
    assert state["trend_sleeve"] == {"enabled": True, "score": 1.0}


def test_holds_through_intraday_noise_without_1h_rails():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    held = eng.position.size
    assert held > 0
    px = daily[-1]
    for hour in range(1, 13):  # -1.5%/h dump with wide ranges, same UTC day
        px *= 0.985
        _bar(eng, len(daily), hour, px, spread=0.03)
        state = eng.tick()
        assert state["signal"]["action"] == "HOLD"
        assert "hold_long" in state["signal"]["reason"]
    assert eng.position.size == held
    assert [t.action for t in eng.trades] == ["BUY"]


def test_exits_when_the_completed_day_score_drops():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    assert eng.position.size > 0
    falling = _then_down(daily)[len(daily):]
    day = len(daily)
    exits = []
    for close in falling:
        _bar(eng, day, 23, close)
        day += 1
        _bar(eng, day, 0, close)
        state = eng.tick()
        if eng.trades[-1].action == "SELL":
            exits.append(state)
            break
    assert exits, "the sleeve never exited a 140-day decline"
    sell = eng.trades[-1]
    assert sell.reason.startswith("TREND_SLEEVE:exit")
    assert sell.strategy == Strategy.TREND.value
    assert eng.position.size == 0.0
    assert eng.sleeve_wants_long() is False


def test_sleeve_exit_is_a_protected_flatten():
    assert is_protected_flatten_reason("TREND_SLEEVE:exit|score=0.4")
    assert is_protected_flatten_reason("[QFE PROFIT EXIT] TREND_SLEEVE:exit|score=0.2")
    assert not is_protected_flatten_reason("TREND_SLEEVE:hold_long|score=1.0")
    assert not is_protected_flatten_reason("TREND_SLEEVE:enter|score=1.0")


def test_external_buy_needs_the_sleeve_long():
    flat_daily = _then_down(_uptrend())
    eng = _engine(flat_daily)
    _bar(eng, len(flat_daily), 0, flat_daily[-1])
    assert eng.sleeve_wants_long() is False
    assert eng.execute_signal("BUY", 0.95, "[CROSS-PAIR] confluence", "MOMENTUM") is None
    assert eng.position.size == 0.0

    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    trade = eng.execute_signal("BUY", 1.0, "TREND_SLEEVE:enter|score=1.0", "MOMENTUM")
    assert trade is not None and trade.strategy == Strategy.TREND.value


def test_external_buy_cannot_top_up_a_held_sleeve():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    held = eng.position.size
    assert held > 0
    assert eng.execute_signal("BUY", 0.95, "[CROSS-PAIR] BTC recovering", "MOMENTUM") is None
    assert eng.position.size == held


def test_external_1h_sell_cannot_cut_a_long_sleeve():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    held = eng.position.size
    assert eng.execute_signal("SELL", 0.9, "[CROSS-PAIR] BTC dumping", "DEFENSIVE") is None
    assert eng.execute_signal("SELL", 0.9, "Momentum fading: extreme overbought", "MOMENTUM") is None
    assert eng.position.size == held


def test_breaker_flatten_still_sells_a_long_sleeve():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    assert eng.position.size > 0
    eng.halted = True
    eng.halt_reason = "CIRCUIT BREAKER: drawdown 15.2% >= 15.0% limit"
    state = eng.tick()
    assert eng.position.size == 0.0
    assert eng.trades[-1].action == "SELL"
    assert eng.trades[-1].reason.startswith("HALT FLATTEN")
    assert state["signal"]["action"] == "HOLD"


def test_halt_flatten_through_execute_signal_keeps_its_label():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    eng.tick()
    eng.halted = True
    trade = eng.execute_signal("SELL", 1.0, "HALT FLATTEN: CIRCUIT BREAKER", "DEFENSIVE")
    assert trade is not None and trade.strategy == Strategy.DEFENSIVE.value


def test_size_multiplier_scales_the_sleeve_entry():
    daily = _uptrend()
    full = _engine(daily)
    _bar(full, len(daily), 0, daily[-1])
    a = full.execute_signal("BUY", 1.0, "TREND_SLEEVE:enter", "TREND", size_multiplier=1.0)
    half = _engine(daily)
    _bar(half, len(daily), 0, daily[-1])
    b = half.execute_signal("BUY", 1.0, "TREND_SLEEVE:enter", "TREND", size_multiplier=0.5)
    assert b.amount == pytest.approx(a.amount * 0.5, rel=1e-9)
    zero = _engine(daily)
    _bar(zero, len(daily), 0, daily[-1])
    assert zero.execute_signal("BUY", 1.0, "TREND_SLEEVE:enter", "TREND",
                               size_multiplier=0.0) is None


def test_gross_cap_binds_a_sleeve_entry():
    """Above 1.0 the multiplier cannot push past max_position_pct (PR-B)."""
    # ~23% annualized: under the 30% target (multiplier 1.0) and still
    # two daily sigmas above the 2% friction hurdle.
    daily = _uptrend(amp=0.01)
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    assert eng._sleeve_vol_multiplier() == 1.0
    trade = eng.execute_signal("BUY", 1.0, "TREND_SLEEVE:enter", "TREND", size_multiplier=1.5)
    assert trade.value == pytest.approx(1000.0 * eng.sizer.max_position_pct, rel=1e-9)


def test_trend_friction_proxy_is_two_daily_sigmas():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    sig = eng._trend_sleeve_signal()
    window = daily[-22:]
    rets = [math.log(b / a) for a, b in zip(window, window[1:])]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    assert eng._expected_move_pct(sig, daily[-1]) == pytest.approx(200.0 * sd)


def test_generate_only_does_not_trade():
    daily = _uptrend()
    eng = _engine(daily)
    _bar(eng, len(daily), 0, daily[-1])
    state = eng.tick(generate_only=True)
    assert state["signal"]["action"] == "BUY"
    assert not eng.trades and eng.position.size == 0.0


def test_sleeve_off_engine_is_unchanged(monkeypatch):
    monkeypatch.delenv("HYDRA_TREND_SLEEVE", raising=False)
    daily = _uptrend()
    eng = _engine(daily, trend_sleeve=None)
    _bar(eng, len(daily), 0, daily[-1])
    state = eng.tick()
    assert state["strategy"] != "TREND"
    assert state["trend_sleeve"] == {"enabled": False, "score": None}
    assert not eng.trades or eng.trades[-1].strategy != Strategy.TREND.value


def test_no_bars_yet_treats_the_last_seeded_day_as_forming():
    daily = _uptrend()
    eng = _engine(daily)
    assert eng._completed_daily_closes() == daily[:-1]
    _bar(eng, len(daily), 0, daily[-1])
    assert eng._completed_daily_closes() == daily


def test_backtest_runner_trades_the_sleeve_end_to_end(tmp_path, monkeypatch):
    """Sqlite pre-window seed warms the sleeve; the runner's post-only
    filler completes the entry; trade labels survive the round trip."""
    from hydra_backtest import BacktestConfig, BacktestRunner
    from hydra_history_store import CandleRow, HistoryStore
    import json

    monkeypatch.setenv("HYDRA_TREND_SLEEVE", "1")
    db = str(tmp_path / "h.sqlite")
    store = HistoryStore(db)
    rng = random.Random(3)
    rows, px = [], 100.0
    t0 = START_DAY * DAY
    hours = 24 * 330
    for h in range(hours):
        px *= math.exp(0.004 / 24 + rng.gauss(0.0, 0.03 / math.sqrt(24)))
        rows.append(CandleRow("BTC/USD", 3600, t0 + h * 3600, px, px * 1.002,
                              px * 0.998, px, 10.0, "kraken_archive"))
    store.upsert_candles(rows)
    start_ts = t0 + 300 * DAY
    cfg = BacktestConfig(
        name="sleeve-smoke", pairs=("BTC/USD",), initial_balance_per_pair=1000.0,
        data_source="sqlite", coordinator_enabled=False,
        data_source_params_json=json.dumps({"db_path": db, "grain_sec": 3600,
                                            "start_ts": start_ts,
                                            "end_ts": t0 + (hours - 1) * 3600}),
    )
    runner = BacktestRunner(cfg)
    assert runner.engines["BTC/USD"].trend_sleeve is True
    assert runner.engines["BTC/USD"].sleeve_trend_score() is not None
    result = runner.run()
    assert result.status == "complete", result.errors
    buys = [t for t in result.trade_log if t.get("side") == "BUY"]
    assert buys, result.trade_log[:3]
    assert buys[0]["strategy"] == "TREND"
    assert buys[0]["signal_reason"].startswith("TREND_SLEEVE:enter")
    assert buys[0]["confidence"] > 0.0
    assert runner.engines["BTC/USD"].trades[0].strategy == "TREND"
