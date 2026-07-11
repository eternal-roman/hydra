"""
HYDRA Engine Test Suite
Validates indicators, regime detection, signal generation, position sizing,
and circuit breaker logic. All tests use deterministic synthetic data.
"""

import sys
import os
import math


class SkipTest(Exception):
    """Raised to skip a test (e.g. missing optional dependency)."""
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_engine import (
    Indicators, RegimeDetector, SignalGenerator, PositionSizer, HydraEngine,
    Regime, Strategy, SignalAction, Candle,
    SIZING_CONSERVATIVE, SIZING_COMPETITION,
)


# ═══════════════════════════════════════════════════════════════
# HELPER: generate synthetic price data
# ═══════════════════════════════════════════════════════════════

def make_prices(base, changes):
    """Build a price series from a base and list of deltas."""
    prices = [base]
    for d in changes:
        prices.append(prices[-1] + d)
    return prices


def make_candles(prices):
    """Build Candle objects from a price list."""
    candles = []
    for i, p in enumerate(prices):
        candles.append(Candle(
            open=p - 0.5, high=p + 1.0, low=p - 1.0, close=p, volume=100.0, timestamp=float(i),
        ))
    return candles


def make_trending_up(n=100):
    """Prices that trend upward steadily."""
    return [100.0 + i * 0.5 for i in range(n)]


def make_trending_down(n=100):
    """Prices that trend downward steadily."""
    return [200.0 - i * 0.5 for i in range(n)]


def make_ranging(n=100):
    """Prices that oscillate in a tight range."""
    import math as m
    return [100.0 + 2.0 * m.sin(i * 0.3) for i in range(n)]


def make_volatile(n=100):
    """Prices with large swings (high ATR)."""
    return [100.0 + (10.0 if i % 2 == 0 else -10.0) for i in range(n)]


# ═══════════════════════════════════════════════════════════════
# 1. INDICATOR TESTS
# ═══════════════════════════════════════════════════════════════

class TestEMA:
    def test_basic(self):
        # SMA seed over first 5 prices = 12.0, then exponential smoothing with
        # k = 2/(5+1) = 1/3 converges to exactly 17.0 on this arithmetic sequence.
        # A regression that swapped EMA for SMA-of-last-5 would return 17.0 too,
        # so test_sma_seed below pins the seed path separately.
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
        result = Indicators.ema(prices, 5)
        assert abs(result - 17.0) < 1e-9

    def test_short_input(self):
        prices = [10.0, 11.0]
        result = Indicators.ema(prices, 5)
        assert result == 11.0  # falls back to last price

    def test_empty(self):
        result = Indicators.ema([], 5)
        assert result == 0.0

    def test_sma_seed(self):
        prices = [2.0, 4.0, 6.0, 8.0, 10.0]
        result = Indicators.ema(prices, 5)
        assert result == 6.0  # exactly SMA when period == len


class TestRSI:
    def test_uptrend(self):
        prices = make_trending_up(50)
        rsi = Indicators.rsi(prices)
        assert rsi > 70  # strong uptrend should be overbought

    def test_downtrend(self):
        prices = make_trending_down(50)
        rsi = Indicators.rsi(prices)
        assert rsi < 30  # strong downtrend should be oversold

    def test_flat(self):
        prices = [100.0] * 50
        rsi = Indicators.rsi(prices)
        assert rsi == 50.0  # no movement = neutral

    def test_insufficient_data(self):
        prices = [100.0, 101.0]
        rsi = Indicators.rsi(prices)
        assert rsi == 50.0  # default when insufficient

    def test_all_gains(self):
        prices = list(range(1, 30))
        rsi = Indicators.rsi(prices)
        assert rsi == 100.0  # all gains, no losses

    def test_range(self):
        prices = make_ranging(100)
        rsi = Indicators.rsi(prices)
        assert 20 < rsi < 80  # ranging should be mid-range


class TestATR:
    def test_basic(self):
        candles = make_candles(make_ranging(30))
        atr = Indicators.atr(candles)
        assert atr > 0

    def test_insufficient_data(self):
        candles = make_candles([100.0, 101.0])
        atr = Indicators.atr(candles)
        assert atr == 0.0

    def test_volatile_higher(self):
        calm_candles = make_candles(make_ranging(30))
        wild_candles = make_candles(make_volatile(30))
        assert Indicators.atr(wild_candles) > Indicators.atr(calm_candles)


class TestBollingerBands:
    def test_basic(self):
        prices = make_ranging(30)
        bb = Indicators.bollinger_bands(prices)
        assert bb["upper"] > bb["middle"] > bb["lower"]
        assert bb["width"] > 0

    def test_flat(self):
        prices = [100.0] * 25
        bb = Indicators.bollinger_bands(prices)
        assert bb["upper"] == bb["middle"] == bb["lower"] == 100.0
        assert bb["width"] == 0.0

    def test_short_input(self):
        prices = [100.0, 101.0]
        bb = Indicators.bollinger_bands(prices)
        assert bb["upper"] == bb["lower"] == 101.0  # falls back to last price


class TestMACD:
    def test_uptrend_positive(self):
        prices = make_trending_up(60)
        macd = Indicators.macd(prices)
        assert macd["macd"] > 0
        assert macd["histogram"] != 0

    def test_downtrend_negative(self):
        prices = make_trending_down(60)
        macd = Indicators.macd(prices)
        assert macd["macd"] < 0

    def test_insufficient_data(self):
        prices = [100.0] * 10
        macd = Indicators.macd(prices)
        assert macd["macd"] == 0.0
        assert macd["signal"] == 0.0
        assert macd["histogram"] == 0.0

    def test_signal_line_differs(self):
        prices = make_trending_up(60)
        macd = Indicators.macd(prices)
        assert macd["signal"] != macd["macd"]  # signal line should lag


# ═══════════════════════════════════════════════════════════════
# 2. REGIME DETECTION TESTS
# ═══════════════════════════════════════════════════════════════

class TestRegimeDetection:
    def test_warmup_returns_ranging(self):
        prices = [100.0] * 20
        candles = make_candles(prices)
        assert RegimeDetector.detect(candles, prices) == Regime.RANGING

    def test_trend_up(self):
        prices = make_trending_up(100)
        candles = make_candles(prices)
        regime = RegimeDetector.detect(candles, prices)
        assert regime == Regime.TREND_UP

    def test_trend_down(self):
        prices = make_trending_down(100)
        candles = make_candles(prices)
        regime = RegimeDetector.detect(candles, prices)
        assert regime == Regime.TREND_DOWN

    def test_ranging(self):
        prices = make_ranging(100)
        candles = make_candles(prices)
        regime = RegimeDetector.detect(candles, prices)
        assert regime == Regime.RANGING

    def test_volatile_overrides_trend(self):
        # Adaptive detection: VOLATILE fires when current ATR% is a spike
        # above the asset's own median.  Build calm history then a sudden spike.
        calm_prices = [100.0 + 0.5 * i for i in range(80)]
        calm_candles = make_candles(calm_prices)
        # Append 20 extremely volatile candles at the end
        for i in range(20):
            p = calm_prices[-1] + (15.0 if i % 2 == 0 else -15.0)
            calm_prices.append(p)
            calm_candles.append(Candle(
                open=p, high=p + 20.0, low=p - 20.0, close=p, volume=100.0,
                timestamp=float(80 + i),
            ))
        regime = RegimeDetector.detect(calm_candles, calm_prices)
        assert regime == Regime.VOLATILE


