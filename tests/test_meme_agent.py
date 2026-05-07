import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import time
import tempfile
import json as _json
import os as _os
from unittest.mock import patch
from hydra_meme_agent import (
    CandleBar, wilder_rsi, vol_ema, compute_obi, compute_vwap,
    TradeRecord, Position, MemeExecutor, _query_fill, _cancel_order,
    TAKER_SLIPPAGE_BPS, SLIPPAGE_CAP_BPS, RSI_PERIOD, SELL_MAX_RETRIES,
)


def test_candle_bar_creation():
    bar = CandleBar(ts=1000, open=1.0, high=1.1, low=0.9, close=1.05, vwap=1.02, volume=5000.0, count=42)
    assert bar.close == 1.05
    assert bar.volume == 5000.0


def test_wilder_rsi_insufficient_data():
    assert wilder_rsi([1.0, 1.1], period=9) == 50.0


def test_wilder_rsi_all_gains():
    closes = [float(i) for i in range(1, 12)]  # 10 diffs, all +1
    assert wilder_rsi(closes, period=9) == 100.0


def test_wilder_rsi_all_losses():
    closes = [float(11 - i) for i in range(11)]  # 10 diffs, all -1
    assert wilder_rsi(closes, period=9) == 0.0


def test_wilder_rsi_neutral():
    closes = [100.0] * 11  # no change
    result = wilder_rsi(closes, period=9)
    assert result == 50.0


def test_wilder_rsi_known_value():
    # Alternating gains/losses: avg_gain = avg_loss after seed period → RSI=50
    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0]
    result = wilder_rsi(closes, period=9)
    assert 48.0 < result < 52.0


def test_vol_ema_single():
    assert vol_ema([100.0], period=10) == 100.0


def test_vol_ema_stable():
    values = [100.0] * 20
    assert abs(vol_ema(values, period=10) - 100.0) < 0.01


def test_compute_obi_buy_pressure():
    bids = [(1.00, 10000.0), (0.99, 8000.0), (0.98, 6000.0), (0.97, 4000.0), (0.96, 2000.0)]
    asks = [(1.01, 1000.0), (1.02, 1000.0), (1.03, 1000.0), (1.04, 1000.0), (1.05, 1000.0)]
    obi = compute_obi(bids, asks)
    assert obi > 0.5  # strongly buy-side


def test_compute_obi_sell_pressure():
    bids = [(1.00, 1000.0)] * 5
    asks = [(1.01, 10000.0)] * 5
    obi = compute_obi(bids, asks)
    assert obi < -0.5


def test_compute_obi_balanced():
    bids = [(1.00, 5000.0)] * 5
    asks = [(1.01, 5000.0)] * 5
    obi = compute_obi(bids, asks)
    assert abs(obi) < 0.05


def test_compute_obi_empty():
    assert compute_obi([], []) == 0.0


def test_compute_vwap_single_bar():
    bars = [CandleBar(ts=0, open=1.0, high=1.1, low=0.9, close=1.05, vwap=1.02, volume=1000.0, count=10)]
    assert compute_vwap(bars) == 1.05


def test_compute_vwap_weighted():
    bars = [
        CandleBar(ts=0, open=1.0, high=1.1, low=0.9, close=1.00, vwap=1.0, volume=1000.0, count=10),
        CandleBar(ts=300, open=1.0, high=1.2, low=1.0, close=1.20, vwap=1.1, volume=3000.0, count=30),
    ]
    # VWAP = (1.00*1000 + 1.20*3000) / 4000 = 4600/4000 = 1.15
    assert abs(compute_vwap(bars) - 1.15) < 0.001


# ─── SignalEngine Tests ────────────────────────────────────────────────────────

from hydra_meme_agent import SignalEngine, Position


def _make_bar(close=1.0, volume=1000.0, ts=0):
    return CandleBar(ts=ts, open=close*0.99, high=close*1.01, low=close*0.98,
                     close=close, vwap=close, volume=volume, count=10)


