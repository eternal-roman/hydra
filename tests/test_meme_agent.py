import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from hydra_meme_agent import CandleBar, wilder_rsi, vol_ema, compute_obi, compute_vwap


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