# ═══════════════════════════════════════════════════════════════
# 3. SIGNAL GENERATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestSignalGeneration:
    def test_warmup_hold(self):
        prices = [100.0] * 10
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.MOMENTUM, prices, candles)
        assert signal.action == SignalAction.HOLD
        assert signal.confidence == 0.0

    def test_momentum_returns_signal(self):
        prices = make_trending_up(60)
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.MOMENTUM, prices, candles)
        assert signal.action in (SignalAction.BUY, SignalAction.SELL, SignalAction.HOLD)
        assert signal.strategy == Strategy.MOMENTUM
        assert 0 <= signal.confidence <= 1

    def test_mean_reversion_returns_signal(self):
        prices = make_ranging(60)
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.MEAN_REVERSION, prices, candles)
        assert signal.strategy == Strategy.MEAN_REVERSION

    def test_grid_returns_signal(self):
        prices = make_volatile(60)
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.GRID, prices, candles)
        assert signal.strategy == Strategy.GRID

    def test_defensive_returns_signal(self):
        prices = make_trending_down(60)
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.DEFENSIVE, prices, candles)
        assert signal.strategy == Strategy.DEFENSIVE

    def test_all_signals_have_indicators(self):
        prices = make_ranging(60)
        candles = make_candles(prices)
        for strat in Strategy:
            signal = SignalGenerator.generate(strat, prices, candles)
            assert "rsi" in signal.indicators
            assert "macd_histogram" in signal.indicators
            assert "bb_upper" in signal.indicators

    def test_defensive_buy_scales_with_rsi(self):
        """DEFENSIVE BUY confidence scales with RSI severity, starts at 0.50."""
        prices = make_trending_down(60)
        prices[-1] = prices[-1] - 50  # extreme oversold
        candles = make_candles(prices)
        signal = SignalGenerator.generate(Strategy.DEFENSIVE, prices, candles)
        assert signal.action == SignalAction.BUY
        # Confidence should be >= 0.50 (meets competition threshold) and <= 0.75
        assert 0.50 <= signal.confidence <= 0.75, f"Got {signal.confidence}"


# ═══════════════════════════════════════════════════════════════
# 4. POSITION SIZING TESTS
# ═══════════════════════════════════════════════════════════════

class TestPositionSizer:
    def setup(self):
        self.conservative = PositionSizer(**SIZING_CONSERVATIVE)
        self.competition = PositionSizer(**SIZING_COMPETITION)

    def test_below_threshold(self):
        self.setup()
        size = self.conservative.calculate(0.50, 10000, 100.0, "SOL/USDC")
        assert size == 0.0

    def test_at_threshold(self):
        self.setup()
        size = self.conservative.calculate(0.65, 10000, 100.0, "SOL/USDC")
        assert size > 0

    def test_max_position_cap(self):
        self.setup()
        size = self.conservative.calculate(0.99, 10000, 100.0, "SOL/USDC")
        value = size * 100.0
        assert value <= 10000 * 0.30

    def test_min_trade_value(self):
        self.setup()
        size = self.conservative.calculate(0.55, 1.0, 100.0, "SOL/USDC")
        assert size == 0.0

    def test_kraken_min_order_size(self):
        self.setup()
        size = self.conservative.calculate(0.56, 100.0, 100.0, "SOL/USDC")
        assert size >= 0.02 or size == 0.0

    def test_btc_min_order_size(self):
        self.setup()
        size = self.conservative.calculate(0.95, 1000.0, 67000.0, "BTC/USDC")
        assert size >= 0.00005 or size == 0.0

    def test_zero_price(self):
        self.setup()
        size = self.conservative.calculate(0.8, 10000, 0.0)
        assert size == 0.0

    def test_scaling(self):
        self.setup()
        low = self.conservative.calculate(0.60, 10000, 100.0, "SOL/USDC")
        high = self.conservative.calculate(0.90, 10000, 100.0, "SOL/USDC")
        assert high > low

    # ─── Competition mode tests ───

    def test_competition_lower_threshold(self):
        self.setup()
        # Both modes share 0.65 min_confidence — verify both reject below
        cons = self.conservative.calculate(0.60, 10000, 100.0, "SOL/USDC")
        comp = self.competition.calculate(0.60, 10000, 100.0, "SOL/USDC")
        assert cons == 0.0  # conservative rejects below 0.65
        assert comp == 0.0  # competition also rejects below 0.65
        # Above threshold: competition produces larger size (half-Kelly > quarter-Kelly)
        cons_above = self.conservative.calculate(0.70, 10000, 100.0, "SOL/USDC")
        comp_above = self.competition.calculate(0.70, 10000, 100.0, "SOL/USDC")
        assert cons_above > 0
        assert comp_above > cons_above

    def test_competition_larger_positions(self):
        self.setup()
        cons = self.conservative.calculate(0.80, 10000, 100.0, "SOL/USDC")
        comp = self.competition.calculate(0.80, 10000, 100.0, "SOL/USDC")
        assert comp > cons  # half-Kelly > quarter-Kelly

    def test_competition_higher_max(self):
        self.setup()
        cons = self.conservative.calculate(0.99, 10000, 100.0, "SOL/USDC")
        comp = self.competition.calculate(0.99, 10000, 100.0, "SOL/USDC")
        cons_val = cons * 100.0
        comp_val = comp * 100.0
        assert cons_val <= 10000 * 0.30  # conservative: 30% max
        assert comp_val <= 10000 * 0.40  # competition: 40% max

    def test_presets_valid(self):
        """Verify sizing presets have all required keys."""
        for preset in [SIZING_CONSERVATIVE, SIZING_COMPETITION]:
            assert "kelly_multiplier" in preset
            assert "min_confidence" in preset
            assert "max_position_pct" in preset
            sizer = PositionSizer(**preset)
            assert sizer.kelly_multiplier > 0
            assert 0 < sizer.min_confidence < 1
            assert 0 < sizer.max_position_pct <= 1