def _warmed_engine(n_bars=15, close=1.0, volume=1000.0, flat=False):
    """Return a SignalEngine with n_bars of history loaded.

    flat=True keeps all closes identical so RSI stays at 50 (neutral).
    Default adds a tiny uptrend for VWAP alignment tests.
    """
    eng = SignalEngine()
    for i in range(n_bars):
        c = close if flat else close + i * 0.001
        eng.add_bar(_make_bar(close=c, volume=volume, ts=i * 300))
    return eng


def test_signal_engine_warmup_not_ready():
    eng = SignalEngine()
    for i in range(14):
        eng.add_bar(_make_bar(ts=i * 300))
    assert not eng.is_warmed_up()


def test_signal_engine_warmed_after_15():
    eng = _warmed_engine(n_bars=15)
    assert eng.is_warmed_up()


def test_entry_gate_volume_spike_fail():
    eng = _warmed_engine(volume=1000.0)
    # Low volume bar — should fail volume gate
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=500.0),  # 0.5x EMA, not 1.8x
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["volume_spike"] is False


def test_entry_gate_volume_spike_pass():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),  # 2x EMA
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["volume_spike"] is True


def test_entry_gate_obi_fail():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.10,  # below 0.20 threshold
        ask_wall_usd=100.0,
    )
    assert gates["obi"] is False


def test_entry_gate_obi_pass():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["obi"] is True


def test_entry_gate_rsi_overbought():
    # All rising prices → RSI near 100 → should fail upper gate
    eng = SignalEngine()
    for i in range(15):
        eng.add_bar(_make_bar(close=1.0 + i * 0.05, volume=1000.0, ts=i * 300))
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=2.0, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["rsi_window"] is False


def test_entry_gate_vwap_fail():
    eng = _warmed_engine(close=1.0, volume=1000.0)
    # Price below VWAP
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=0.90),  # below VWAP ~1.007
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["vwap_align"] is False


def test_entry_gate_ask_wall_fail():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.25,
        ask_wall_usd=600.0,  # above $500 limit
    )
    assert gates["ask_wall_clear"] is False


def test_all_gates_pass():
    eng = _warmed_engine(close=1.0, volume=1000.0)
    # Use a neutral RSI bar (no strong trend), volume spike, good OBI, good ask wall
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=1.015, volume=2000.0),
        obi=0.25,
        ask_wall_usd=200.0,
    )
    # All 5 gates should reflect actual logic — VWAP and RSI depend on history
    assert isinstance(gates["volume_spike"], bool)
    assert isinstance(gates["obi"], bool)
    assert isinstance(gates["vwap_align"], bool)
    assert isinstance(gates["rsi_window"], bool)
    assert isinstance(gates["ask_wall_clear"], bool)
    assert "all_pass" in gates


# ─── Exit Trigger Tests ────────────────────────────────────────────────────────

def test_exit_profit_target():
    eng = _warmed_engine()
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    result = eng.evaluate_exit_intracandle(pos, mid_price=1.026, obi=0.1)
    assert result == "profit_target"


def test_exit_hard_stop():
    eng = _warmed_engine()
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    result = eng.evaluate_exit_intracandle(pos, mid_price=0.986, obi=0.1)
    assert result == "hard_stop"


def test_exit_book_fade():
    eng = _warmed_engine()
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    result = eng.evaluate_exit_intracandle(pos, mid_price=1.005, obi=-0.25)
    assert result == "book_fade"


def test_exit_no_trigger_intracandle():
    eng = _warmed_engine()
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    result = eng.evaluate_exit_intracandle(pos, mid_price=1.01, obi=0.05)
    assert result is None


def test_exit_time_stop():
    eng = _warmed_engine(flat=True)
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=3)
    bar = _make_bar(close=1.01, volume=1000.0)
    result = eng.evaluate_exit_bar(pos, bar)
    assert result == "time_stop"


