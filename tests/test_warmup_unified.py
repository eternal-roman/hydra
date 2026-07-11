"""PR-F: unified 50-candle warmup before executable signals."""
from hydra_engine import HydraEngine, SignalAction, SignalGenerator


def test_warmup_candles_constant():
    assert SignalGenerator.WARMUP_CANDLES == 50


def test_no_actionable_signal_before_50_bars():
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    non_hold = 0
    for i in range(49):
        p = 100.0 + (i % 5)
        eng.ingest_candle({
            "open": p, "high": p + 1, "low": p - 1,
            "close": p, "volume": 100,
            "timestamp": float(i),
        })
        st = eng.tick(generate_only=True)
        if st["signal"]["action"] != "HOLD":
            non_hold += 1
    assert non_hold == 0