# ═══════════════════════════════════════════════════════════════
# 5. ENGINE INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestHydraEngine:
    def test_ingest_and_tick(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        for i in range(60):
            engine.ingest_candle({
                "open": 95000 + i, "high": 95100 + i,
                "low": 94900 + i, "close": 95000 + i * 2,
                "volume": 100,
            })
        state = engine.tick()
        assert "regime" in state
        assert "strategy" in state
        assert "signal" in state
        assert "portfolio" in state
        assert "candles" in state
        assert "trend" in state
        assert "ema20" in state["trend"]
        assert "ema50" in state["trend"]
        assert "volatility" in state
        assert "atr" in state["volatility"]
        assert "atr_pct" in state["volatility"]
        assert "volume" in state
        assert "current" in state["volume"]
        assert "avg_20" in state["volume"]
        assert "candle_interval" in state
        assert "candle_status" in state

    def test_candle_deduplication(self):
        """Duplicate timestamps update in place instead of appending."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        engine.ingest_candle({"open": 100, "high": 101, "low": 99, "close": 100, "volume": 50, "timestamp": 1000.0})
        assert len(engine.candles) == 1
        assert engine.prices[-1] == 100
        # Same timestamp, different close — should update, not append
        engine.ingest_candle({"open": 100, "high": 102, "low": 98, "close": 105, "volume": 80, "timestamp": 1000.0})
        assert len(engine.candles) == 1
        assert engine.prices[-1] == 105
        # New timestamp — should append
        engine.ingest_candle({"open": 105, "high": 106, "low": 104, "close": 105.5, "volume": 60, "timestamp": 1300.0})
        assert len(engine.candles) == 2

    def test_configurable_regime_thresholds(self):
        """Adaptive volatile multipliers can be tuned to control sensitivity."""
        # Build data with a volatile tail so adaptive detection has a spike
        calm = [100.0 + 0.3 * i for i in range(80)]
        calm_candles = make_candles(calm)
        for i in range(20):
            p = calm[-1] + (10.0 if i % 2 == 0 else -10.0)
            calm.append(p)
            calm_candles.append(Candle(open=p, high=p+12, low=p-12, close=p, volume=100, timestamp=float(80+i)))
        # With very low multiplier (1.01), small variance triggers VOLATILE
        regime = RegimeDetector.detect(calm_candles, calm, volatile_atr_mult=1.01, volatile_bb_mult=1.01)
        assert regime == Regime.VOLATILE
        # With very high multiplier, even the spike is not "exceptional" enough
        regime2 = RegimeDetector.detect(calm_candles, calm, volatile_atr_mult=999.0, volatile_bb_mult=999.0)
        assert regime2 != Regime.VOLATILE

    def test_atr_pct_series_length(self):
        """atr_pct_series returns one value per candle from period onward."""
        prices = make_ranging(100)
        candles = make_candles(prices)
        series = Indicators.atr_pct_series(candles, period=14)
        assert len(series) == len(candles) - 14
        # Too few candles → empty
        assert Indicators.atr_pct_series(candles[:14], period=14) == []

    def test_bb_width_series_length(self):
        """bb_width_series returns one value per price from period onward."""
        prices = make_ranging(100)
        series = Indicators.bb_width_series(prices, period=20)
        assert len(series) == len(prices) - 20 + 1  # inclusive window
        assert Indicators.bb_width_series(prices[:19], period=20) == []

    def test_adaptive_sol_not_always_volatile(self):
        """SOL-like data (high but steady ATR) should NOT be perpetually VOLATILE."""
        # Simulate SOL: base ~150, natural swing +/-8 (ATR ~5-6% of price)
        prices = []
        for i in range(100):
            prices.append(150.0 + 8.0 * math.sin(i * 0.5))
        candles = []
        for i, p in enumerate(prices):
            candles.append(Candle(
                open=p - 4.0, high=p + 5.0, low=p - 5.0, close=p,
                volume=100.0, timestamp=float(i),
            ))
        regime = RegimeDetector.detect(candles, prices)
        # With adaptive detection, steady high-vol should be RANGING (or TREND),
        # NOT perpetually VOLATILE
        assert regime != Regime.VOLATILE

    def test_adaptive_btc_spike_detected(self):
        """BTC-like data with sudden spike should trigger VOLATILE."""
        # 80 calm candles (BTC-like ATR ~1%)
        prices = [50000.0 + 50.0 * math.sin(i * 0.2) for i in range(80)]
        candles = []
        for i, p in enumerate(prices):
            candles.append(Candle(
                open=p - 100, high=p + 200, low=p - 200, close=p,
                volume=100.0, timestamp=float(i),
            ))
        # 20 spiking candles (ATR jumps 5x)
        for i in range(20):
            p = prices[-1] + (2000 if i % 2 == 0 else -2000)
            prices.append(p)
            candles.append(Candle(
                open=p, high=p + 3000, low=p - 3000, close=p,
                volume=100.0, timestamp=float(80 + i),
            ))
        regime = RegimeDetector.detect(candles, prices)
        assert regime == Regime.VOLATILE

    def test_uniform_volatility_not_volatile(self):
        """Constant ATR% → current equals median → multiplier > 1 never triggers."""
        # Every candle has identical range relative to price
        prices = [100.0 + 0.01 * i for i in range(100)]
        candles = []
        for i, p in enumerate(prices):
            candles.append(Candle(
                open=p, high=p + 2.0, low=p - 2.0, close=p,
                volume=100.0, timestamp=float(i),
            ))
        regime = RegimeDetector.detect(candles, prices)
        assert regime != Regime.VOLATILE

    def test_floor_prevents_degenerate(self):
        """Dead market (tiny ATR%) should not trigger VOLATILE on trivial moves."""
        # Stablecoin-like: ATR ~0.01% of price
        prices = [1.000 + 0.0001 * math.sin(i * 0.3) for i in range(100)]
        candles = []
        for i, p in enumerate(prices):
            candles.append(Candle(
                open=p, high=p + 0.0002, low=p - 0.0002, close=p,
                volume=100.0, timestamp=float(i),
            ))
        regime = RegimeDetector.detect(candles, prices)
        assert regime != Regime.VOLATILE

    def test_candle_status(self):
        """Candle status reports forming/closed based on age."""
        import time as _time
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC", candle_interval=5)
        # Recent candle should be "forming"
        engine.ingest_candle({"open": 100, "high": 101, "low": 99, "close": 100, "volume": 50, "timestamp": _time.time()})
        assert engine._candle_status() == "forming"
        # Old candle should be "closed"
        engine.ingest_candle({"open": 100, "high": 101, "low": 99, "close": 100, "volume": 50, "timestamp": _time.time() - 600})
        assert engine._candle_status() == "closed"

    def test_circuit_breaker(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        # Force a massive drawdown
        engine.peak_equity = 10000
        engine.balance = 8000  # 20% loss
        engine.position.size = 0
        for i in range(60):
            engine.ingest_candle({
                "open": 50000, "high": 50100, "low": 49900, "close": 50000, "volume": 100,
            })
        state = engine.tick()
        assert state["halted"] is True
        assert "CIRCUIT BREAKER" in state["halt_reason"]

    def test_buy_updates_position(self):
        # hold_through default ON blocks MR/RANGING buys — base-path test
        engine = HydraEngine(
            initial_balance=10000, asset="SOL/USDC", hold_through=False
        )
        # Phase 1: stable ranging to establish Bollinger Bands
        import random
        rng = random.Random(42)
        for i in range(55):
            p = 100.0 + rng.uniform(-0.3, 0.3)
            engine.ingest_candle({
                "open": p - 0.1, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 100,
            })
            engine.tick()
        # Phase 2: price dips below BB lower with RSI < 35 to trigger mean reversion BUY
        p = 100.0
        for i in range(20):
            p -= 0.5
            engine.ingest_candle({
                "open": p + 0.1, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 100,
            })
            engine.tick()
        # Engine should have opened a position via mean reversion buy
        assert engine.position.size > 0, "Expected position to be open"
        assert engine.position.avg_entry > 0, "Expected valid entry price"
        assert engine.balance < 10000, "Expected balance to decrease after buying"

    def test_candle_memory_bound(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        for i in range(500):
            engine.ingest_candle({
                "open": 95000, "high": 95100, "low": 94900, "close": 95000, "volume": 100,
            })
        assert len(engine.candles) <= engine.MAX_CANDLES
        assert len(engine.prices) <= engine.MAX_CANDLES

    def test_performance_report(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        for i in range(60):
            engine.ingest_candle({
                "open": 95000, "high": 95100, "low": 94900, "close": 95000, "volume": 100,
            })
            engine.tick()
        report = engine.get_performance_report()
        assert "HYDRA PERFORMANCE REPORT" in report
        assert "BTC/USD" in report
        assert "Net P&L" in report

    def test_state_has_candles(self):
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(30):
            engine.ingest_candle({
                "open": 80.0, "high": 81.0, "low": 79.0, "close": 80.0, "volume": 100,
            })
        state = engine.tick()
        assert len(state["candles"]) == 30
        assert "o" in state["candles"][0]
        assert "h" in state["candles"][0]
        assert "l" in state["candles"][0]
        assert "c" in state["candles"][0]

    def test_sell_records_profit(self):
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        # Feed enough candle data for indicators
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100,
            })
        # Manually set a position (bought at 90, now at 100 → profitable)
        engine.position.size = 10.0
        engine.position.avg_entry = 90.0
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9100.0
        # Execute a SELL directly and verify profit is recorded
        trade = engine.execute_signal("SELL", 0.75, "test sell", "MOMENTUM")
        assert trade is not None, "Expected SELL trade to be generated"
        assert trade.action == "SELL"
        assert trade.profit is not None
        assert trade.profit > 0, "Selling at 100 with entry at 90 should be profitable"


class TestSnapshotAndRollback:
    """Tests for snapshot_position/restore_position and win/loss counting."""

    def _make_engine_with_data(self, balance=10000, asset="SOL/USDC"):
        """Create engine with enough candle data for trading."""
        engine = HydraEngine(initial_balance=balance, asset=asset)
        import random
        rng = random.Random(99)
        for i in range(55):
            p = 100.0 + rng.uniform(-0.3, 0.3)
            engine.ingest_candle({
                "open": p - 0.1, "high": p + 0.2, "low": p - 0.2,
                "close": p, "volume": 100, "timestamp": float(i),
            })
        return engine

    def test_snapshot_position_roundtrip(self):
        """snapshot_position + restore_position preserves all trade-relevant state."""
        engine = self._make_engine_with_data()
        engine.position.size = 5.0
        engine.position.avg_entry = 95.0
        engine.position.realized_pnl = 1.23
        engine.position.params_at_entry = {"volatile_atr_mult": 2.0}
        engine.balance = 9500.0
        engine.total_trades = 7
        engine.win_count = 3
        engine.loss_count = 4
        engine.trades.append("dummy")

        snap = engine.snapshot_position()

        # Mutate everything
        engine.position.size = 0.0
        engine.position.avg_entry = 0.0
        engine.position.realized_pnl = 0.0
        engine.position.params_at_entry = None
        engine.balance = 0.0
        engine.total_trades = 0
        engine.win_count = 0
        engine.loss_count = 0
        engine.trades.append("extra")

        engine.restore_position(snap)

        assert engine.position.size == 5.0
        assert engine.position.avg_entry == 95.0
        assert engine.position.realized_pnl == 1.23
        assert engine.position.params_at_entry == {"volatile_atr_mult": 2.0}
        assert engine.balance == 9500.0
        assert engine.total_trades == 7
        assert engine.win_count == 3
        assert engine.loss_count == 4
        assert len(engine.trades) == 1  # trimmed back to snapshot length

    def test_sell_above_min_confidence_full_closes(self):
        """Fix 6: any SELL >= min_confidence full-closes the position. The
        previous 50/50 split at conf=0.7 asymmetrically under-exited mid-
        confidence signals — spot-only, half-exit doesn't reduce risk
        proportionally."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        engine.position.size = 10.0
        engine.position.avg_entry = 90.0
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9100.0
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })

        # Mid-confidence SELL (0.66 > min_confidence=0.65) — used to do 50%,
        # now full-closes. Win/loss is counted because the position is closed.
        trade = engine.execute_signal("SELL", 0.66, "test full-close", "MOMENTUM")
        assert trade is not None, "Expected trade to be generated"
        assert engine.position.size == 0.0, "Expected full close at mid confidence"
        # 10 units @ avg 90 sold @ 100 = +100 profit → win
        assert engine.win_count == 1, "Full close with profit counts as win"
        assert engine.loss_count == 0

    def test_winning_round_trip_counted_correctly(self):
        """Full round trip (BUY → SELL close) with profit counts as WIN."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        # Simulate a profitable trade
        engine.position.size = 1.0
        engine.position.avg_entry = 90.0
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9910.0
        # Full close (conf > 0.7)
        trade = engine.execute_signal("SELL", 0.75, "close winner", "MOMENTUM")
        assert trade is not None
        assert engine.position.size == 0.0
        assert engine.win_count == 1
        assert engine.loss_count == 0

    def test_losing_round_trip_counted_correctly(self):
        """Full close with loss counts as LOSS."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        engine.position.size = 1.0
        engine.position.avg_entry = 110.0  # entry above current price = loss
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9890.0
        trade = engine.execute_signal("SELL", 0.75, "close loser", "MOMENTUM")
        assert trade is not None
        assert engine.position.size == 0.0
        assert engine.win_count == 0
        assert engine.loss_count == 1

    def test_breakeven_counted_as_loss(self):
        """Break-even trade (P&L == 0) counts as loss per industry standard."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        engine.position.size = 1.0
        engine.position.avg_entry = 100.0  # same as current price = break-even
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9900.0
        trade = engine.execute_signal("SELL", 0.75, "close breakeven", "MOMENTUM")
        assert trade is not None
        assert engine.position.size == 0.0
        assert engine.win_count == 0, "Break-even should not be a win"
        assert engine.loss_count == 1, "Break-even should count as loss"
        assert engine.total_trades == 1
        assert engine.total_trades == engine.win_count + engine.loss_count

    def test_rollback_restores_equity_history(self):
        """Rollback restores equity_history, peak_equity, and max_drawdown."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        snap = engine.snapshot_position()
        orig_eq_len = len(engine.equity_history)
        orig_peak = engine.peak_equity
        orig_dd = engine.max_drawdown

        # Execute a losing sell that would increase drawdown
        engine.position.size = 5.0
        engine.position.avg_entry = 110.0
        engine.balance = 9450.0
        engine.execute_signal("SELL", 0.75, "losing sell", "MOMENTUM")

        # Restore — equity_history, peak_equity, max_drawdown should revert
        engine.restore_position(snap)
        assert len(engine.equity_history) == orig_eq_len
        assert engine.peak_equity == orig_peak
        assert engine.max_drawdown == orig_dd

    def test_rollback_restores_after_failed_buy(self):
        """Simulates engine commit + rollback for a failed BUY order."""
        # hold_through default ON would re-apply rails on execute_signal
        engine = HydraEngine(
            initial_balance=10000, asset="SOL/USDC", hold_through=False
        )
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })

        # Snapshot before trade
        snap = engine.snapshot_position()
        orig_balance = engine.balance
        orig_pos = engine.position.size
        orig_trades = engine.total_trades

        # Execute a BUY (engine commits internally)
        trade = engine.execute_signal("BUY", 0.70, "test buy", "MOMENTUM")
        assert trade is not None, "Expected BUY trade to be generated"
        assert engine.position.size > orig_pos
        assert engine.balance < orig_balance
        # total_trades only increments on round-trip close, not BUY
        assert engine.total_trades == orig_trades

        # Simulate Kraken failure → rollback
        engine.restore_position(snap)
        assert engine.balance == orig_balance
        assert engine.position.size == orig_pos
        assert engine.total_trades == orig_trades

    def test_rollback_restores_after_failed_sell(self):
        """Simulates engine commit + rollback for a failed SELL order."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        engine.position.size = 5.0
        engine.position.avg_entry = 95.0
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9525.0

        snap = engine.snapshot_position()

        # Execute a full SELL (conf > 0.7 → 100%)
        trade = engine.execute_signal("SELL", 0.75, "test sell", "MOMENTUM")
        assert trade is not None, "Expected SELL trade to be generated"
        assert engine.position.size == 0.0
        assert engine.win_count == 1  # profitable trade

        # Simulate failure → rollback
        engine.restore_position(snap)
        assert engine.position.size == 5.0
        assert engine.position.avg_entry == 95.0
        assert engine.win_count == 0
        assert engine.balance == 9525.0

    def test_realized_pnl_accumulates_across_partials(self):
        """Multiple partial sells accumulate realized_pnl; final close uses
        total. Under Fix 6, signal-driven SELL always full-closes, so partial
        sells now happen via reconcile_partial_fill when the exchange only
        fills part of an optimistic full-close commitment. This test drives
        that equivalent path to verify the accumulation invariant still
        holds."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 100, "timestamp": float(i),
            })
        engine.position.size = 10.0
        engine.position.avg_entry = 90.0
        engine.position.params_at_entry = engine.snapshot_params()
        engine.balance = 9100.0

        # Signal #1: full-close optimistically committed (conf > min)
        snap1 = engine.snapshot_position()
        t1 = engine.execute_signal("SELL", 0.66, "sell signal 1", "MOMENTUM")
        assert t1 is not None
        # Engine believes position is zero (optimistic full close); but only
        # half of the order actually filled on the exchange — reconcile to
        # reflect reality (5 units sold, 5 units still held).
        engine.reconcile_partial_fill(
            side="SELL", placed_amount=10.0, vol_exec=5.0, limit_price=100.0,
            pre_trade_snapshot=snap1,
        )
        assert engine.position.size == 5.0, "5 units remain after partial fill"
        pnl_after_partial = engine.position.realized_pnl
        assert pnl_after_partial > 0, "Partial fill realized profit"

        # Signal #2: full-close the remaining 5 units (this time fills fully)
        t2 = engine.execute_signal("SELL", 0.75, "close remainder", "MOMENTUM")
        assert t2 is not None, "Expected closing SELL trade to be generated"
        assert engine.position.size == 0, "Position fully closed"
        assert engine.win_count == 1
        assert t2.profit > pnl_after_partial, "Close profit includes accumulated partial"