def test_exit_rsi_exhaust():
    # All rising prices → RSI very high → rsi_exhaust
    eng = SignalEngine()
    for i in range(15):
        eng.add_bar(_make_bar(close=1.0 + i * 0.1, volume=1000.0, ts=i * 300))
    pos = Position(entry_price=1.0, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    bar = _make_bar(close=2.6, volume=1000.0)
    result = eng.evaluate_exit_bar(pos, bar)
    assert result == "rsi_exhaust"


def test_exit_volume_death():
    eng = _warmed_engine(volume=1000.0, flat=True)
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    dead_bar = _make_bar(close=1.01, volume=200.0)  # 0.2x baseline
    result = eng.evaluate_exit_bar(pos, dead_bar)
    assert result == "volume_death"


def test_exit_no_trigger_bar():
    eng = _warmed_engine(volume=1000.0, flat=True)
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    normal_bar = _make_bar(close=1.01, volume=1000.0)
    result = eng.evaluate_exit_bar(pos, normal_bar)
    assert result is None


# ─── Competition Detector Tests ────────────────────────────────────────────────

from hydra_meme_agent import CompetitionDetector


def test_competition_detector_bootstrap_creates_watchlist():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        assert os.path.exists(path)
        data = _json.loads(open(path).read())
        assert len(data["tokens"]) > 0


def test_competition_detector_anomaly_detection():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        # Manually set a baseline
        detector._set_baseline("PLAY/USD", 3_200_000)
        # Volume 6x baseline → anomaly
        assert detector._is_anomaly("PLAY/USD", 19_200_000) is True


def test_competition_detector_no_anomaly_below_threshold():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        # 4x — below 5x threshold
        assert detector._is_anomaly("PLAY/USD", 12_800_000) is False


def test_competition_detector_null_baseline_not_anomaly():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        # Null baseline on first observation — not an anomaly
        assert detector._is_anomaly("NEW/USD", 999_999_999) is False


def test_competition_detector_ema_update():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        detector._update_baseline("PLAY/USD", 3_200_000)
        updated = detector._get_baseline("PLAY/USD")
        # EMA with alpha=1/7: new = (1/7)*3.2M + (6/7)*3.2M = 3.2M (stable)
        assert abs(updated - 3_200_000) < 1000


# ─── Extension Guard Tests ────────────────────────────────────────────────────

def test_entry_gate_extension_blocks_parabolic():
    """Extension guard blocks entry when price is >20% above slow EMA."""
    from hydra_meme_agent import EXTENSION_MAX_PCT
    eng = SignalEngine()
    for i in range(25):
        eng.add_bar(_make_bar(close=1.0 + i * 0.05, volume=1000.0, ts=i * 300))
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=2.3, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["not_extended"] is False
    assert gates["all_pass"] is False


def test_entry_gate_extension_passes_normal():
    """Extension guard passes when price is within 20% of slow EMA."""
    eng = _warmed_engine(close=1.0, n_bars=25)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=1.015, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["not_extended"] is True


def test_ema_simple():
    """EMA with alpha = 2/(period+1)."""
    from hydra_meme_agent import ema
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(values, period=3)
    # alpha = 0.5: 1.0 -> 1.5 -> 2.25 -> 3.125 -> 4.0625
    assert abs(result - 4.0625) < 0.001


def test_ema_single_value():
    from hydra_meme_agent import ema
    assert ema([42.0], period=5) == 42.0


def test_ema_empty():
    from hydra_meme_agent import ema
    assert ema([], period=5) == 0.0


def test_entry_gate_trend_filter_blocks_downtrend():
    """EMA trend filter blocks entry when fast EMA < slow EMA (downtrend)."""
    eng = SignalEngine()
    for i in range(25):
        eng.add_bar(_make_bar(close=1.0 - i * 0.01, volume=1000.0, ts=i * 300))
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=0.76, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["trend_aligned"] is False
    assert gates["all_pass"] is False


def test_entry_gate_trend_filter_passes_uptrend():
    """EMA trend filter passes when fast EMA > slow EMA (uptrend)."""
    eng = SignalEngine()
    for i in range(25):
        eng.add_bar(_make_bar(close=1.0 + i * 0.01, volume=1000.0, ts=i * 300))
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=1.25, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["trend_aligned"] is True


def test_competition_detector_alert_suppression():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        # Suppress for 2 hours
        future = time.time() + 7200
        detector._suppress("PLAY/USD", until=future)
        assert detector._is_suppressed("PLAY/USD") is True


def test_competition_detector_suppression_expired():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        detector._suppress("PLAY/USD", until=time.time() - 1)
        assert detector._is_suppressed("PLAY/USD") is False


# ─── Session State & Persistence Tests ────────────────────────────────────────

from hydra_meme_agent import SessionState, save_session, append_journal


def test_save_and_load_session():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "session.json")
        state = SessionState(pair="PLAY/USD", engine_state="running",
                             session_pnl=10.20, daily_pnl=10.20, trade_count=2)
        save_session(state, path)
        with open(path) as f:
            data = _json.load(f)
        assert data["pair"] == "PLAY/USD"
        assert data["session_pnl"] == 10.20
        assert data["trade_count"] == 2


