"""No blanket 50-bar hold. Indicators publish as soon as a price exists,
and a strategy can fire once its own lookback is real."""
from hydra_engine import (
    Candle, HydraEngine, SignalAction, SignalGenerator, Strategy,
)


_T0 = 1_700_000_000.0


def _flat(n, price=100.0):
    prices = [price] * n
    candles = [
        Candle(price, price, price, price, 1.0, _T0 + float(i) * 3600.0)
        for i in range(n)
    ]
    return prices, candles


def test_first_bar_publishes_rsi_instead_of_a_warmup_hold():
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", hold_through=False)
    eng.ingest_candle({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 10.0, "timestamp": 1_700_000_000.0,
    })
    state = eng.tick(generate_only=True)
    assert "warming" not in state["signal"]["reason"].lower()
    assert state["indicators"]["rsi"] == 50.0
    assert state["indicators"]["price"] == 100.0


def test_collapsed_band_is_not_a_buy():
    prices, candles = _flat(8)
    grid = SignalGenerator.generate(Strategy.GRID, prices, candles)
    mean = SignalGenerator.generate(Strategy.MEAN_REVERSION, prices, candles)
    assert grid.action == SignalAction.HOLD
    assert "not formed" in grid.reason
    assert mean.action == SignalAction.HOLD
    assert "not formed" in mean.reason


def test_mean_reversion_can_fire_before_50_bars():
    """A real Bollinger touch plus a real RSI, on fewer than 50 closes."""
    prices = []
    candles = []
    px = 100.0
    for i in range(24):
        px = 100.0 + (0.3 if i % 2 else -0.3)
        prices.append(px)
        candles.append(Candle(px, px + 0.2, px - 0.2, px, 10.0, _T0 + float(i) * 3600.0))
    for i, px in enumerate((99.2, 98.4, 97.6, 96.8, 96.0, 95.2), start=24):
        prices.append(px)
        candles.append(Candle(px + 0.2, px + 0.2, px - 0.3, px, 20.0, _T0 + float(i) * 3600.0))
    assert len(prices) < 50
    sig = SignalGenerator.generate(Strategy.MEAN_REVERSION, prices, candles)
    assert sig.action == SignalAction.BUY
    assert sig.indicators["rsi"] < 35

    eng = HydraEngine(initial_balance=10_000.0, asset="BTC/USD", hold_through=False)
    for c in candles:
        eng.ingest_candle({
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume, "timestamp": c.timestamp,
        })
    state = eng.tick(generate_only=True)
    assert state["signal"]["action"] == "BUY"
    assert len(eng.prices) < 50