class TestCompetitionMode:
    def test_engine_accepts_competition_sizing(self):
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC", sizing=SIZING_COMPETITION)
        assert engine.sizer.kelly_multiplier == 0.50
        assert engine.sizer.min_confidence == 0.65
        assert engine.sizer.max_position_pct == 0.40

    def test_engine_defaults_to_conservative(self):
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        assert engine.sizer.kelly_multiplier == 0.25
        assert engine.sizer.min_confidence == 0.65

    def test_competition_uses_half_kelly(self):
        """Competition mode positions are larger than conservative for same confidence."""
        cons_sizer = PositionSizer(**SIZING_CONSERVATIVE)
        comp_sizer = PositionSizer(**SIZING_COMPETITION)
        # At 0.80 confidence, half-Kelly should produce larger size than quarter-Kelly
        cons_size = cons_sizer.calculate(0.80, 10000, 100.0, "SOL/USDC")
        comp_size = comp_sizer.calculate(0.80, 10000, 100.0, "SOL/USDC")
        assert comp_size > cons_size
        # Half-Kelly should be roughly 2x quarter-Kelly (before caps)
        assert 1.5 < (comp_size / cons_size) < 2.5


# ═══════════════════════════════════════════════════════════════
# 6. BRAIN TESTS (no API key needed — tests fallback behavior)
# ═══════════════════════════════════════════════════════════════