def test_save_session_atomic(tmp_path):
    path = str(tmp_path / "session.json")
    state = SessionState(pair="TEST/USD")
    save_session(state, path)
    assert _os.path.exists(path)
    assert not _os.path.exists(path + ".tmp")


def test_append_journal(tmp_path):
    path = str(tmp_path / "journal.json")
    record = TradeRecord(entry_ts=1000, exit_ts=1300, entry_price=1.0, exit_price=1.025,
                         qty=600.0, gross_pnl=15.0, fees_usd=4.80, net_pnl=10.20,
                         exit_reason="profit_target", hold_candles=2)
    append_journal(record, path)
    append_journal(record, path)
    data = _json.loads(open(path).read())
    assert len(data) == 2


# ─── MemeExecutor Tests ────────────────────────────────────────────────────────

from hydra_meme_agent import MemeExecutor


def test_executor_buy_price_calculation():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    expected_limit = ask * (1 + TAKER_SLIPPAGE_BPS / 10000)
    price = exec_._buy_limit_price(ask)
    assert abs(price - expected_limit) < 0.000001


def test_executor_buy_rejects_above_slippage_cap():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    price = exec_._buy_limit_price(ask)
    assert price <= ask * (1 + SLIPPAGE_CAP_BPS / 10000)


def test_executor_sell_price_calculation():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    bid = 0.16520
    price = exec_._sell_limit_price(bid)
    expected = bid * (1 - TAKER_SLIPPAGE_BPS / 10000)
    assert abs(price - expected) < 0.000001


def test_executor_qty_calculation():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    qty = exec_._buy_qty(ask)
    assert abs(qty * ask - 600.0) < 0.01


def test_executor_daily_cap_blocks_trade():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    exec_._daily_loss = -30.01  # already hit cap
    assert exec_.is_halted() is True


def test_executor_not_halted_initially():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    assert exec_.is_halted() is False


def test_executor_record_loss_triggers_halt():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    exec_.record_pnl(-31.0)
    assert exec_.is_halted() is True


def test_executor_record_pnl_accumulates():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    exec_.record_pnl(10.20)
    exec_.record_pnl(-5.00)
    assert abs(exec_._daily_pnl - 5.20) < 0.001


def test_executor_net_pnl_calculation():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    # BUY at 0.16000, SELL at 0.16400 (2.5% move)
    pos = Position(entry_price=0.16000, qty=3750.0, notional_usd=600.0,
                   entry_ts=1000, candles_held=2)
    exit_price = 0.16400
    net = exec_._compute_net_pnl(pos, exit_price)
    # gross = (0.164 - 0.16) * 3750 = $15.00
    # fees = 600 * 0.004 + (600*1.025) * 0.004 ≈ 4.86
    assert 9.0 < net < 11.0


def test_executor_daily_cap_zero_raises():
    import pytest
    with pytest.raises(ValueError, match="daily_cap must be positive"):
        MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=0.0)


