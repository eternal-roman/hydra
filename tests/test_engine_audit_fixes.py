"""Engine fixes from the 2026-10 audit, each pinned by a regression test."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_engine import HydraEngine, Regime, RegimeDetector, Signal, SignalAction, Strategy


def _feed(eng, closes, t0=1_700_000_000.0, spread=0.004):
    for i, c in enumerate(closes):
        eng.ingest_candle({"open": c, "high": c * (1 + spread), "low": c * (1 - spread),
                           "close": c, "volume": 10.0, "timestamp": t0 + i * 3600})


def test_volatile_dump_still_triggers_the_downtrend_flatten(monkeypatch):
    """A dump spikes ATR/BB width, so detect() says VOLATILE and never
    TREND_DOWN. The hold-through flatten keyed on the label rode the dump."""
    monkeypatch.setenv("HYDRA_TREND_OVERLAY", "0")
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", hold_through=True)
    closes = [100.0 + 0.02 * i for i in range(80)]
    price = closes[-1]
    for _ in range(12):  # -1.5%/h with widening ranges
        price *= 0.985
        closes.append(price)
    for i, c in enumerate(closes):
        wide = 0.004 if i < 80 else 0.03
        eng.ingest_candle({"open": c, "high": c * (1 + wide), "low": c * (1 - wide),
                           "close": c, "volume": 10.0,
                           "timestamp": 1_700_000_000.0 + i * 3600})
    eng.position.size, eng.position.avg_entry = 1.0, 101.0
    regime = RegimeDetector.detect(eng.candles, eng.prices)
    assert regime == Regime.VOLATILE
    assert RegimeDetector.trend_down(eng.prices)
    sig = Signal(SignalAction.HOLD, 0.5, "Grid HOLD", Strategy.GRID)
    out = eng._apply_hold_through(regime, sig)
    assert out.action == SignalAction.SELL
    assert "force_flatten" in out.reason


def test_volatile_without_a_downtrend_does_not_flatten(monkeypatch):
    monkeypatch.setenv("HYDRA_TREND_OVERLAY", "0")
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", hold_through=True)
    _feed(eng, [100.0 + 0.05 * i for i in range(80)])
    eng.position.size, eng.position.avg_entry = 1.0, 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "Grid HOLD", Strategy.GRID)
    out = eng._apply_hold_through(Regime.VOLATILE, sig)
    assert out.action == SignalAction.HOLD


def test_cvd_sigma_does_not_depend_on_where_the_buffer_starts():
    """The slope was normalised by |mean cumulative CVD|, whose zero is the
    oldest bar in the buffer: one candle 250 bars back flipped the sign."""
    random.seed(7)
    rows = []
    for i in range(400):
        c = 100.0 + random.uniform(-1, 1)
        rows.append({"open": c, "high": c + 0.5, "low": c - 0.5,
                     "close": c + random.uniform(-0.4, 0.4),
                     "volume": random.uniform(50, 150), "timestamp": i * 3600.0})

    def sigma(skip, first_volume=None):
        eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", candle_interval=60)
        for j, r in enumerate(rows[skip:]):
            r = dict(r)
            if j == 0 and first_volume is not None:
                r["volume"] = first_volume
            eng.ingest_candle(r)
        return eng.cvd_divergence_sigma()

    base = sigma(150)
    assert base is not None
    assert sigma(150, first_volume=3000.0) == base
    assert sigma(149) == base


def test_trend_vol_multiplier_fails_open_until_the_overlay_scores(monkeypatch):
    monkeypatch.delenv("HYDRA_TREND_OVERLAY", raising=False)
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD")
    random.seed(3)
    daily = []
    px = 100.0
    for d in range(100):  # vol computable (>22 closes) but score is None (<210)
        px *= math.exp(random.gauss(0, 0.05))
        daily.append({"timestamp": d * 86400.0, "close": px})
    eng.seed_daily_closes(daily)
    assert eng.daily_trend_score() is None
    assert eng.daily_realized_vol_pct() is not None
    assert eng._trend_vol_multiplier() == 1.0


def test_remainder_write_off_books_the_closed_pnl():
    """set_base_remainder zeroed the book but kept realized_pnl, so the
    next round trip inherited it (a real -5 trip was booked as a +4 win)."""
    eng = HydraEngine(initial_balance=1000.0, asset="ETH/USD")
    eng.position.size = 0.1
    eng.position.avg_entry = 100.0
    eng.position.realized_pnl = 9.0
    eng.set_base_remainder(0.9, 0.9)  # exchange had 0.9, all of it sold
    assert eng.position.size == 0.0
    assert eng.position.realized_pnl == 0.0
    assert eng.total_trades == 1 and eng.win_count == 1
    assert eng.gross_profit == 9.0


def test_default_on_switches_need_an_explicit_off(monkeypatch):
    from hydra_engine import _env_default_on
    for raw, expected in (("", True), ("enabled", True), ("1", True),
                          ("0", False), ("false", False), (" OFF ", False), ("no", False)):
        monkeypatch.setenv("HYDRA_HOLD_THROUGH", raw)
        assert _env_default_on("HYDRA_HOLD_THROUGH") is expected, raw
        assert HydraEngine(asset="BTC/USD").hold_through is expected, raw
    monkeypatch.delenv("HYDRA_HOLD_THROUGH")
    assert _env_default_on("HYDRA_HOLD_THROUGH") is True


def test_intra_candle_ticks_keep_one_equity_point_per_candle():
    eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD")
    _feed(eng, [100.0 + 0.1 * i for i in range(40)])
    before = len(eng.equity_history)
    for _ in range(12):  # live: 12 ticks on the same forming bar
        eng.tick()
    assert len(eng.equity_history) == before + 1
    eng.ingest_candle({"open": 104, "high": 104.5, "low": 103.5, "close": 104,
                       "volume": 10.0, "timestamp": 1_700_000_000.0 + 40 * 3600})
    eng.tick()
    assert len(eng.equity_history) == before + 2