def _brain_available():
    """Check if brain dependencies (anthropic or openai SDK) are installed."""
    try:
        from hydra_brain import HAS_ANTHROPIC, HAS_OPENAI
        return HAS_ANTHROPIC or HAS_OPENAI
    except ImportError:
        return False


class TestBrain:
    def _skip_if_no_sdk(self):
        """Skip brain tests that require an SDK to construct a HydraBrain."""
        if not _brain_available():
            raise SkipTest("anthropic/openai SDK not installed")

    def _make_state(self, action="BUY", confidence=0.7):
        """Build a minimal engine state dict for brain testing."""
        return {
            "tick": 1,
            "timestamp": 0,
            "asset": "SOL/USDC",
            "price": 100.0,
            "regime": "TREND_UP",
            "strategy": "MOMENTUM",
            "signal": {"action": action, "confidence": confidence, "reason": "Test signal"},
            "position": {"size": 0, "avg_entry": 0, "unrealized_pnl": 0},
            "portfolio": {"balance": 1000, "equity": 1000, "pnl_pct": 0, "max_drawdown_pct": 0, "peak_equity": 1000},
            "performance": {"total_trades": 5, "win_count": 3, "loss_count": 2, "win_rate_pct": 60, "sharpe_estimate": 1.5},
            "indicators": {"rsi": 55, "macd_line": 0.3, "macd_signal": -0.2, "macd_histogram": 0.5, "bb_upper": 105, "bb_middle": 100, "bb_lower": 95, "bb_width": 0.1},
            "trend": {"ema20": 101.0, "ema50": 99.5},
            "volatility": {"atr": 2.5, "atr_pct": 2.5},
            "volume": {"current": 150.0, "avg_20": 120.0},
            "candle_interval": 5,
            "candle_status": "closed",
            "candles": [{"o": 99, "h": 101, "l": 98, "c": 100, "t": i} for i in range(10)],
        }

    def test_fallback_decision(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        # Create brain with dummy key (won't actually call API)
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        state = self._make_state("BUY", 0.72)
        # Force fallback by disabling API
        brain.api_available = False
        decision = brain.deliberate(state)
        assert decision.fallback is True
        assert decision.final_signal == "BUY"
        assert decision.confidence_adj == 0.72
        assert decision.action == "CONFIRM"

    def test_fallback_preserves_hold(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        state = self._make_state("HOLD", 0.3)
        brain.api_available = False
        decision = brain.deliberate(state)
        assert decision.final_signal == "HOLD"
        assert decision.fallback is True

    def test_budget_guard(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key", max_daily_cost=0.0)
        state = self._make_state("BUY", 0.8)
        decision = brain.deliberate(state)
        assert decision.fallback is True  # budget exceeded immediately

    def test_get_stats(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        stats = brain.get_stats()
        assert "active" in stats
        assert "decisions_today" in stats
        assert "cost_today" in stats
        assert "model" in stats
        assert stats["decisions_today"] == 0

    def test_json_parser(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        # Direct JSON
        assert brain._parse_json('{"a": 1}') == {"a": 1}
        # Wrapped in markdown
        assert brain._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
        # Invalid
        assert brain._parse_json('not json') is None
        # Empty
        assert brain._parse_json('') is None

    def test_prompt_builders(self):
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        state = self._make_state()
        prompt = brain._build_analyst_prompt(state)
        assert "SOL/USDC" in prompt
        assert "TREND_UP" in prompt
        assert "RSI=" in prompt

        risk_prompt = brain._build_risk_prompt(state, {"thesis": "test", "conviction": 0.7, "signal_agreement": True, "concern": None})
        assert "ENGINE SIGNAL" in risk_prompt
        assert "QUANT THESIS" in risk_prompt  # v2.14 renamed from "ANALYST THESIS"
        # New fields from enriched prompts
        assert "EMA20=" in prompt
        assert "ATR=" in prompt
        assert "VOLUME:" in prompt
        assert "MACD=[line=" in prompt
        # Risk prompt now has pair/price/regime and key indicators
        assert "SOL/USDC" in risk_prompt
        assert "RSI=" in risk_prompt
        assert "Balance=" in risk_prompt
        assert "Peak=" in risk_prompt
        # Timeframe and candle status in prompts
        assert "TIMEFRAME: 5m" in prompt
        assert "CANDLE: closed" in prompt
        assert "TIMEFRAME: 5m" in risk_prompt

    def test_indicators_include_macd_full(self):
        """Indicators dict includes MACD line, signal, and histogram."""
        engine = HydraEngine(initial_balance=10000, asset="SOL/USDC")
        for i in range(60):
            engine.ingest_candle({
                "open": 100 + i * 0.1, "high": 101 + i * 0.1,
                "low": 99 + i * 0.1, "close": 100 + i * 0.1, "volume": 100,
            })
        state = engine.tick()
        ind = state["indicators"]
        assert "macd_line" in ind
        assert "macd_signal" in ind
        assert "macd_histogram" in ind

    def test_decision_history_per_pair(self):
        """Decision history is keyed per pair, not a shared flat list."""
        self._skip_if_no_sdk()
        from hydra_brain import HydraBrain
        brain = HydraBrain(anthropic_key="sk-ant-test-fake-key")
        # decision_history should be a dict (per-pair), not a list
        assert isinstance(brain.decision_history, dict)
        # Simulate recording decisions for different pairs
        brain.decision_history["SOL/USDC"] = [
            {"tick": 1, "action": "CONFIRM", "signal": "BUY", "conviction": 0.7, "escalated": False},
        ]
        brain.decision_history["BTC/USDC"] = [
            {"tick": 1, "action": "OVERRIDE", "signal": "HOLD", "conviction": 0.3, "escalated": True},
        ]
        # Verify per-pair isolation
        assert len(brain.decision_history["SOL/USDC"]) == 1
        assert len(brain.decision_history["BTC/USDC"]) == 1
        assert brain.decision_history["SOL/USDC"][0]["signal"] == "BUY"
        assert brain.decision_history["BTC/USDC"][0]["signal"] == "HOLD"
        # Verify analyst prompt reads only the current pair's history
        state = self._make_state()
        state["asset"] = "SOL/USDC"
        prompt = brain._build_analyst_prompt(state)
        assert "CONFIRM BUY" in prompt
        assert "OVERRIDE HOLD" not in prompt  # BTC/USDC history should not leak

# ═══════════════════════════════════════════════════════════════
# TEST: HF-002 — execute_signal must respect halted engine
# ═══════════════════════════════════════════════════════════════

class TestHaltedEngineExecuteSignal:
    """Regression tests for HF-002 (execute_signal bypasses halt check).

    Before the fix, only tick() checked the halted flag. Any caller that
    invoked execute_signal() directly (e.g., swap handler at
    hydra_agent.py:1337) would bypass the halt check. The fix adds an
    early return in _maybe_execute, so all execution paths respect halt.
    """

    def _halted_engine(self):
        from hydra_engine import HydraEngine
        # hold_through default ON would convert mid-TREND_UP SELL → HOLD
        eng = HydraEngine(
            initial_balance=100.0, asset="SOL/USDC", hold_through=False
        )
        # Seed enough candles to pass warmup
        for i in range(60):
            price = 100.0 + i * 0.1
            eng.ingest_candle({
                "open": price, "high": price, "low": price,
                "close": price, "volume": 100.0,
                "timestamp": float(1700000000 + i * 300),
            })
        eng.halted = True
        eng.halt_reason = "test: simulated circuit breaker"
        return eng

    def test_execute_signal_buy_returns_none_when_halted(self):
        eng = self._halted_engine()
        result = eng.execute_signal("BUY", 0.75, "test")
        assert result is None, f"expected None from halted execute_signal, got {result!r}"

    def test_execute_signal_sell_allowed_when_halted_with_position(self):
        """PR-A: circuit breaker must not trap inventory — SELL still runs."""
        eng = self._halted_engine()
        eng.position.size = 0.1
        eng.position.avg_entry = 95.0
        result = eng.execute_signal("SELL", 0.80, "test")
        assert result is not None, "halted engine must allow risk-reducing SELL"
        assert result.action == "SELL"
        assert eng.position.size == 0.0

    def test_halted_engine_buy_no_position_change(self):
        eng = self._halted_engine()
        pre_balance = eng.balance
        pre_position = eng.position.size
        _ = eng.execute_signal("BUY", 0.75, "test")
        assert eng.balance == pre_balance, "halted execute_signal must not change balance"
        assert eng.position.size == pre_position, "halted execute_signal must not change position"


# ═══════════════════════════════════════════════════════════════
# v2.11.0 — informational-only (tradable=False) engine guard
# ═══════════════════════════════════════════════════════════════

class TestTradableFlag:
    """Verifies HydraEngine's `tradable` gate (v2.11.0).

    When `tradable=False`, _maybe_execute and execute_signal must
    short-circuit to None; the drawdown-based circuit breaker must not
    fire; and snapshot/restore must preserve the flag with a backward-
    compatible default of True on pre-2.11.0 snapshots.
    """

    def _seeded_engine(self, tradable: bool = True) -> HydraEngine:
        eng = HydraEngine(initial_balance=1.0, asset="SOL/BTC", tradable=tradable)
        # Enough candles to clear warmup + enable sizer logic.
        for i in range(60):
            price = 0.0011 + i * 0.000001
            eng.ingest_candle({
                "open": price, "high": price, "low": price,
                "close": price, "volume": 100.0,
                "timestamp": float(1700000000 + i * 300),
            })
        return eng

    def test_default_tradable_true(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        assert eng.tradable is True, "tradable must default to True for backward compat"

    def test_execute_signal_buy_returns_none_when_not_tradable(self):
        eng = self._seeded_engine(tradable=False)
        result = eng.execute_signal("BUY", 0.80, "test")
        assert result is None
        # And no state mutation.
        assert eng.position.size == 0.0
        assert eng.balance == 1.0

    def test_execute_signal_sell_returns_none_when_not_tradable(self):
        eng = self._seeded_engine(tradable=False)
        eng.position.size = 0.5
        eng.position.avg_entry = 0.001
        pre_balance = eng.balance
        result = eng.execute_signal("SELL", 0.85, "test")
        assert result is None
        assert eng.balance == pre_balance
        assert eng.position.size == 0.5, "position must not change on non-tradable engine"

    def test_circuit_breaker_suppressed_when_not_tradable(self):
        eng = self._seeded_engine(tradable=False)
        # Force a drawdown that would halt a tradable engine.
        eng.peak_equity = 100.0
        eng.balance = 50.0
        eng.position.size = 0
        state = eng.tick()
        assert state["halted"] is False, (
            "tradable=False engine must not halt via circuit breaker — its "
            "phantom equity is meaningless"
        )

    def test_circuit_breaker_still_fires_when_tradable(self):
        # Ensure we haven't broken the existing circuit breaker for
        # real (tradable) engines. Mirrors the existing test_circuit_breaker
        # but uses the same helper to minimize drift.
        eng = HydraEngine(initial_balance=10000, asset="BTC/USD")
        eng.peak_equity = 10000
        eng.balance = 8000
        eng.position.size = 0
        for i in range(60):
            eng.ingest_candle({
                "open": 50000, "high": 50100, "low": 49900,
                "close": 50000, "volume": 100,
            })
        state = eng.tick()
        assert state["halted"] is True

    def test_snapshot_roundtrip_preserves_flag(self):
        eng = self._seeded_engine(tradable=False)
        snap = eng.snapshot_position()
        assert snap.get("tradable") is False
        eng2 = self._seeded_engine(tradable=True)
        eng2.restore_position(snap)
        assert eng2.tradable is False

    def test_missing_tradable_key_defaults_true(self):
        # Pre-2.11.0 snapshot: no `tradable` key. Engine must come back
        # with tradable=True so existing resume flows are unaffected.
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC", tradable=False)
        legacy_snap = {
            "balance": 100.0,
            "position_size": 0.0,
            "position_avg_entry": 0.0,
            "position_realized_pnl": 0.0,
            "position_params_at_entry": None,
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "trades_len": 0,
            "equity_history_len": 0,
            "peak_equity": 100.0,
            "max_drawdown": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            # no "tradable" key
        }
        eng.restore_position(legacy_snap)
        assert eng.tradable is True


# ═══════════════════════════════════════════════════════════════
# TEST: HF-004 — snapshot_runtime/restore_runtime round-trip for trades
# ═══════════════════════════════════════════════════════════════

class TestSnapshotTradesRoundTrip:
    """Regression tests for HF-004 (trades list lost on --resume).

    Before the fix, snapshot_runtime omitted self.trades, so restore_runtime
    started every resumed session with an empty trades list even though
    total_trades/win_count/loss_count were restored correctly. This broke
    per-pair P&L calculations that iterate over engine.trades.
    """

    def _make_engine_with_trades(self):
        from hydra_engine import HydraEngine, Trade
        import time as _time
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        # Seed candles for price history
        for i in range(10):
            price = 100.0 + i
            eng.ingest_candle({
                "open": price, "high": price, "low": price,
                "close": price, "volume": 100.0,
                "timestamp": float(1700000000 + i * 300),
            })
        # Manually append trades
        eng.trades.append(Trade(
            action="BUY", asset="SOL/USDC", price=100.0, amount=0.5,
            value=50.0, reason="test buy", confidence=0.75, strategy="MOMENTUM",
            timestamp=1700000000.0,
        ))
        eng.trades.append(Trade(
            action="SELL", asset="SOL/USDC", price=110.0, amount=0.5,
            value=55.0, reason="test sell", confidence=0.80, strategy="MOMENTUM",
            timestamp=1700000300.0, profit=5.0,
        ))
        eng.trades.append(Trade(
            action="BUY", asset="SOL/USDC", price=108.0, amount=0.3,
            value=32.4, reason="second buy", confidence=0.65, strategy="MEAN_REVERSION",
            timestamp=1700000600.0, params_at_entry={"ema_short": 20, "ema_long": 50},
        ))
        eng.total_trades = 1
        eng.win_count = 1
        eng.loss_count = 0
        return eng

    def test_snapshot_includes_trades(self):
        eng = self._make_engine_with_trades()
        snap = eng.snapshot_runtime()
        assert "trades" in snap, "snapshot must include 'trades' key (HF-004)"
        assert len(snap["trades"]) == 3, f"expected 3 trades, got {len(snap['trades'])}"

    def test_snapshot_trades_fields_serialized(self):
        eng = self._make_engine_with_trades()
        snap = eng.snapshot_runtime()
        t0 = snap["trades"][0]
        # Every Trade field must be present
        for key in ("action", "asset", "price", "amount", "value", "reason",
                     "confidence", "strategy", "timestamp", "profit", "params_at_entry"):
            assert key in t0, f"trade dict missing key {key!r}"
        assert t0["action"] == "BUY"
        assert t0["price"] == 100.0
        assert t0["amount"] == 0.5

    def test_restore_runtime_rebuilds_trades(self):
        from hydra_engine import HydraEngine
        eng = self._make_engine_with_trades()
        snap = eng.snapshot_runtime()

        # Create a fresh engine and restore from snapshot
        fresh = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        assert len(fresh.trades) == 0  # fresh engine has no trades
        fresh.restore_runtime(snap)
        assert len(fresh.trades) == 3, f"expected 3 restored trades, got {len(fresh.trades)}"

        # Verify the first trade's fields round-tripped correctly
        t = fresh.trades[0]
        assert t.action == "BUY"
        assert t.asset == "SOL/USDC"
        assert t.price == 100.0
        assert t.amount == 0.5
        assert t.confidence == 0.75
        assert t.strategy == "MOMENTUM"

    def test_restore_runtime_preserves_profit_field(self):
        from hydra_engine import HydraEngine
        eng = self._make_engine_with_trades()
        snap = eng.snapshot_runtime()
        fresh = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        fresh.restore_runtime(snap)
        # Second trade had profit=5.0
        assert fresh.trades[1].profit == 5.0

    def test_restore_runtime_preserves_params_at_entry(self):
        from hydra_engine import HydraEngine
        eng = self._make_engine_with_trades()
        snap = eng.snapshot_runtime()
        fresh = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        fresh.restore_runtime(snap)
        # Third trade had params_at_entry set
        assert fresh.trades[2].params_at_entry == {"ema_short": 20, "ema_long": 50}

    def test_restore_runtime_tolerates_legacy_snapshot_without_trades(self):
        """A snapshot from before the HF-004 fix has no 'trades' key. Restore
        must silently fall back to an empty list without crashing."""
        from hydra_engine import HydraEngine
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        # Simulate a legacy snapshot (no "trades" key)
        legacy_snap = {
            "asset": "SOL/USDC",
            "initial_balance": 100.0,
            "balance": 90.0,
            "position": {"asset": "SOL/USDC", "size": 0.1, "avg_entry": 100.0,
                          "unrealized_pnl": 0.0, "realized_pnl": 0.0,
                          "params_at_entry": None},
            "peak_equity": 100.0,
            "max_drawdown": 0.0,
            "win_count": 0, "loss_count": 0, "total_trades": 0, "tick_count": 10,
            "halted": False, "halt_reason": "",
            "equity_history": [],
            "candles": [],
        }
        eng.restore_runtime(legacy_snap)
        assert eng.trades == [], "legacy snapshot restore should leave trades empty"

    def test_restore_runtime_skips_non_dict_entries(self):
        """restore_runtime must silently skip non-dict entries (string, None, list).
        Valid dicts with missing optional fields get defaults."""
        from hydra_engine import HydraEngine
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        snap = {
            "trades": [
                {"action": "BUY", "asset": "SOL/USDC", "price": 100.0, "amount": 0.5,
                 "value": 50.0, "reason": "ok", "confidence": 0.75, "strategy": "MOMENTUM",
                 "timestamp": 1700000000.0},
                "not-a-dict",  # skipped
                None,  # skipped
                ["not", "a", "dict"],  # skipped
                {"action": "BUY", "asset": "SOL/USDC", "price": 110.0, "amount": 0.3,
                 "value": 33.0, "reason": "ok2", "confidence": 0.65, "strategy": "MOMENTUM",
                 "timestamp": 1700000300.0},
            ],
        }
        eng.restore_runtime(snap)
        assert len(eng.trades) == 2, f"expected 2 good trades after skipping non-dicts, got {len(eng.trades)}"

    def test_restore_runtime_handles_unparseable_numeric_fields(self):
        """A dict whose 'price' isn't a number should be skipped via the
        try/except in the restore loop, not crash the whole restore."""
        from hydra_engine import HydraEngine
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USDC")
        snap = {
            "trades": [
                {"action": "BUY", "asset": "SOL/USDC", "price": "not-a-number",
                 "amount": 0.5, "value": 50.0, "reason": "bad", "confidence": 0.75,
                 "strategy": "MOMENTUM", "timestamp": 1700000000.0},
                {"action": "BUY", "asset": "SOL/USDC", "price": 100.0, "amount": 0.5,
                 "value": 50.0, "reason": "ok", "confidence": 0.75,
                 "strategy": "MOMENTUM", "timestamp": 1700000000.0},
            ],
        }
        eng.restore_runtime(snap)
        assert len(eng.trades) == 1, f"expected 1 good trade after skipping unparseable, got {len(eng.trades)}"


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    skipped = 0
    errors = []

    test_classes = [
        TestEMA, TestRSI, TestATR, TestBollingerBands, TestMACD,
        TestRegimeDetection, TestSignalGeneration, TestPositionSizer,
        TestHydraEngine, TestSnapshotAndRollback, TestCompetitionMode, TestBrain,
        TestHaltedEngineExecuteSignal, TestTradableFlag, TestSnapshotTradesRoundTrip,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except SkipTest as e:
                skipped += 1
                print(f"  SKIP  {test_name}: {e}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  ERROR {test_name}: {e}")

    print(f"\n  {'='*50}")
    print(f"  Results: {passed} passed, {failed} failed, {skipped} skipped, {passed + failed + skipped} total")
    print(f"  {'='*50}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