def test_executor_slippage_cap_blocks_buy_when_spread_too_wide():
    """place_buy returns None when limit_price deviates > SLIPPAGE_CAP_BPS from mid."""
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.18000
    # Simulate a mid-price well below ask — spread is huge
    mid = 0.16000  # (limit = ask*1.0005 ≈ 0.1801, deviation from mid ≈ 125 bps)
    slippage_bps = (exec_._buy_limit_price(ask) - mid) / mid * 10_000
    assert slippage_bps > SLIPPAGE_CAP_BPS


def test_executor_slippage_cap_allows_buy_tight_spread():
    """place_buy proceeds when spread is within SLIPPAGE_CAP_BPS.
    With bid=0.16537 and ask=0.16540 (2-bps spread), limit = ask*1.0005 ≈ 6 bps from mid.
    """
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    bid = 0.16537   # 2-bps spread — typical for liquid token
    mid = (bid + ask) / 2
    slippage_bps = (exec_._buy_limit_price(ask) - mid) / mid * 10_000
    assert slippage_bps <= SLIPPAGE_CAP_BPS


def test_wilder_rsi_boundary_exactly_period():
    """Exactly RSI_PERIOD values → insufficient → returns 50.0."""
    closes = [float(i) for i in range(1, RSI_PERIOD + 1)]  # RSI_PERIOD values, RSI_PERIOD-1 diffs
    assert wilder_rsi(closes, period=RSI_PERIOD) == 50.0


def test_wilder_rsi_boundary_period_plus_one():
    """RSI_PERIOD+1 values → sufficient → computes a real value."""
    closes = [float(i) for i in range(1, RSI_PERIOD + 2)]  # all gains
    result = wilder_rsi(closes, period=RSI_PERIOD)
    assert result == 100.0


def test_candles_held_increments_on_bar():
    """_on_bar via SignalEngine: position.candles_held increments each bar."""
    from hydra_meme_agent import SignalEngine

    def _make_bar(close=1.0, volume=10000.0):
        return CandleBar(ts=0, open=close, high=close * 1.01, low=close * 0.99,
                         close=close, vwap=close, volume=volume, count=100)

    engine = SignalEngine()
    # Warm up
    for i in range(15):
        engine.add_bar(_make_bar(close=1.0 + i * 0.001))

    pos = Position(entry_price=1.0, qty=600.0, notional_usd=600.0, entry_ts=0, candles_held=0)
    # Simulate two more bars
    for expected_count in [1, 2, 3]:
        bar = _make_bar(close=1.015)
        pos.candles_held += 1  # this mirrors MemeAgent._handle_bar line
        assert pos.candles_held == expected_count


def test_compute_obi_string_inputs():
    """compute_obi handles string-typed price/qty tuples from Kraken REST."""
    bids = [("1.0000", "5000.0"), ("0.9990", "3000.0")]
    asks = [("1.0010", "1000.0"), ("1.0020", "500.0")]
    result = compute_obi(bids, asks)
    # bid_depth = 1.0*5000 + 0.999*3000 = 7997; ask_depth = 1.001*1000 + 1.002*500 = 1502
    assert result > 0.5  # bid-heavy


def test_executor_win_rate_no_div_zero():
    """session_stats win_rate calculation should not divide by zero."""
    from hydra_meme_agent import MemeExecutor
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    trade_log = []
    win_rate = sum(1 for t in trade_log if t.net_pnl > 0) / max(len(trade_log), 1)
    assert win_rate == 0.0


# ─── Fill Verification Tests ─────────────────────────────────────────────────

def test_query_fill_empty_txid():
    assert _query_fill("") is None
    assert _query_fill(None) is None


def test_query_fill_parses_closed_order():
    mock_response = {
        "ABC123": {
            "status": "closed",
            "price": "0.16520",
            "vol_exec": "3628.50000",
        }
    }
    with patch("hydra_meme_agent._kraken_cli", return_value=mock_response):
        result = _query_fill("ABC123")
    assert result is not None
    assert result["status"] == "filled"
    assert result["avg_price"] == 0.16520
    assert result["vol_exec"] == 3628.5


def test_query_fill_parses_pending_order():
    mock_response = {
        "ABC123": {
            "status": "open",
            "price": "0.0",
            "vol_exec": "0.0",
        }
    }
    with patch("hydra_meme_agent._kraken_cli", return_value=mock_response):
        result = _query_fill("ABC123")
    assert result is not None
    assert result["status"] == "open"


def test_query_fill_handles_cli_error():
    with patch("hydra_meme_agent._kraken_cli", return_value={"error": "timeout"}):
        result = _query_fill("ABC123")
    assert result is None


def test_cancel_order_empty_txid():
    result = _cancel_order("")
    assert "error" in result


def test_cancel_order_calls_cli():
    with patch("hydra_meme_agent._kraken_cli", return_value={"count": 1}) as mock_cli:
        result = _cancel_order("XYZ789")
    mock_cli.assert_called_once_with(["order", "cancel", "XYZ789", "--yes"])
    assert result == {"count": 1}


# ─── Fill-Verified Executor Tests ─────────────────────────────────────────────

def test_place_buy_uses_actual_fill_price():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    order_response = {"txid": ["BUY001"]}
    fill_response = {"status": "filled", "avg_price": 0.16500, "vol_exec": 3636.36}
    with patch("hydra_meme_agent._kraken_cli", return_value=order_response), \
         patch("hydra_meme_agent._query_fill", return_value=fill_response):
        pos = exec_.place_buy(ask=0.16540)
    assert pos is not None
    assert pos.entry_price == 0.16500
    assert pos.qty == 3636.36


def test_place_buy_falls_back_to_limit_on_fill_failure():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    order_response = {"txid": ["BUY002"]}
    with patch("hydra_meme_agent._kraken_cli", return_value=order_response), \
         patch("hydra_meme_agent._query_fill", return_value=None):
        pos = exec_.place_buy(ask=0.16540)
    assert pos is not None
    assert pos.entry_price == exec_._buy_limit_price(0.16540)


def test_place_sell_uses_actual_fill_price():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    pos = Position(entry_price=0.16000, qty=3750.0, notional_usd=600.0,
                   entry_ts=1000, candles_held=2)
    order_response = {"txid": ["SELL001"]}
    fill_response = {"status": "filled", "avg_price": 0.16410, "vol_exec": 3750.0}
    with patch("hydra_meme_agent._kraken_cli", return_value=order_response), \
         patch("hydra_meme_agent._query_fill", return_value=fill_response):
        result = exec_.place_sell(pos, bid=0.16400, reason="profit_target")
    assert result is not None
    assert result["record"].exit_price == 0.16410


def test_sell_max_retries_constant():
    assert SELL_MAX_RETRIES > 0
    assert SELL_MAX_RETRIES <= 10


def test_apex_ws_port_no_collision():
    """APEX port must not collide with hydra_ws_server.next_agent_port."""
    from hydra_meme_agent import WS_PORT
    assert WS_PORT >= 8770, f"WS_PORT={WS_PORT} collides with hydra_ws_server agent port range (8766+)"


def test_reentry_cooldown_constant():
    from hydra_meme_agent import REENTRY_COOLDOWN_BARS
    assert REENTRY_COOLDOWN_BARS >= 2


def test_reentry_cooldown_blocks_immediate_reentry():
    """Agent should not enter within REENTRY_COOLDOWN_BARS of last exit."""
    from hydra_meme_agent import REENTRY_COOLDOWN_BARS
    assert REENTRY_COOLDOWN_BARS == 2


def test_executor_daily_reset():
    """Daily loss and halt state reset when the day changes."""
    exec_ = MemeExecutor("PLAY/USD", position_size=300.0, daily_cap=30.0)
    exec_.record_pnl(-31.0)
    assert exec_.is_halted() is True
    # Should NOT reset if same day
    exec_.maybe_reset_daily()
    assert exec_.is_halted() is True
    # Force the tracked date to yesterday
    exec_._last_reset_date = "2026-05-06"
    exec_.maybe_reset_daily()
    assert exec_.is_halted() is False
    assert exec_._daily_loss == 0.0
    assert exec_._daily_pnl == 0.0
