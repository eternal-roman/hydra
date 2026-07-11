#!/usr/bin/env python3
"""
HYDRA Engine — Hyper-adaptive Dynamic Regime-switching Universal Agent
Core strategy engine: indicators, regime detection, signal generation, position sizing.
Portable pure-Python. No dependencies beyond standard library + json.

Usage:
    from hydra_engine import HydraEngine
    engine = HydraEngine()
    engine.ingest_candle({"open": 95000, "high": 95500, "low": 94500, "close": 95200, "volume": 150})
    state = engine.tick()
    print(state)  # {'regime': 'RANGING', 'strategy': 'MEAN_REVERSION', 'signal': {...}, ...}
"""

import math
import os
import statistics
import sys
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

# pair_registry is pure stdlib (dataclasses/typing only); importing it
# does NOT violate the engine's "no numpy/pandas" isolation rule.
from hydra_pair_registry import STABLE_QUOTES


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"

class Strategy(str, Enum):
    MOMENTUM = "MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    GRID = "GRID"
    DEFENSIVE = "DEFENSIVE"

class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: float = field(default_factory=time.time)

@dataclass
class Signal:
    action: SignalAction
    confidence: float
    reason: str
    strategy: Strategy
    indicators: Dict[str, float] = field(default_factory=dict)

@dataclass
class Trade:
    action: str  # BUY or SELL
    asset: str
    price: float
    amount: float
    value: float
    reason: str
    confidence: float
    strategy: str
    timestamp: float = field(default_factory=time.time)
    profit: Optional[float] = None
    params_at_entry: Optional[Dict[str, float]] = None

@dataclass
class Position:
    asset: str
    size: float = 0.0
    avg_entry: float = 0.0
    unrealized_pnl: float = 0.0
    params_at_entry: Optional[Dict[str, float]] = None
    realized_pnl: float = 0.0  # Accumulated profit across partial sells of this position

    def update_pnl(self, current_price: float):
        if self.size > 0:
            self.unrealized_pnl = (current_price - self.avg_entry) * self.size
        else:
            self.unrealized_pnl = 0.0


# ═══════════════════════════════════════════════════════════════
# INDICATORS (Pure Python, no pandas/numpy)
# ═══════════════════════════════════════════════════════════════

class Indicators:
    """All indicator calculations. Input: list of floats. Output: float."""

    @staticmethod
    def ema(prices: List[float], period: int) -> float:
        """Exponential Moving Average."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        k = 2.0 / (period + 1)
        ema_val = sum(prices[:period]) / period
        for i in range(period, len(prices)):
            ema_val = prices[i] * k + ema_val * (1 - k)
        return ema_val

    @staticmethod
    def sma(prices: List[float], period: int) -> float:
        """Simple Moving Average."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        return sum(prices[-period:]) / period

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> float:
        """Relative Strength Index (0–100) using Wilder's exponential smoothing."""
        if len(prices) < period + 1:
            return 50.0
        # Seed with SMA of first `period` changes
        avg_gain = 0.0
        avg_loss = 0.0
        for i in range(1, period + 1):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                avg_gain += diff
            else:
                avg_loss -= diff
        avg_gain /= period
        avg_loss /= period
        # Wilder's exponential smoothing for remaining prices
        for i in range(period + 1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gain = diff if diff > 0 else 0.0
            loss = -diff if diff < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @staticmethod
    def atr(candles: List[Candle], period: int = 14) -> float:
        """Average True Range using Wilder's exponential smoothing."""
        if len(candles) < period + 1:
            return 0.0
        # Seed: SMA of the first `period` true ranges
        atr_val = 0.0
        for i in range(1, period + 1):
            atr_val += max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
        atr_val /= period
        # Wilder's smoothing for all remaining candles
        for i in range(period + 1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            atr_val = (atr_val * (period - 1) + tr) / period
        return atr_val

    @staticmethod
    def atr_pct_series(candles: List[Candle], period: int = 14) -> List[float]:
        """Rolling ATR-as-%-of-price series using Wilder's smoothing.

        Returns one ATR% value per candle from index ``period`` onward.
        Single O(n) pass — same math as :meth:`atr` but records intermediates.
        """
        n = len(candles)
        if n < period + 1:
            return []
        # Seed: SMA of first `period` true ranges
        atr_val = 0.0
        for i in range(1, period + 1):
            atr_val += max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
        atr_val /= period
        close = candles[period].close
        series = [(atr_val / close * 100) if close > 0 else 0.0]
        # Wilder's smoothing for remaining candles
        for i in range(period + 1, n):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            atr_val = (atr_val * (period - 1) + tr) / period
            close = candles[i].close
            series.append((atr_val / close * 100) if close > 0 else 0.0)
        return series

    @staticmethod
    def bb_width_series(
        prices: List[float], period: int = 20, std_mult: float = 2.0
    ) -> List[float]:
        """Rolling Bollinger Band width series.

        Returns one width value per price from index ``period-1`` onward.
        Width = (2 * std_mult * std) / mean, same formula as :meth:`bollinger_bands`.
        """
        n = len(prices)
        if n < period:
            return []
        series: List[float] = []
        for end in range(period, n + 1):
            sl = prices[end - period:end]
            mean = sum(sl) / period
            if mean <= 0:
                series.append(0.0)
                continue
            variance = sum((x - mean) ** 2 for x in sl) / period
            std = math.sqrt(variance)
            series.append((std_mult * 2 * std) / mean)
        return series

    @staticmethod
    def bollinger_bands(
        prices: List[float], period: int = 20, std_mult: float = 2.0
    ) -> Dict[str, float]:
        """Bollinger Bands: upper, middle, lower, width."""
        if len(prices) < period:
            p = prices[-1] if prices else 0.0
            return {"upper": p, "middle": p, "lower": p, "width": 0.0}
        sl = prices[-period:]
        mean = sum(sl) / period
        variance = sum((x - mean) ** 2 for x in sl) / period
        std = math.sqrt(variance)
        upper = mean + std_mult * std
        lower = mean - std_mult * std
        width = (std_mult * 2 * std) / mean if mean > 0 else 0.0
        return {"upper": upper, "middle": mean, "lower": lower, "width": width}

    @staticmethod
    def macd(
        prices: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9
    ) -> Dict[str, float]:
        """MACD: macd_line, signal_line, histogram."""
        if len(prices) < slow:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        # Build historical MACD series by computing EMA-fast minus EMA-slow at each point
        k_fast = 2.0 / (fast + 1)
        k_slow = 2.0 / (slow + 1)
        ema_f = sum(prices[:fast]) / fast
        ema_s = sum(prices[:slow]) / slow
        # Advance fast EMA to slow start point
        for i in range(fast, slow):
            ema_f = prices[i] * k_fast + ema_f * (1 - k_fast)
        macd_hist = []
        for i in range(slow, len(prices)):
            ema_f = prices[i] * k_fast + ema_f * (1 - k_fast)
            ema_s = prices[i] * k_slow + ema_s * (1 - k_slow)
            macd_hist.append(ema_f - ema_s)
        macd_line = macd_hist[-1] if macd_hist else 0.0
        # Signal line = EMA of MACD series
        if len(macd_hist) >= signal_period:
            k_sig = 2.0 / (signal_period + 1)
            sig = sum(macd_hist[:signal_period]) / signal_period
            for i in range(signal_period, len(macd_hist)):
                sig = macd_hist[i] * k_sig + sig * (1 - k_sig)
            signal_line = sig
        else:
            signal_line = macd_line
        histogram = macd_line - signal_line
        return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


# ═══════════════════════════════════════════════════════════════
# REGIME DETECTOR
# ═══════════════════════════════════════════════════════════════

class RegimeDetector:
    """Detects market regime from indicator values."""

    @staticmethod
    def detect(candles: List[Candle], prices: List[float],
               volatile_atr_mult: float = 1.8, volatile_bb_mult: float = 1.8,
               trend_ema_ratio: float = 1.005,
               volatile_atr_floor: float = 1.5,
               volatile_bb_floor: float = 0.03) -> Regime:
        if len(prices) < 50:
            return Regime.RANGING

        ema20 = Indicators.ema(prices, 20)
        ema50 = Indicators.ema(prices, 50)
        atr = Indicators.atr(candles)
        bb = Indicators.bollinger_bands(prices)
        current = prices[-1]
        atr_pct = (atr / current) * 100 if current > 0 else 0

        # Adaptive volatility threshold — derived from asset's own history.
        # VOLATILE fires only when current volatility is significantly above
        # the asset's own median, not a fixed absolute number.
        atr_series = Indicators.atr_pct_series(candles)
        if len(atr_series) >= 20:
            median_atr = statistics.median(atr_series)
            atr_threshold = max(volatile_atr_mult * median_atr, volatile_atr_floor)
        else:
            atr_threshold = volatile_atr_floor  # warmup fallback

        bb_series = Indicators.bb_width_series(prices)
        if len(bb_series) >= 20:
            median_bb = statistics.median(bb_series)
            bb_threshold = max(volatile_bb_mult * median_bb, volatile_bb_floor)
        else:
            bb_threshold = volatile_bb_floor

        if atr_pct > atr_threshold or bb["width"] > bb_threshold:
            return Regime.VOLATILE

        # Trend detection with tunable threshold
        down_ratio = 1.0 / trend_ema_ratio  # multiplicative mirror: 1.005 → 0.99502
        if ema20 > ema50 * trend_ema_ratio and current > ema20:
            return Regime.TREND_UP
        if ema20 < ema50 * down_ratio and current < ema20:
            return Regime.TREND_DOWN

        return Regime.RANGING


# ═══════════════════════════════════════════════════════════════
# STRATEGY SELECTOR
# ═══════════════════════════════════════════════════════════════

REGIME_STRATEGY_MAP = {
    Regime.TREND_UP: Strategy.MOMENTUM,
    Regime.TREND_DOWN: Strategy.DEFENSIVE,
    Regime.RANGING: Strategy.MEAN_REVERSION,
    Regime.VOLATILE: Strategy.GRID,
}


# ═══════════════════════════════════════════════════════════════
# SIGNAL GENERATOR
# ═══════════════════════════════════════════════════════════════

def _fmt_price(p: float) -> str:
    """Format a price for human-readable signal reasons.
    Uses full precision for small prices (e.g. SOL/BTC at 0.0012)."""
    if p < 0.01:
        return f"{p:.6f}"
    if p < 1:
        return f"{p:.4f}"
    return f"{p:.0f}"


def _chaikin_signed_volume(candle: "Candle") -> float:
    """OHLC-based proxy for trade-tape CVD. Chaikin Money Flow multiplier:
    signed_volume = volume × ((close − low) − (high − close)) / (high − low).
    Positive when close is near the high (net buying pressure within the
    candle); negative when close is near the low. Zero when high == low
    (flat candle — neither side dominant). Returns 0.0 for zero-volume
    candles so divergence math stays well-defined."""
    rng = candle.high - candle.low
    if rng <= 0 or candle.volume <= 0:
        return 0.0
    multiplier = ((candle.close - candle.low) - (candle.high - candle.close)) / rng
    return candle.volume * multiplier


def _linear_slope(values: List[float]) -> Optional[float]:
    """Ordinary least-squares slope over evenly-spaced (x=0,1,2…) samples.
    Used for divergence detection — absolute slope magnitude matters less
    than the SIGN DIFFERENCE between CVD and price slopes."""
    n = len(values)
    if n < 2:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


class SignalGenerator:
    """Generates BUY/SELL/HOLD signals based on active strategy.

    Confidence = BASE + signal_strength * weight + vol_bonus, where:
    - BASE (0.50) = signal generation floor (execution requires min_confidence ≥ 0.65)
    - signal_strength = 0-1 from dimensionless market ratios (MACD/ATR, BB penetration, RSI position)
    - weight = fills range from BASE to cap (self-consistent: BASE + weight + vol = cap)
    - vol_bonus = volume confirmation (small, confirmatory)

    All normalizations use dimensionless ratios so confidence is identical
    across the active triangle (e.g. SOL/USD ~$150, SOL/BTC ~0.0012, BTC/USD ~$95k).
    """

    # Signal generation floor — signals start at BASE and build upward.
    # Execution requires min_confidence (0.65), so only strong signals trade.
    BASE = 0.50
    # Volume is confirmatory, not primary — caps at VOLUME_WEIGHT above average
    VOLUME_WEIGHT = 0.05

    # PR-F: single warmup gate (matches RegimeDetector's 50-bar requirement).
    # Pre-PR-F signals could fire at 26 bars while regime was still forced RANGING.
    WARMUP_CANDLES = 50

    @staticmethod
    def generate(
        strategy: Strategy, prices: List[float], candles: List[Candle],
        momentum_rsi_lower: float = 30.0, momentum_rsi_upper: float = 70.0,
        mean_reversion_rsi_buy: float = 35.0, mean_reversion_rsi_sell: float = 65.0,
    ) -> Signal:
        if len(prices) < SignalGenerator.WARMUP_CANDLES:
            return Signal(
                action=SignalAction.HOLD,
                confidence=0.0,
                reason="Insufficient data — warming up indicators",
                strategy=strategy,
            )

        rsi = Indicators.rsi(prices)
        macd = Indicators.macd(prices)
        bb = Indicators.bollinger_bands(prices)
        atr = Indicators.atr(candles) if len(candles) >= 15 else 0.0
        current = prices[-1]

        # Volume context: ratio of latest candle volume to 20-period average
        vol_window = candles[-20:] if len(candles) >= 20 else candles
        volumes = [c.volume for c in vol_window]
        avg_volume = sum(volumes) / len(volumes) if volumes else 1.0
        vol_ratio = candles[-1].volume / avg_volume if avg_volume > 0 else 1.0

        atr_pct = (atr / current * 100) if current > 0 else 0.0

        # BB width factor: how expanded/compressed BB is vs ATR-derived reference.
        # BB = 2*std above + 2*std below = 4*std.  ATR approximates std for typical
        # candle distributions.  Reference bb_width = 4 * ATR / price = 4 * atr_pct/100.
        ref_bb_width = 4.0 * atr_pct / 100.0 if atr_pct > 0 else bb["width"]
        bb_width_factor = min(1.25, max(0.75, bb["width"] / ref_bb_width)) if ref_bb_width > 0 else 1.0

        # Previous MACD histogram for momentum direction detection (crossover/acceleration).
        # Avoids signaling on steady-state histogram — only signals fresh moves.
        prev_histogram = Indicators.macd(prices[:-1])["histogram"] if len(prices) >= 27 else 0.0

        ctx = {
            "atr": atr,
            "atr_pct": atr_pct,
            "vol_ratio": vol_ratio,
            "bb_width": bb["width"],
            "bb_width_factor": bb_width_factor,
            "prev_histogram": prev_histogram,
        }

        # 8 decimals everywhere — no price-dependent formatting threshold
        indicators = {
            "rsi": round(rsi, 2),
            "macd_line": round(macd["macd"], 8),
            "macd_signal": round(macd["signal"], 8),
            "macd_histogram": round(macd["histogram"], 8),
            "bb_upper": round(bb["upper"], 8),
            "bb_middle": round(bb["middle"], 8),
            "bb_lower": round(bb["lower"], 8),
            "bb_width": round(bb["width"], 6),
            "price": round(current, 8),
            "atr_pct": round(atr_pct, 4),
            "vol_ratio": round(vol_ratio, 4),
        }

        if strategy == Strategy.MOMENTUM:
            return SignalGenerator._momentum(rsi, macd, bb, current, indicators, ctx,
                                             rsi_lower=momentum_rsi_lower,
                                             rsi_upper=momentum_rsi_upper)
        elif strategy == Strategy.MEAN_REVERSION:
            return SignalGenerator._mean_reversion(rsi, bb, current, indicators, ctx,
                                                   rsi_buy=mean_reversion_rsi_buy,
                                                   rsi_sell=mean_reversion_rsi_sell)
        elif strategy == Strategy.GRID:
            return SignalGenerator._grid(bb, current, indicators, ctx)
        elif strategy == Strategy.DEFENSIVE:
            return SignalGenerator._defensive(rsi, current, indicators, ctx)
        else:
            return Signal(
                action=SignalAction.HOLD,
                confidence=SignalGenerator.BASE,
                reason="Unknown strategy",
                strategy=strategy,
                indicators=indicators,
            )

    @staticmethod
    def _vol_bonus(ctx) -> float:
        """Volume confirmation: 0 at average volume, VOLUME_WEIGHT at 2x average."""
        excess = max(0.0, ctx["vol_ratio"] - 1.0)
        return min(1.0, excess) * SignalGenerator.VOLUME_WEIGHT

    @staticmethod
    def _momentum(rsi, macd, bb, price, indicators, ctx,
                  rsi_lower: float = 30.0, rsi_upper: float = 70.0) -> Signal:
        BASE = SignalGenerator.BASE
        hist = macd["histogram"]
        prev = ctx["prev_histogram"]

        # Dead zone: MACD must exceed 10% of ATR to count as meaningful.
        # Filters noise oscillations around zero that cause whipsaw churn.
        noise_floor = ctx["atr"] * 0.10 if ctx["atr"] > 0 else 0.0

        # BUY: meaningful positive momentum that is building or a fresh crossover.
        # "Building" = histogram increasing; "fresh" = previous was at/below zero.
        if (rsi_lower < rsi < rsi_upper
                and hist > noise_floor
                and price > bb["middle"]
                and (hist > prev or prev <= 0)):
            macd_strength = min(1.0, abs(hist) / ctx["atr"]) if ctx["atr"] > 0 else 0.0
            vol = SignalGenerator._vol_bonus(ctx)
            # Cap 0.95: 3 entry conditions + direction confirmed
            conf = min(0.95, BASE + macd_strength * 0.40 + vol)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Momentum confirmed: MACD hist {hist:.2f} > 0, "
                       f"price {_fmt_price(price)} > BB mid {_fmt_price(bb['middle'])}, RSI {rsi:.1f}",
                strategy=Strategy.MOMENTUM,
                indicators=indicators,
            )

        # SELL: symmetric with BUY — require ALL of {RSI meaningful, MACD
        # fading past noise, price below BB mid, fading-or-fresh}. Previously
        # SELL used OR of just {rsi > upper+5, macd_fading}, letting a single
        # indicator noise flip us out of trending winners. Fix 5 makes entry
        # and exit structurally symmetric — "losing entries is just as bad as
        # losing exits" per the capital-discipline mandate.
        #
        # Emergency override at rsi > rsi_upper + 15: a truly extreme
        # overbought reading is still enough on its own (e.g., RSI > 85 on
        # default 70 threshold) — this preserves the "panic exit" capability
        # without letting moderate overbought (75-85) alone trigger.
        symmetric_sell = (
            rsi_lower < rsi < rsi_upper
            and hist < -noise_floor
            and price < bb["middle"]
            and (hist < prev or prev >= 0)
        )
        extreme_overbought = rsi > rsi_upper + 15
        if symmetric_sell or extreme_overbought:
            rsi_strength = max(0.0, rsi - rsi_upper) / (100.0 - rsi_upper) if rsi_upper < 100 else 0.0
            macd_strength = min(1.0, abs(hist) / ctx["atr"]) if hist < 0 and ctx["atr"] > 0 else 0.0
            primary = max(rsi_strength, macd_strength)
            vol = SignalGenerator._vol_bonus(ctx)
            conf = min(0.90, BASE + primary * 0.35 + vol)
            if extreme_overbought and not symmetric_sell:
                reason = f"Momentum fading: RSI {rsi:.1f} > {rsi_upper + 15:.0f} extreme overbought"
            else:
                reason = (f"Momentum fading: MACD hist {hist:.2f} < 0, "
                          f"price {_fmt_price(price)} < BB mid {_fmt_price(bb['middle'])}, RSI {rsi:.1f}")
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=reason,
                strategy=Strategy.MOMENTUM,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=BASE,
            reason=f"Awaiting momentum confirmation (RSI {rsi:.1f}, MACD hist {hist:.6f})",
            strategy=Strategy.MOMENTUM,
            indicators=indicators,
        )

    @staticmethod
    def _mean_reversion(rsi, bb, price, indicators, ctx,
                        rsi_buy: float = 35.0, rsi_sell: float = 65.0) -> Signal:
        BASE = SignalGenerator.BASE
        wf = ctx["bb_width_factor"]
        vol = SignalGenerator._vol_bonus(ctx)
        band_span = bb["upper"] - bb["lower"]

        if price <= bb["lower"] and rsi < rsi_buy:
            # Penetration: how far below lower band, normalized by band span
            # 0.5 at the band (signal just triggered), 1.0 at one full span below
            penetration = (bb["lower"] - price) / band_span if band_span > 0 else 0.0
            primary = min(1.0, 0.5 + penetration)
            # Range: BASE(0.50) + primary(0.30)*wf + vol(0.05) = 0.90 at wf=1.17
            conf = min(0.90, BASE + primary * 0.30 * wf + vol)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Mean reversion BUY: price {_fmt_price(price)} at/below BB lower {_fmt_price(bb['lower'])}, RSI {rsi:.1f} oversold",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        if price >= bb["upper"] and rsi > rsi_sell:
            penetration = (price - bb["upper"]) / band_span if band_span > 0 else 0.0
            primary = min(1.0, 0.5 + penetration)
            conf = min(0.90, BASE + primary * 0.30 * wf + vol)
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=f"Mean reversion SELL: price {_fmt_price(price)} at/above BB upper {_fmt_price(bb['upper'])}, RSI {rsi:.1f} overbought",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            # Use BASE for consistency with momentum/defensive HOLD signals.
            # HOLD confidence is informational only (no trade executes), but
            # inconsistent values were misleading on the dashboard.
            confidence=BASE,
            reason=f"Price {_fmt_price(price)} within bands ({_fmt_price(bb['lower'])}--{_fmt_price(bb['upper'])}), no reversion signal",
            strategy=Strategy.MEAN_REVERSION,
            indicators=indicators,
        )

    @staticmethod
    def _grid(bb, price, indicators, ctx) -> Signal:
        BASE = SignalGenerator.BASE
        grid_spacing = (bb["upper"] - bb["lower"]) / 5 if bb["upper"] != bb["lower"] else 1.0
        dist_from_lower = (price - bb["lower"]) / grid_spacing if grid_spacing > 0 else 2.5

        # Band span vs ATR: reference = 4*ATR (BB = 4*std, std approx ATR)
        band_span = bb["upper"] - bb["lower"]
        ref_span = 4.0 * ctx["atr"] if ctx["atr"] > 0 else band_span
        wf = min(1.25, max(0.75, band_span / ref_span)) if ref_span > 0 else 1.0

        if dist_from_lower < 1:
            zone_depth = 1.0 - dist_from_lower  # 0 at zone edge, 1 at band bottom
            # Range: (BASE + depth*0.35) * wf, cap 0.90
            conf = min(0.90, (BASE + zone_depth * 0.35) * wf)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Grid BUY: price {_fmt_price(price)} in bottom zone (zone {dist_from_lower:.1f}/5)",
                strategy=Strategy.GRID,
                indicators=indicators,
            )
        if dist_from_lower > 4:
            zone_depth = dist_from_lower - 4.0
            conf = min(0.90, (BASE + zone_depth * 0.35) * wf)
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=f"Grid SELL: price {_fmt_price(price)} in top zone (zone {dist_from_lower:.1f}/5)",
                strategy=Strategy.GRID,
                indicators=indicators,
            )
        # HOLD: distance from grid center (2.5) normalized to [0, 1]
        center_distance = abs(dist_from_lower - 2.5) / 2.5
        conf = 0.30 + center_distance * 0.15
        return Signal(
            action=SignalAction.HOLD,
            confidence=conf,
            reason=f"Grid HOLD: price in neutral zone {dist_from_lower:.1f}/5",
            strategy=Strategy.GRID,
            indicators=indicators,
        )

    @staticmethod
    def _defensive(rsi, price, indicators, ctx) -> Signal:
        BASE = SignalGenerator.BASE
        if rsi < 25:
            # Severity: 0 at RSI 25, 1 at RSI 5 (range = 20, from threshold to extreme)
            rsi_severity = (25.0 - rsi) / 20.0
            vol = SignalGenerator._vol_bonus(ctx)
            # Cap 0.75: defensive buys are cautious counter-trend nibbles
            # Range: BASE(0.50) + severity(0.20) + vol(0.05) = 0.75
            conf = min(0.75, BASE + rsi_severity * 0.20 + vol)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Defensive: extreme oversold RSI {rsi:.1f} — cautious nibble",
                strategy=Strategy.DEFENSIVE,
                indicators=indicators,
            )
        # Sell threshold: midpoint of TA-standard oversold (30) and neutral (50) = 40.
        # In TREND_DOWN, RSI oscillates 20-45; old threshold of 50 never fired.
        # Threshold 40 captures bounce exits before dead-cat-bounce failure.
        if rsi > 40:
            # PR-A / A3: floor conf at 0.65 so dashboard + any residual conf
            # gates agree with "SELL when RSI>40". Execution no longer needs
            # min_confidence on SELL (A2), but operators reading conf still
            # saw soft 0.50–0.64 SELLs that looked non-actionable.
            # Severity: 0 at RSI 40 → conf 0.65; 1 at RSI 100 → conf 0.90.
            rsi_severity = (rsi - 40.0) / 60.0
            conf = min(0.90, 0.65 + rsi_severity * 0.25)
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=f"Defensive: RSI {rsi:.1f} > 40 in downtrend — reducing exposure",
                strategy=Strategy.DEFENSIVE,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=BASE,
            reason=f"Defensive HOLD: preserving capital (RSI {rsi:.1f})",
            strategy=Strategy.DEFENSIVE,
            indicators=indicators,
        )


# ═══════════════════════════════════════════════════════════════
# POSITION SIZER (Quarter-Kelly Criterion)
# ═══════════════════════════════════════════════════════════════

# ─── Sizing Presets ───

SIZING_CONSERVATIVE = {
    "kelly_multiplier": 0.25,   # Quarter-Kelly
    "min_confidence": 0.65,
    "max_position_pct": 0.30,
}

SIZING_COMPETITION = {
    "kelly_multiplier": 0.50,   # Half-Kelly — more aggressive
    "min_confidence": 0.65,     # Quality filter — only ≥15% Kelly edge
    "max_position_pct": 0.40,   # Larger positions allowed
}


class PositionSizer:
    # Kraken minimum order sizes per base asset (ordermin). Class-level on
    # purpose: Kraken's ordermin/costmin are exchange-wide constants (same
    # value for SOL regardless of which pair loaded it), and hydra_agent.py
    # + tests access these as a shared registry. Multi-engine "isolation"
    # does not apply to exchange constants.
    MIN_ORDER_SIZE = {
        "SOL": 0.02,
        "BTC": 0.00005,
        "ETH": 0.001,
    }

    # Kraken minimum order cost per quote currency (costmin)
    MIN_COST = {
        "USDC": 0.5,
        "USD": 0.5,
        "BTC": 0.00002,
    }

    def __init__(self, kelly_multiplier: float = 0.25,
                 min_confidence: float = 0.65,
                 max_position_pct: float = 0.30):
        self.kelly_multiplier = kelly_multiplier
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct

    def apply_pair_limits(self, pair_constants: dict):
        """Update MIN_ORDER_SIZE and MIN_COST from dynamically loaded pair data.

        Mutates the class-level dicts so all PositionSizer instances see
        the updated values.  Hardcoded defaults remain for any asset not
        present in pair_constants.
        """
        for _friendly, info in pair_constants.items():
            base = info.get("base", "")
            quote = info.get("quote", "")
            if base and "ordermin" in info:
                PositionSizer.MIN_ORDER_SIZE[base] = info["ordermin"]
            if quote and "costmin" in info:
                PositionSizer.MIN_COST[quote] = info["costmin"]

    def calculate(self, confidence: float, balance: float, price: float,
                  asset: str = "") -> float:
        """Returns position size in asset units using Kelly criterion."""
        # Pair-aware costmin: use quote currency's minimum (e.g. 0.5 USD, 0.00002 BTC).
        # The fallback "USD" applies only when an asset is passed without
        # "/" — in normal use every asset is a triangle pair like "SOL/USD".
        quote = asset.split("/")[1] if "/" in asset else "USD"
        costmin = self.MIN_COST.get(quote, 0.5)

        if confidence < self.min_confidence or balance < costmin or price <= 0:
            return 0.0

        # PR-D / D1: excess-over-threshold Kelly.
        # Old formula edge=(conf*2-1) treated conf=0.65 as a 30% edge (massive
        # oversize for an uncalibrated heuristic). Now conf at the execution
        # floor maps to a small edge (0.10) and only conf→1.0 reaches full
        # edge 1.0. Kelly multiplier (quarter/half) still scales on top.
        span = max(1e-9, 1.0 - self.min_confidence)
        t = max(0.0, min(1.0, (confidence - self.min_confidence) / span))
        edge = 0.10 + 0.90 * t  # 0.10 at min_conf → 1.0 at conf=1.0
        kelly = edge * self.kelly_multiplier

        position_value = kelly * balance

        # Enforce max position limit
        max_value = balance * self.max_position_pct
        position_value = min(position_value, max_value)

        # Enforce minimum cost (Kraken costmin per quote currency)
        if position_value < costmin:
            return 0.0

        size = position_value / price

        # Enforce Kraken minimum order sizes (ordermin per base asset)
        base_asset = asset.split("/")[0] if "/" in asset else asset
        min_size = self.MIN_ORDER_SIZE.get(base_asset, 0.02)
        if size < min_size:
            return 0.0

        return size


# ═══════════════════════════════════════════════════════════════
# ORDER BOOK ANALYZER
# ═══════════════════════════════════════════════════════════════

class OrderBookAnalyzer:
    """Analyzes order book depth to generate confidence modifiers.

    Parses Kraken depth data (bids/asks arrays), computes volume imbalance,
    spread, wall detection, and a signal-aware confidence modifier.
    """

    # Imbalance thresholds
    BULLISH_THRESHOLD = 1.5   # bid/ask ratio above this = bullish pressure
    BEARISH_THRESHOLD = 0.67  # bid/ask ratio below this = bearish pressure
    WALL_MULTIPLIER = 3.0     # single level > 3x average = wall detected
    MAX_BOOK_MODIFIER = 0.07  # max confidence adjustment from order book

    @staticmethod
    def analyze(depth_data: dict, signal_action: str = "HOLD") -> dict:
        """Analyze order book depth and return metrics with confidence modifier.

        Args:
            depth_data: Raw Kraken depth JSON with 'bids' and 'asks' arrays.
                        Each entry: [price_str, volume_str, timestamp].
            signal_action: Current signal ("BUY", "SELL", or "HOLD") to
                           determine directional modifier.

        Returns:
            dict with bid_volume, ask_volume, imbalance_ratio, spread_bps,
            bid_wall, ask_wall, confidence_modifier.
        """
        result = {
            "bid_volume": 0.0,
            "ask_volume": 0.0,
            "imbalance_ratio": 1.0,
            "spread_bps": 0.0,
            "bid_wall": False,
            "ask_wall": False,
            "confidence_modifier": 0.0,
        }

        # Extract bids and asks from Kraken depth format
        # Kraken returns: {"PAIR": {"bids": [...], "asks": [...]}}
        bids_raw = []
        asks_raw = []

        if isinstance(depth_data, dict):
            # Direct format: {"bids": [...], "asks": [...]}
            if "bids" in depth_data and "asks" in depth_data:
                bids_raw = depth_data["bids"]
                asks_raw = depth_data["asks"]
            else:
                # Nested format: {"BTCUSD": {"bids": [...], "asks": [...]}}
                for key, val in depth_data.items():
                    if isinstance(val, dict) and "bids" in val and "asks" in val:
                        bids_raw = val["bids"]
                        asks_raw = val["asks"]
                        break

        if not bids_raw or not asks_raw:
            return result

        # Parse top 10 levels: [[price, volume, timestamp], ...]
        bid_levels = []
        for entry in bids_raw[:10]:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                bid_levels.append((float(entry[0]), float(entry[1])))

        ask_levels = []
        for entry in asks_raw[:10]:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                ask_levels.append((float(entry[0]), float(entry[1])))

        if not bid_levels or not ask_levels:
            return result

        # Volume totals
        bid_volumes = [v for _, v in bid_levels]
        ask_volumes = [v for _, v in ask_levels]
        bid_volume = sum(bid_volumes)
        ask_volume = sum(ask_volumes)

        result["bid_volume"] = round(bid_volume, 6)
        result["ask_volume"] = round(ask_volume, 6)

        # Imbalance ratio
        if ask_volume > 0:
            result["imbalance_ratio"] = round(bid_volume / ask_volume, 4)

        # Spread in basis points
        best_bid = bid_levels[0][0]
        best_ask = ask_levels[0][0]
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            result["spread_bps"] = round((best_ask - best_bid) / mid * 10000, 1)

        # Wall detection: any single level > 3x the average
        avg_bid = bid_volume / len(bid_volumes) if bid_volumes else 0
        avg_ask = ask_volume / len(ask_volumes) if ask_volumes else 0
        result["bid_wall"] = any(v > avg_bid * OrderBookAnalyzer.WALL_MULTIPLIER for v in bid_volumes) if avg_bid > 0 else False
        result["ask_wall"] = any(v > avg_ask * OrderBookAnalyzer.WALL_MULTIPLIER for v in ask_volumes) if avg_ask > 0 else False

        # Confidence modifier based on imbalance and signal direction
        # Scales linearly: half of MAX at threshold, full MAX at extreme (ratio 3.0+ / 0.33-)
        ratio = result["imbalance_ratio"]
        modifier = 0.0
        cap = OrderBookAnalyzer.MAX_BOOK_MODIFIER
        half = cap / 2.0
        bull_range = 3.0 - OrderBookAnalyzer.BULLISH_THRESHOLD   # 1.5
        bear_range = OrderBookAnalyzer.BEARISH_THRESHOLD - 0.33  # 0.34

        if signal_action == "BUY":
            if ratio > OrderBookAnalyzer.BULLISH_THRESHOLD:
                # Strong bid support confirms buy
                excess = min(ratio - OrderBookAnalyzer.BULLISH_THRESHOLD, bull_range)
                modifier = min(cap, half + excess / bull_range * half)
            elif ratio < OrderBookAnalyzer.BEARISH_THRESHOLD:
                # Weak bids contradict buy
                excess = min(OrderBookAnalyzer.BEARISH_THRESHOLD - ratio, bear_range)
                modifier = max(-cap, -(half + excess / bear_range * half))
        elif signal_action == "SELL":
            if ratio > OrderBookAnalyzer.BULLISH_THRESHOLD:
                # Strong bids — don't sell into strength (half modifier)
                modifier = -half
            elif ratio < OrderBookAnalyzer.BEARISH_THRESHOLD:
                # Weak bids confirm sell
                excess = min(OrderBookAnalyzer.BEARISH_THRESHOLD - ratio, bear_range)
                modifier = min(cap, half + excess / bear_range * half)
        # HOLD: no modifier

        result["confidence_modifier"] = round(modifier, 4)
        return result


# ═══════════════════════════════════════════════════════════════
# CROSS-PAIR REGIME COORDINATOR
# ═══════════════════════════════════════════════════════════════

class CrossPairCoordinator:
    """Detects cross-pair regime divergences and generates coordinated signals.

    Monitors regime states across the active trading triangle and produces
    override signals when cross-pair evidence contradicts a single pair's
    signal.

    Hydra's strategy is fundamentally a triangle of three roles:
      stable_sol  — the SOL pair quoted in the active stable currency
      stable_btc  — the BTC pair quoted in the active stable currency
      bridge      — the SOL/BTC cross (quote-independent)

    The literal pair names depend on the active stable quote (USD vs USDC
    vs USDT), so the coordinator addresses pairs by their TradingTriangle
    role rather than by name. The override keys returned to consumers
    remain pair-symbol strings (e.g. "SOL/USD") because downstream code
    correlates by pair symbol — the role indirection is internal.

    Construction accepts either:
      - a TradingTriangle (preferred; explicit role binding), or
      - a List[str] of pair symbols (legacy; the triangle is derived
        best-effort from the list. If the list doesn't contain a complete
        triangle, the coordinator becomes a no-op — every rule's role
        lookup returns None and the guards short-circuit to empty
        overrides, matching the pre-v2.19 behavior when only one pair
        was passed.)
    """

    HISTORY_SIZE = 10

    # Rule 4 confluence parameters. CO_MOVE_THRESHOLD gates the boost on a
    # minimum Pearson correlation of log-returns — below this the pairs are
    # behaving independently and their simultaneous signals are coincidence,
    # not confluence. CONFLUENCE_WINDOW is the number of aligned candles
    # used to compute ρ (the engine keeps up to 250). CONFLUENCE_MAX_BONUS
    # caps the confidence boost — shares the +0.15 total-modifier budget
    # documented in CLAUDE.md with the order-book and FOREX modifiers.
    CO_MOVE_THRESHOLD = 0.5
    CONFLUENCE_WINDOW = 60
    CONFLUENCE_MAX_BONUS = 0.10

    def __init__(self, triangle_or_pairs):
        # Lazy-import TradingTriangle to keep the engine importable even
        # if hydra_config is broken — the coordinator falls back to
        # pair-list mode in that case.
        try:
            from hydra_config import TradingTriangle
        except Exception:
            TradingTriangle = None  # type: ignore

        if TradingTriangle is not None and isinstance(triangle_or_pairs, TradingTriangle):
            self.triangle = triangle_or_pairs
            self.pairs: List[str] = [p.cli_format for p in triangle_or_pairs.as_tuple()]
        elif isinstance(triangle_or_pairs, (list, tuple)):
            self.pairs = list(triangle_or_pairs)
            self.triangle = self._derive_triangle(self.pairs)
        else:
            raise TypeError(
                "CrossPairCoordinator requires a TradingTriangle or a "
                f"list of pair symbols; got {type(triangle_or_pairs).__name__}"
            )
        self.regime_history: Dict[str, List[str]] = {p: [] for p in self.pairs}

    @staticmethod
    def _derive_triangle(pairs: List[str]):
        """Best-effort triangle derivation from a list of pair symbols.

        Scans the list for a SOL-stable pair, a BTC-stable pair (matching
        quote), and SOL/BTC. Returns a TradingTriangle if all three are
        present and quotes match, else None.
        """
        try:
            from hydra_pair_registry import default_registry
            from hydra_config import TradingTriangle
        except Exception:
            return None
        reg = default_registry()
        sol_stable = None
        btc_stable = None
        bridge = None
        for sym in pairs:
            p = reg.get(sym)
            if p is None:
                continue
            if p.base == "SOL" and p.is_stable_quoted:
                sol_stable = p
            elif p.base == "BTC" and p.is_stable_quoted:
                btc_stable = p
            elif p.base == "SOL" and p.quote == "BTC":
                bridge = p
        if (sol_stable is not None and btc_stable is not None
                and bridge is not None
                and sol_stable.quote == btc_stable.quote):
            try:
                return TradingTriangle(
                    stable_sol=sol_stable,
                    stable_btc=btc_stable,
                    bridge=bridge,
                    quote=sol_stable.quote,
                )
            except Exception:
                return None
        return None

    def update(self, pair: str, regime: str):
        """Record regime state for a pair. Keeps last HISTORY_SIZE entries."""
        history = self.regime_history.setdefault(pair, [])
        history.append(regime)
        if len(history) > self.HISTORY_SIZE:
            self.regime_history[pair] = history[-self.HISTORY_SIZE:]

    def get_overrides(self, all_states: Dict[str, dict],
                      price_series: Optional[Dict[str, List[float]]] = None,
                      ) -> Dict[str, dict]:
        """Return signal overrides where cross-pair evidence contradicts single-pair signals.

        Rules (all defined in role terms; literal pair names depend on
        the active triangle's stable quote — e.g. SOL/USD when quote=USD,
        SOL/USDC when quote=USDC):

        1. BTC leads SOL down: If stable_btc is TREND_DOWN and stable_sol is
           still TREND_UP or RANGING → override stable_sol to DEFENSIVE.
        2. BTC recovery boost: If stable_btc is TREND_UP and stable_sol is
           TREND_DOWN → boost stable_sol confidence (recovery likely).
        3. Coordinated swap: If stable_sol is TREND_DOWN and bridge is
           TREND_UP → suggest selling stable_sol and buying bridge.
        4. Signal confluence: If bridge and stable_sol emit same-direction
           BUY or SELL AND their log-return correlation over the last
           CONFLUENCE_WINDOW candles exceeds CO_MOVE_THRESHOLD, boost the
           stable_sol confidence by a bounded covariance-weighted amount.
           Requires `price_series` to be supplied for both pairs — without
           it Rule 4 is a no-op (safe fallback).

        Args:
            all_states: per-pair state dicts from HydraEngine.tick(),
                keyed by pair cli_format ("SOL/USD", "BTC/USD", "SOL/BTC"
                — the registry canonicalizes upstream so legacy XBT/USDC
                forms are no longer expected here).
            price_series: optional per-pair close-price history. Only
                consumed by Rule 4. Keys match `all_states`.

        Returns:
            {pair: {"action": str, "signal": str, "confidence_adj": float,
                    "reason": str, "swap": optional dict,
                    "confluence_source": optional dict}}
        """
        overrides: Dict[str, dict] = {}

        if self.triangle is None:
            # Incomplete triangle — coordinator is a no-op. Pre-v2.19 the
            # equivalent was each lookup returning None and every rule
            # short-circuiting; we just exit early here for clarity.
            return overrides

        sol_key = self.triangle.stable_sol.cli_format
        btc_key = self.triangle.stable_btc.cli_format
        bridge_key = self.triangle.bridge.cli_format

        btc_state = all_states.get(btc_key)
        sol_state = all_states.get(sol_key)
        bridge_state = all_states.get(bridge_key)

        btc_regime = btc_state.get("regime") if btc_state else None
        sol_regime = sol_state.get("regime") if sol_state else None
        bridge_regime = bridge_state.get("regime") if bridge_state else None

        # Rule 1: BTC leads SOL down
        # stable_btc trending down while stable_sol hasn't reacted yet
        if btc_regime == "TREND_DOWN" and sol_regime in ("TREND_UP", "RANGING"):
            overrides[sol_key] = {
                "action": "OVERRIDE",
                "signal": "SELL",
                "confidence_adj": 0.8,
                "reason": "Cross-pair: BTC trending down — SOL likely to follow",
            }

        # Rule 2: BTC recovery boost
        # stable_btc trending up while stable_sol is still down — recovery likely
        rule2_recovery = (
            btc_regime == "TREND_UP" and sol_regime == "TREND_DOWN"
        )
        if rule2_recovery:
            sol_conf = 0.5
            if sol_state and sol_state.get("signal"):
                sol_conf = sol_state["signal"].get("confidence", 0.5)
            overrides[sol_key] = {
                "action": "ADJUST",
                "signal": "BUY",
                "confidence_adj": min(0.95, sol_conf + 0.15),
                "reason": "Cross-pair: BTC recovering — SOL recovery likely, boosting confidence",
            }

        # Rule 3: Coordinated swap
        # SOL weakening vs stable but strengthening vs BTC — rotate into BTC.
        # PR-E / E3–E4: do NOT overwrite Rule 2 recovery (prefer hold for bounce).
        # Also require bridge engine to be tradable when that flag is present
        # (info-only bridge cannot fund the buy leg).
        if sol_regime == "TREND_DOWN" and bridge_regime == "TREND_UP" and not rule2_recovery:
            sol_pos = 0.0
            if sol_state and sol_state.get("position"):
                sol_pos = sol_state["position"].get("size", 0.0)
            bridge_tradable = True
            if bridge_state is not None and "tradable" in bridge_state:
                bridge_tradable = bool(bridge_state.get("tradable"))
            # Only suggest swap if we actually hold SOL and bridge can trade
            if sol_pos > 0 and bridge_tradable:
                overrides[sol_key] = {
                    "action": "OVERRIDE",
                    "signal": "SELL",
                    "confidence_adj": 0.85,
                    "reason": (
                        f"Cross-pair swap: SOL weakening vs {self.triangle.quote} "
                        "but strong vs BTC — rotate to BTC"
                    ),
                    "swap": {
                        "sell_pair": sol_key,
                        "buy_pair": bridge_key,
                        "reason": (
                            f"{sol_key} TREND_DOWN + {bridge_key} TREND_UP "
                            "— coordinated rotation"
                        ),
                    },
                }

        # Rule 4: bridge ↔ stable_sol signal confluence.
        # When both same-direction signals fire AND the two pairs are
        # behaving as co-movers (ρ > CO_MOVE_THRESHOLD over the last
        # CONFLUENCE_WINDOW candles of log-returns), boost stable_sol
        # confidence. This converts the bridge from "dead informational
        # signal for a stable-only portfolio" into a second-order
        # confidence source on stable_sol, without triggering any
        # independent bridge trade (which is blocked at the engine level
        # by tradable=False whenever we hold no BTC). Rule 4 does not
        # override an action — it only ADJUSTs an existing BUY/SELL
        # confidence upward.
        rule3_active = (sol_key in overrides
                        and overrides[sol_key].get("action") == "OVERRIDE")
        if (not rule3_active and sol_state and bridge_state and price_series):
            sol_sig = (sol_state.get("signal") or {})
            bridge_sig = (bridge_state.get("signal") or {})
            sol_action = sol_sig.get("action")
            bridge_action = bridge_sig.get("action")
            if sol_action in ("BUY", "SELL") and sol_action == bridge_action:
                # Gate SELL confluence on actually holding SOL — same guard
                # as Rule 3 so we don't boost an exit signal we can't act on.
                sell_gate_ok = True
                if sol_action == "SELL":
                    pos = sol_state.get("position") or {}
                    sol_pos = pos.get("size", 0.0)
                    sell_gate_ok = sol_pos > 0
                if sell_gate_ok:
                    prices_stable = price_series.get(sol_key) or []
                    prices_bridge = price_series.get(bridge_key) or []
                    rho = CrossPairCoordinator.pair_correlation(
                        prices_stable, prices_bridge,
                        window=self.CONFLUENCE_WINDOW,
                    )
                    if rho > self.CO_MOVE_THRESHOLD:
                        bridge_conf = float(bridge_sig.get("confidence") or 0.0)
                        sol_conf = float(sol_sig.get("confidence") or 0.0)
                        bonus = CrossPairCoordinator.confluence_bonus(
                            rho, bridge_conf,
                            max_bonus=self.CONFLUENCE_MAX_BONUS,
                        )
                        if bonus > 0.0:
                            boosted = min(0.95, sol_conf + bonus)
                            overrides[sol_key] = {
                                "action": "ADJUST",
                                "signal": sol_action,
                                "confidence_adj": boosted,
                                "reason": (
                                    f"Rule 4 confluence: {bridge_key} {sol_action} "
                                    f"(conf {bridge_conf:.2f}) co-moves with {sol_key} "
                                    f"(ρ={rho:.2f}) — +{bonus:.3f} boost"
                                ),
                                "confluence_source": {
                                    "source_pair": bridge_key,
                                    "rho": round(rho, 4),
                                    "bonus": round(bonus, 4),
                                    "other_conf": round(bridge_conf, 4),
                                    "window": self.CONFLUENCE_WINDOW,
                                },
                            }

        return overrides

    # ─── Confluence helpers (pure-Python, stdlib only) ─────────────────
    # Respects the "no numpy/pandas" engine invariant. All three methods
    # are static so they can be called before the coordinator has state.

    @staticmethod
    def _log_returns(prices: List[float]) -> List[float]:
        """Log-return series from a price list. Returns [] when input has
        fewer than 2 points or any non-positive price (log undefined)."""
        if len(prices) < 2:
            return []
        out: List[float] = []
        for i in range(1, len(prices)):
            prev = prices[i - 1]
            curr = prices[i]
            if prev <= 0.0 or curr <= 0.0:
                return []
            out.append(math.log(curr / prev))
        return out

    @staticmethod
    def pair_correlation(prices_a: List[float], prices_b: List[float],
                         window: int = 60) -> float:
        """Pearson correlation of log-returns over the last `window`
        aligned observations. Returns 0.0 when either series has fewer
        than `window + 1` points (insufficient data) or when either
        return series has zero variance (undefined correlation — treat
        as "no co-movement signal" rather than raising)."""
        ra = CrossPairCoordinator._log_returns(prices_a)
        rb = CrossPairCoordinator._log_returns(prices_b)
        if len(ra) < window or len(rb) < window:
            return 0.0
        ra = ra[-window:]
        rb = rb[-window:]
        n = len(ra)
        mean_a = sum(ra) / n
        mean_b = sum(rb) / n
        cov = 0.0
        var_a = 0.0
        var_b = 0.0
        for i in range(n):
            da = ra[i] - mean_a
            db = rb[i] - mean_b
            cov += da * db
            var_a += da * da
            var_b += db * db
        if var_a <= 0.0 or var_b <= 0.0:
            return 0.0
        return cov / math.sqrt(var_a * var_b)

    @staticmethod
    def confluence_bonus(rho: float, other_conf: float,
                         max_bonus: float = 0.10) -> float:
        """Deterministic confidence bonus from a confluence signal.

        Scales linearly with ρ (how strongly the pairs co-move) and with
        the other pair's confidence above 0.5 (the Kelly-edge threshold,
        below which the sizer already returns 0). Caps at `max_bonus`.
        Returns 0.0 when either input is non-positive — no bonus from a
        weak or negative confidence, no bonus from anti- or un-correlated
        pairs.
        """
        if rho <= 0.0 or other_conf <= 0.5:
            return 0.0
        raw = rho * (other_conf - 0.5) * 0.3
        if raw <= 0.0:
            return 0.0
        return min(raw, max_bonus)


# ═══════════════════════════════════════════════════════════════
# HYDRA ENGINE (Main orchestrator)
# ═══════════════════════════════════════════════════════════════

class HydraEngine:
    """
    Main engine. Ingest candles, get back regime/strategy/signal/trade decisions.

    Usage:
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        engine.ingest_candle({"open": 95000, "high": 95500, "low": 94500, "close": 95200, "volume": 150})
        state = engine.tick()
    """

    MAX_CANDLES = 250
    CIRCUIT_BREAKER_PCT = 15.0  # Stop if drawdown exceeds 15%
    # Friction expectancy gate (entries only; see _maybe_execute).
    # Round trip = 2 x 16 bps Kraken maker + ~10 bps spread/adverse buffer.
    ROUND_TRIP_FRICTION_PCT = 0.42
    FRICTION_HURDLE_MULT = 2.0

    def __init__(self, initial_balance: float = 10000.0, asset: str = "BTC/USD",
                 sizing: Optional[Dict[str, float]] = None,
                 candle_interval: int = 15,
                 volatile_atr_mult: float = 1.8,
                 volatile_bb_mult: float = 1.8,
                 trend_ema_ratio: float = 1.005,
                 momentum_rsi_lower: float = 30.0,
                 momentum_rsi_upper: float = 70.0,
                 mean_reversion_rsi_buy: float = 35.0,
                 mean_reversion_rsi_sell: float = 65.0,
                 tradable: bool = True):
        self.asset = asset
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.friction_skips = 0  # BUYs skipped by the friction expectancy gate
        # `tradable` gates the execution path. When False, _maybe_execute and
        # execute_signal short-circuit without producing a Trade, and the
        # drawdown-based circuit breaker is suppressed. Signal generation in
        # tick() continues so the engine can still contribute as a confluence
        # source for other pairs (see CrossPairCoordinator Rule 4). The agent
        # flips this flag per-tick based on real exchange holdings of the
        # quote currency — pairs whose quote we don't hold cannot transact.
        self.tradable = tradable

        self.position = Position(asset=asset)
        cfg = sizing or SIZING_CONSERVATIVE
        self.sizer = PositionSizer(**cfg)
        self.candle_interval = candle_interval
        self.volatile_atr_mult = volatile_atr_mult
        self.volatile_bb_mult = volatile_bb_mult
        self.trend_ema_ratio = trend_ema_ratio
        self.momentum_rsi_lower = momentum_rsi_lower
        self.momentum_rsi_upper = momentum_rsi_upper
        self.mean_reversion_rsi_buy = mean_reversion_rsi_buy
        self.mean_reversion_rsi_sell = mean_reversion_rsi_sell
        self.candles: List[Candle] = []
        self.prices: List[float] = []
        # v2.14: per-candle signed-volume proxy for CVD divergence detection.
        # Hydra does not subscribe to Kraken's trade-tape WebSocket, so we
        # cannot compute true CVD (buyer-initiated minus seller-initiated
        # volume from aggressor side). Instead we use the Chaikin Money
        # Flow multiplier: signed_volume = volume × ((close-low) - (high-close)) / (high-low).
        # This is the standard OHLC-based proxy — directionally correct and
        # adequate for divergence detection, though less precise than true CVD.
        self.signed_volumes: List[float] = []
        self.trades: List[Trade] = []
        self.equity_history: List[float] = []
        self.peak_equity = initial_balance
        self.max_drawdown = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.total_trades = 0
        self.tick_count = 0
        self.halted = False
        self.halt_reason = ""
        self.gross_profit = 0.0
        self.gross_loss = 0.0

    def ingest_candle(self, raw: Dict[str, Any]) -> None:
        """Add a candle from kraken ohlc JSON output. Deduplicates by timestamp."""
        has_timestamp = "timestamp" in raw
        try:
            candle = Candle(
                open=float(raw.get("open") or 0),
                high=float(raw.get("high") or 0),
                low=float(raw.get("low") or 0),
                close=float(raw.get("close") or 0),
                volume=float(raw.get("volume") or 0),
                timestamp=float(raw.get("timestamp") or time.time()),
            )
        except (TypeError, ValueError):
            return  # Malformed candle data — skip silently
        signed = _chaikin_signed_volume(candle)
        # Deduplicate: if Kraken timestamp matches last candle, update in place (incomplete candle refresh)
        if has_timestamp and self.candles and self.candles[-1].timestamp == candle.timestamp:
            self.candles[-1] = candle
            self.prices[-1] = candle.close
            if self.signed_volumes:
                self.signed_volumes[-1] = signed
            return
        self.candles.append(candle)
        self.prices.append(candle.close)
        self.signed_volumes.append(signed)
        # Keep memory bounded
        if len(self.candles) > self.MAX_CANDLES:
            self.candles = self.candles[-self.MAX_CANDLES:]
            self.prices = self.prices[-self.MAX_CANDLES:]
            self.signed_volumes = self.signed_volumes[-self.MAX_CANDLES:]

    def cvd_divergence_sigma(self) -> Optional[float]:
        """v2.14 Quant signal: z-score of (cvd_slope − price_slope) measured
        over the most recent 1h window (4 candles at 15-min) against its
        standard deviation over the last 24h (96 candles). Positive means
        CVD is outpacing price to the upside (accumulation); negative
        means CVD is leading price to the downside (distribution).
        Divergence > 2σ opposing the engine's signal is a material warning
        that smart money is leaning the other way.

        Returns None if there is insufficient history to compute (<~6h
        of candles at the current candle_interval). The Quant's R10 rule
        treats None as a staleness input, not a veto.
        """
        samples_1h = max(2, int(60 / max(1, self.candle_interval)))
        if len(self.signed_volumes) < samples_1h * 8:  # ~8 windows to estimate variance
            return None

        cvd_series = [sum(self.signed_volumes[: i + 1]) for i in range(len(self.signed_volumes))]
        window = samples_1h
        diffs: List[float] = []
        for end in range(window, len(cvd_series)):
            cvd_seg = cvd_series[end - window : end]
            px_seg = self.prices[end - window : end]
            cvd_slope = _linear_slope(cvd_seg)
            px_slope = _linear_slope(px_seg)
            if cvd_slope is None or px_slope is None:
                continue
            # Normalize each slope by its series mean magnitude so unit
            # difference between CVD (signed volume) and price is removed.
            cvd_norm = abs(sum(cvd_seg) / len(cvd_seg)) or 1.0
            px_norm = abs(sum(px_seg) / len(px_seg)) or 1.0
            diffs.append((cvd_slope / cvd_norm) - (px_slope / px_norm))

        # v2.14.1: require at least 8 diff windows (so history has >=7
        # samples for pstdev). Below that the z-score is unstable and
        # can swing to extreme values from a single volatile candle.
        if len(diffs) < 8:
            return None

        recent = diffs[-1]
        history = diffs[:-1]
        try:
            mu = statistics.mean(history)
            sd = statistics.pstdev(history)
        except statistics.StatisticsError:
            return None
        if sd <= 0:
            return 0.0
        return round((recent - mu) / sd, 3)

    def tick(self, generate_only: bool = False) -> Dict[str, Any]:
        """Run one decision cycle. Returns full state as dict.

        Args:
            generate_only: If True, generate signal but do NOT execute trades.
                           Use execute_signal() afterward to execute selectively.
                           This allows an external layer (e.g. AI brain) to review
                           the signal before committing to a trade.
        """
        self.tick_count += 1

        if self.halted:
            # PR-A: breaker stops new risk but must not freeze inventory.
            # With an open position, emit SELL (and execute unless generate_only)
            # so the agent brain path can also complete the flatten.
            flatten_trade = None
            if self.position.size > 0 and self.prices:
                flatten_sig = Signal(
                    action=SignalAction.SELL,
                    confidence=1.0,
                    reason=f"HALT FLATTEN: {self.halt_reason}",
                    strategy=Strategy.DEFENSIVE,
                )
                if not generate_only:
                    flatten_trade = self._maybe_execute(flatten_sig)
                # After execute, position may be flat → report HOLD; else SELL.
                if self.position.size > 0:
                    out_sig = flatten_sig
                else:
                    out_sig = Signal(
                        action=SignalAction.HOLD, confidence=0.0,
                        reason=f"HALTED (flat): {self.halt_reason}",
                        strategy=Strategy.DEFENSIVE,
                    )
                return self._build_state(
                    Regime.VOLATILE, Strategy.DEFENSIVE, out_sig, flatten_trade,
                )
            return self._build_state(
                Regime.VOLATILE,
                Strategy.DEFENSIVE,
                Signal(SignalAction.HOLD, 0.0, self.halt_reason, Strategy.DEFENSIVE),
            )

        # Detect regime
        regime = RegimeDetector.detect(
            self.candles, self.prices,
            self.volatile_atr_mult, self.volatile_bb_mult, self.trend_ema_ratio,
        )
        strategy = REGIME_STRATEGY_MAP[regime]

        # Generate signal
        signal = SignalGenerator.generate(
            strategy, self.prices, self.candles,
            momentum_rsi_lower=self.momentum_rsi_lower,
            momentum_rsi_upper=self.momentum_rsi_upper,
            mean_reversion_rsi_buy=self.mean_reversion_rsi_buy,
            mean_reversion_rsi_sell=self.mean_reversion_rsi_sell,
        )

        # Execute if actionable (skip when generate_only for external review)
        trade = None if generate_only else self._maybe_execute(signal)

        # Update portfolio metrics
        current_price = self.prices[-1] if self.prices else 0
        self.position.update_pnl(current_price)
        equity = self.balance + (self.position.size * current_price)
        self.equity_history.append(equity)

        # Track drawdown
        if equity > self.peak_equity:
            self.peak_equity = equity
        drawdown = ((self.peak_equity - equity) / self.peak_equity * 100) if self.peak_equity > 0 else 0
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        # Circuit breaker — suppressed for informational-only engines.
        # A non-tradable engine's equity curve is driven purely by price
        # movement on a phantom balance, not by real P&L, so halting it on
        # drawdown would be meaningless and would block it from re-activating
        # if the operator later deposits the quote currency.
        if self.tradable and self.max_drawdown > self.CIRCUIT_BREAKER_PCT:
            self.halted = True
            self.halt_reason = f"CIRCUIT BREAKER: drawdown {self.max_drawdown:.1f}% > {self.CIRCUIT_BREAKER_PCT}% limit"

        return self._build_state(regime, strategy, signal, trade)

    @staticmethod
    def _apply_size_multiplier(raw: float) -> float:
        """Apply brain size multiplier: raw below 1.0, exponential above, cap 2.0."""
        if raw <= 1.0:
            return raw
        return min(2.0, 4.0 ** (raw - 1.0))

    def _expected_move_pct(self, signal: Signal, current_price: float) -> Optional[float]:
        """Strategy-implied expected gross move for a BUY, in percent.

        Mean-reversion family (MEAN_REVERSION, GRID) targets the Bollinger
        middle band; trend family (MOMENTUM, DEFENSIVE) uses 2x ATR%% as the
        continuation proxy. Returns None when indicators are insufficient —
        the friction gate fails OPEN on missing data (a data gap must not
        silently suppress trading; staleness is R10's job, not this gate's).
        """
        ind = signal.indicators or {}
        price = ind.get("price") or current_price
        if not price or price <= 0:
            return None
        if signal.strategy in (Strategy.MEAN_REVERSION, Strategy.GRID):
            mid = ind.get("bb_middle")
            if (not mid or mid <= 0) and len(self.prices) >= 20:
                # execute_signal() builds Signals without indicators (the
                # brain/coordinator path) — recompute from engine history so
                # the gate is live on EVERY execution path, not just tick().
                mid = Indicators.bollinger_bands(list(self.prices))["middle"]
            if mid and mid > 0:
                return abs(mid - price) / price * 100.0
            # MR/GRID targets the BB middle — do not fall through to ATR
            # (wrong proxy). Fail open when mid is unavailable.
            return None
        atr_pct = ind.get("atr_pct")
        if (not atr_pct or atr_pct <= 0) and len(self.candles) >= 15:
            atr = Indicators.atr(list(self.candles))
            atr_pct = (atr / price * 100.0) if atr > 0 else None
        if atr_pct and atr_pct > 0:
            return 2.0 * atr_pct
        return None

    def _maybe_execute(self, signal: Signal, size_multiplier: float = 1.0) -> Optional[Trade]:
        """Execute trade if signal is actionable.

        Halt policy (PR-A / exit guarantees):
          * BUY is refused while ``halted`` — circuit breaker stops new risk.
          * SELL is **allowed** while halted when ``position.size > 0`` so the
            breaker cannot trap inventory through further mark-to-market loss.
          * Entries still require ``min_confidence``; exits do **not** (A2) —
            Kelly sizes entries, full-close on any SELL once a position exists
            and is above ordermin.

        Informational-only engines (tradable=False) never produce a Trade
        — the agent-level guard (real quote-currency balance) flipped the
        flag because we don't hold the currency needed to fund this pair's
        orders. The signal still exists in state for confluence consumers.
        """
        if not self.tradable:
            return None
        if not self.prices:
            return None

        # Halt blocks new risk only. Risk-reducing SELLs must still run.
        if self.halted and signal.action != SignalAction.SELL:
            return None
        if self.halted and signal.action == SignalAction.SELL and self.position.size <= 0:
            return None

        current_price = self.prices[-1]
        effective_mult = self._apply_size_multiplier(size_multiplier)

        # Friction expectancy gate (v2.27, entries only): a BUY whose
        # strategy-implied expected move cannot clear a multiple of the
        # round-trip friction (fees + spread) has negative expectancy even
        # when the signal is "right". SKIP semantics — exits are never
        # gated (friction on an open position is sunk; blocking the SELL
        # would trap it). Kill switch: HYDRA_FRICTION_GATE_DISABLED=1.
        if (signal.action == SignalAction.BUY
                and os.environ.get("HYDRA_FRICTION_GATE_DISABLED") != "1"):
            expected = self._expected_move_pct(signal, current_price)
            # PR-D / D2: timeframe-aware hurdle. On 1h+ candles the BB-mid /
            # 2×ATR proxies almost always clear 0.84% (audit: 0/437 blocks
            # on SOL 1y), so the gate was inert. Raise the floor for longer
            # bars so only trades with material expected move clear.
            hurdle = self.FRICTION_HURDLE_MULT * self.ROUND_TRIP_FRICTION_PCT
            if getattr(self, "candle_interval", 15) >= 60:
                hurdle = max(hurdle, 2.0)  # percent
            elif getattr(self, "candle_interval", 15) >= 30:
                hurdle = max(hurdle, 1.2)
            if expected is not None and expected < hurdle:
                self.friction_skips += 1
                return None

        if signal.action == SignalAction.BUY and signal.confidence >= self.sizer.min_confidence:
            size = self.sizer.calculate(signal.confidence, self.balance, current_price, self.asset)
            size = size * effective_mult
            # PR-B: hard risk caps AFTER size_multiplier (B1) and against
            # gross inventory (B2). Advertised max_position_pct is the
            # ceiling on total position notional / equity — not a pre-mult
            # Kelly-only clamp that mult could defeat up to ~80% cash.
            if current_price > 0:
                equity = self.balance + self.position.size * current_price
                max_notional = equity * self.sizer.max_position_pct
                current_notional = self.position.size * current_price
                room_units = max(0.0, max_notional - current_notional) / current_price
                max_cash_units = self.balance / current_price
                size = min(size, room_units, max_cash_units)
            if size > 0:
                cost = size * current_price
                # Update position (average in)
                if self.position.size > 0:
                    total_size = self.position.size + size
                    self.position.avg_entry = (
                        self.position.avg_entry * self.position.size + current_price * size
                    ) / total_size
                    self.position.size = total_size
                    # Update params to latest on average-in so tuner sees current params
                    self.position.params_at_entry = self.snapshot_params()
                else:
                    self.position.size = size
                    self.position.avg_entry = current_price
                    self.position.params_at_entry = self.snapshot_params()

                self.balance -= cost

                trade = Trade(
                    action="BUY",
                    asset=self.asset,
                    price=current_price,
                    amount=size,
                    value=cost,
                    reason=signal.reason,
                    confidence=signal.confidence,
                    strategy=signal.strategy.value,
                )
                self.trades.append(trade)
                return trade

        elif signal.action == SignalAction.SELL and self.position.size > 0:
            # PR-A / A2: exits ignore min_confidence. Kelly sizes entries only.
            # Soft DEFENSIVE/GRID SELL signals (conf 0.50–0.64) must still
            # flatten inventory — reusing the entry floor trapped longs in
            # TREND_DOWN (audit: 200+ dead SELLs / SOL-year).
            # Full-close only (Fix 6): spot-only half-exit does not reduce risk
            # proportionally; it delays the exit to a worse price.
            base_asset = self.asset.split("/")[0] if "/" in self.asset else self.asset
            min_size = self.sizer.MIN_ORDER_SIZE.get(base_asset, 0.02)
            if self.position.size < min_size:
                # PR-C / C4: write off unsellable dust instead of leaving a
                # permanent [0, ordermin) bag that blocks state forever.
                written = self.write_off_dust(reason="unsellable_below_ordermin")
                if written > 0:
                    return None  # dust cleared; no exchange sell
                return None
            sell_amount = self.position.size  # Full close
            revenue = sell_amount * current_price
            profit = (current_price - self.position.avg_entry) * sell_amount
            # Capture params before position state is cleared
            entry_params = self.position.params_at_entry

            self.balance += revenue
            self.position.size -= sell_amount
            self.position.realized_pnl += profit
            total_profit = profit  # default: single-leg profit
            position_closed = False
            dust_threshold = min_size * 0.1 if min_size > 0 else 0.00001
            if self.position.size < dust_threshold:
                self.position.size = 0.0
                self.position.avg_entry = 0.0
                position_closed = True
                # Only count as a completed trade when position is fully closed.
                # Use accumulated realized PnL so partial sells at different
                # confidence levels are tallied correctly (previously only the
                # final leg's profit was used to decide win vs loss).
                total_profit = self.position.realized_pnl
                self.total_trades += 1
                if total_profit > 0:
                    self.win_count += 1
                    self.gross_profit += total_profit
                else:
                    # Break-even (== 0) counts as loss per industry standard:
                    # zero gain after fees and opportunity cost is not a win.
                    self.loss_count += 1
                    self.gross_loss += abs(total_profit)
                self.position.params_at_entry = None
                self.position.realized_pnl = 0.0

            trade = Trade(
                action="SELL",
                asset=self.asset,
                price=current_price,
                amount=sell_amount,
                value=revenue,
                reason=signal.reason,
                confidence=signal.confidence,
                strategy=signal.strategy.value,
                # On full close, report total accumulated P&L; on partial, just this leg
                profit=total_profit if position_closed else profit,
                # Preserve entry params for tuner — cleared from position on close
                params_at_entry=entry_params if position_closed else None,
            )
            self.trades.append(trade)
            return trade

        return None

    def execute_signal(self, action: str, confidence: float, reason: str = "",
                        strategy: str = "MOMENTUM",
                        size_multiplier: float = 1.0) -> Optional[Trade]:
        """Execute a trade based on an externally-provided signal.

        Use after tick(generate_only=True) to execute with a (possibly modified)
        signal from an AI brain or cross-pair coordinator.

        Args:
            action: "BUY", "SELL", or "HOLD"
            confidence: Signal confidence 0-1
            reason: Human-readable reason string
            strategy: Strategy name for logging
            size_multiplier: Brain-derived sizing multiplier (default 1.0).
                Raw pass-through below 1.0; exponential above 1.0 (cap 2.0).

        Returns:
            Trade if executed, None otherwise
        """
        try:
            sig_action = SignalAction(action)
        except ValueError:
            return None
        try:
            sig_strategy = Strategy(strategy)
        except ValueError:
            sig_strategy = Strategy.MOMENTUM

        signal = Signal(
            action=sig_action,
            confidence=confidence,
            reason=reason,
            strategy=sig_strategy,
        )
        return self._maybe_execute(signal, size_multiplier=size_multiplier)

    def snapshot_params(self) -> Dict[str, float]:
        """Return a snapshot of the current tunable parameters."""
        return {
            "volatile_atr_mult": self.volatile_atr_mult,
            "volatile_bb_mult": self.volatile_bb_mult,
            "trend_ema_ratio": self.trend_ema_ratio,
            "momentum_rsi_lower": self.momentum_rsi_lower,
            "momentum_rsi_upper": self.momentum_rsi_upper,
            "mean_reversion_rsi_buy": self.mean_reversion_rsi_buy,
            "mean_reversion_rsi_sell": self.mean_reversion_rsi_sell,
            "min_confidence_threshold": self.sizer.min_confidence,
        }

    def apply_tuned_params(self, params: Dict[str, float]):
        """Apply tuned parameters from ParameterTracker.

        Defense-in-depth: every value is clamped to ``PARAM_BOUNDS`` before
        it touches engine state. The tuner already clamps on its side, but
        this method is also fed by the per-pair ``hydra_params_<pair>.json``
        file at startup (``hydra_agent`` load path) and by backtest/shadow
        overrides — any of which could carry an out-of-range or corrupted
        value. Unknown keys are ignored (a contract relied on by
        ``hydra_backtest_server``). A degenerate RSI band (lower >= upper)
        would silently suppress all momentum/mean-reversion signals, so the
        coupling is enforced after clamping and rejected (left unchanged)
        rather than applied.
        """
        # Deferred import: tuner does not import engine, so this is safe and
        # keeps the engine free of a module-level dependency on the tuner.
        from hydra_tuner import PARAM_BOUNDS

        def _clamp(key: str) -> Optional[float]:
            if key not in params:
                return None
            try:
                val = float(params[key])
            except (TypeError, ValueError):
                return None
            lo, hi = PARAM_BOUNDS[key]
            return max(lo, min(hi, val))

        atr_mult = _clamp("volatile_atr_mult")
        if atr_mult is not None:
            self.volatile_atr_mult = atr_mult
        bb_mult = _clamp("volatile_bb_mult")
        if bb_mult is not None:
            self.volatile_bb_mult = bb_mult
        ema_ratio = _clamp("trend_ema_ratio")
        if ema_ratio is not None:
            self.trend_ema_ratio = ema_ratio

        # Momentum RSI band — apply only if the (clamped) pair is coherent.
        mom_lo = _clamp("momentum_rsi_lower")
        mom_hi = _clamp("momentum_rsi_upper")
        eff_lo = mom_lo if mom_lo is not None else self.momentum_rsi_lower
        eff_hi = mom_hi if mom_hi is not None else self.momentum_rsi_upper
        if eff_lo < eff_hi:
            if mom_lo is not None:
                self.momentum_rsi_lower = mom_lo
            if mom_hi is not None:
                self.momentum_rsi_upper = mom_hi
        elif mom_lo is not None or mom_hi is not None:
            print(f"  [TUNE] rejected momentum RSI band lower={eff_lo} >= "
                  f"upper={eff_hi} — keeping existing "
                  f"({self.momentum_rsi_lower}/{self.momentum_rsi_upper})")

        # Mean-reversion RSI band — same coherence guard.
        mr_buy = _clamp("mean_reversion_rsi_buy")
        mr_sell = _clamp("mean_reversion_rsi_sell")
        eff_buy = mr_buy if mr_buy is not None else self.mean_reversion_rsi_buy
        eff_sell = mr_sell if mr_sell is not None else self.mean_reversion_rsi_sell
        if eff_buy < eff_sell:
            if mr_buy is not None:
                self.mean_reversion_rsi_buy = mr_buy
            if mr_sell is not None:
                self.mean_reversion_rsi_sell = mr_sell
        elif mr_buy is not None or mr_sell is not None:
            print(f"  [TUNE] rejected mean-reversion RSI band buy={eff_buy} >= "
                  f"sell={eff_sell} — keeping existing "
                  f"({self.mean_reversion_rsi_buy}/{self.mean_reversion_rsi_sell})")

        min_conf = _clamp("min_confidence_threshold")
        if min_conf is not None:
            self.sizer.min_confidence = min_conf

    def snapshot_position(self) -> Dict[str, Any]:
        """Snapshot position/balance state for rollback on failed exchange orders."""
        return {
            "balance": self.balance,
            "position_size": self.position.size,
            "position_avg_entry": self.position.avg_entry,
            "position_realized_pnl": self.position.realized_pnl,
            "position_params_at_entry": self.position.params_at_entry,
            "total_trades": self.total_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "trades_len": len(self.trades),
            # tick() updates these before we know if the exchange accepted the order.
            # Capture them so the non-brain path can restore on failed orders.
            "equity_history_len": len(self.equity_history),
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            # _maybe_execute updates these on position-closing SELLs before
            # the exchange confirms — must rollback on rejection.
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            # tradable flag — informational-only engines cannot place orders.
            # Persisted so --resume doesn't silently re-enable a pair whose
            # quote currency the user no longer holds.
            "tradable": self.tradable,
        }

    def restore_position(self, snap: Dict[str, Any]) -> None:
        """Restore position/balance state from snapshot (rollback failed trade)."""
        self.balance = snap["balance"]
        self.position.size = snap["position_size"]
        self.position.avg_entry = snap["position_avg_entry"]
        self.position.realized_pnl = snap["position_realized_pnl"]
        self.position.params_at_entry = snap["position_params_at_entry"]
        self.total_trades = snap["total_trades"]
        self.win_count = snap["win_count"]
        self.loss_count = snap["loss_count"]
        self.trades = self.trades[:snap["trades_len"]]
        # Restore analytics state that tick() may have updated before rollback
        self.equity_history = self.equity_history[:snap["equity_history_len"]]
        self.peak_equity = snap["peak_equity"]
        self.max_drawdown = snap["max_drawdown"]
        self.gross_profit = snap["gross_profit"]
        self.gross_loss = snap["gross_loss"]
        # Backward-compat: snapshots written before v2.11.0 have no
        # "tradable" field; default to True so resumed sessions behave
        # identically to pre-flag behavior until the agent refreshes.
        self.tradable = snap.get("tradable", True)

    def true_up_fill(
        self,
        side: str,
        amount: float,
        fill_price: float,
        pre_trade_snapshot: Optional[Dict[str, Any]] = None,
        reason: str = "fill_true_up",
        strategy: str = "MOMENTUM",
        confidence: float = 0.0,
    ) -> bool:
        """PR-C / C1: rewrite engine books to exchange fill price/amount.

        Restores ``pre_trade_snapshot`` then applies the fill at
        ``fill_price``. Used for both full FILLED and partial events so
        avg_entry / balance match Kraken truth (not candle close).
        Returns True if true-up applied, False if skipped (no snapshot).
        """
        if amount <= 0 or fill_price <= 0:
            return False
        if pre_trade_snapshot is None:
            return False
        self.restore_position(pre_trade_snapshot)
        try:
            sig_strategy = Strategy(strategy)
        except ValueError:
            sig_strategy = Strategy.MOMENTUM
        if side.upper() == "BUY":
            self._apply_buy_fill(amount, fill_price, reason, sig_strategy, confidence)
        elif side.upper() == "SELL":
            self._apply_sell_fill(amount, fill_price, reason, sig_strategy, confidence)
        else:
            return False
        return True

    def write_off_dust(self, reason: str = "dust_write_off") -> float:
        """PR-C / C4: zero position residue in [0, ordermin) that cannot sell.

        Returns the written-off size (0 if nothing done).
        """
        base_asset = self.asset.split("/")[0] if "/" in self.asset else self.asset
        min_size = self.sizer.MIN_ORDER_SIZE.get(base_asset, 0.02)
        size = self.position.size
        if size <= 0:
            return 0.0
        if size >= min_size:
            return 0.0
        # Anything below ordermin is unsellable on Kraken — clear books.
        written = size
        self.position.size = 0.0
        self.position.avg_entry = 0.0
        self.position.params_at_entry = None
        self.position.unrealized_pnl = 0.0
        return written

    def reconcile_partial_fill(
        self,
        side: str,
        placed_amount: float,
        vol_exec: float,
        limit_price: float,
        pre_trade_snapshot: Optional[Dict[str, Any]] = None,
        reason: str = "partial_fill_reconcile",
        strategy: str = "MOMENTUM",
        confidence: float = 0.0,
    ) -> None:
        """Correct engine state after a PARTIALLY_FILLED (or full true-up) event.

        At execute_signal time, the engine optimistically committed the full
        `placed_amount` to position/balance. The exchange actually filled only
        `vol_exec` (or full at a different price). Preferred path: restore
        snapshot + replay filled amount at fill price (PR-C true-up).

        When `pre_trade_snapshot` is None (resume-path: previous session's
        snapshot wasn't persisted), we fall back to arithmetic reversal and
        accept avg_entry drift.

        Args:
            side: "BUY" or "SELL"
            placed_amount: What execute_signal was told to commit
            vol_exec: What actually filled on the exchange
            limit_price: The fill / limit price to book
            pre_trade_snapshot: Snapshot from snapshot_position() taken before
                execute_signal was called. Preferred path.
            reason / strategy / confidence: carried into the replayed Trade
                for audit trail.
        """
        if placed_amount <= 0:
            return
        if vol_exec < 0:
            vol_exec = 0.0

        # PR-C: always prefer restore+replay when snapshot exists — even on
        # full fills — so avg_entry tracks exchange fill price not candle close.
        if pre_trade_snapshot is not None:
            if vol_exec <= 0:
                self.restore_position(pre_trade_snapshot)
                return
            self.true_up_fill(
                side=side,
                amount=float(vol_exec),
                fill_price=float(limit_price),
                pre_trade_snapshot=pre_trade_snapshot,
                reason=reason,
                strategy=strategy,
                confidence=confidence,
            )
            return

        # Full fill without snapshot — nothing arithmetic to reverse
        if vol_exec >= placed_amount * 0.999999:  # float-safe
            return

        # Fallback: no snapshot available (resume-path). Arithmetic reversal

        # Fallback: no snapshot available (resume-path). Arithmetic reversal
        # of the unfilled delta. Cannot recover exact avg_entry weighting if
        # the original trade was an average-in — we accept that drift and log.
        unfilled = placed_amount - vol_exec
        if side.upper() == "BUY":
            # BUY over-committed: refund unfilled quote, remove unfilled base
            self.balance += unfilled * limit_price
            self.position.size = max(0.0, self.position.size - unfilled)
            if self.position.size == 0.0:
                self.position.avg_entry = 0.0
        elif side.upper() == "SELL":
            # SELL over-committed: remove unfilled quote we optimistically
            # took in, add unfilled base back to position
            self.balance -= unfilled * limit_price
            self.position.size += unfilled

    def _apply_buy_fill(
        self, amount: float, price: float, reason: str,
        strategy: Strategy, confidence: float,
    ) -> None:
        """Mirror of the BUY state-mutation in _maybe_execute, but with an
        exogenous `amount` (no sizer). Used only by reconcile_partial_fill
        after restore_position. Mutates position / balance / trades."""
        cost = amount * price
        if self.position.size > 0:
            total_size = self.position.size + amount
            self.position.avg_entry = (
                self.position.avg_entry * self.position.size + price * amount
            ) / total_size
            self.position.size = total_size
            self.position.params_at_entry = self.snapshot_params()
        else:
            self.position.size = amount
            self.position.avg_entry = price
            self.position.params_at_entry = self.snapshot_params()
        self.balance -= cost
        self.trades.append(Trade(
            action="BUY", asset=self.asset, price=price, amount=amount,
            value=cost, reason=reason, confidence=confidence,
            strategy=strategy.value,
        ))

    def _apply_sell_fill(
        self, amount: float, price: float, reason: str,
        strategy: Strategy, confidence: float,
    ) -> None:
        """Mirror of the SELL state-mutation in _maybe_execute, but with an
        exogenous `amount`. Bypasses ordermin / dust checks (the exchange
        already accepted and filled this amount)."""
        if amount <= 0 or self.position.size <= 0:
            return
        amount = min(amount, self.position.size)  # never oversell
        revenue = amount * price
        profit = (price - self.position.avg_entry) * amount
        entry_params = self.position.params_at_entry
        base_asset = self.asset.split("/")[0] if "/" in self.asset else self.asset
        min_size = self.sizer.MIN_ORDER_SIZE.get(base_asset, 0.02)
        self.balance += revenue
        self.position.size -= amount
        self.position.realized_pnl += profit
        total_profit = profit
        position_closed = False
        dust_threshold = min_size * 0.1 if min_size > 0 else 0.00001
        if self.position.size < dust_threshold:
            self.position.size = 0.0
            self.position.avg_entry = 0.0
            position_closed = True
            total_profit = self.position.realized_pnl
            self.total_trades += 1
            if total_profit > 0:
                self.win_count += 1
                self.gross_profit += total_profit
            else:
                self.loss_count += 1
                self.gross_loss += abs(total_profit)
            self.position.params_at_entry = None
            self.position.realized_pnl = 0.0
        self.trades.append(Trade(
            action="SELL", asset=self.asset, price=price, amount=amount,
            value=revenue, reason=reason, confidence=confidence,
            strategy=strategy.value,
            profit=total_profit if position_closed else profit,
            params_at_entry=entry_params if position_closed else None,
        ))

    def snapshot_runtime(self) -> Dict[str, Any]:
        """Serialize full engine runtime state for session persistence.

        HF-004 fix: trades list is now included. Prior versions omitted it,
        causing trades_list_len=0 while total_trades>0 on every --resume,
        breaking per-pair P&L and tuner analytics that iterate over trades.
        """
        return {
            "asset": self.asset,
            "initial_balance": self.initial_balance,
            "balance": self.balance,
            "position": {
                "asset": self.position.asset,
                "size": self.position.size,
                "avg_entry": self.position.avg_entry,
                "unrealized_pnl": self.position.unrealized_pnl,
                "params_at_entry": self.position.params_at_entry,
                "realized_pnl": self.position.realized_pnl,
            },
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_trades": self.total_trades,
            "tick_count": self.tick_count,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "equity_history": self.equity_history[-500:],
            "trades": [
                {
                    "action": t.action,
                    "asset": t.asset,
                    "price": t.price,
                    "amount": t.amount,
                    "value": t.value,
                    "reason": t.reason,
                    "confidence": t.confidence,
                    "strategy": t.strategy,
                    "timestamp": t.timestamp,
                    "profit": t.profit,
                    "params_at_entry": t.params_at_entry,
                }
                for t in self.trades[-500:]  # Bounded to prevent unbounded snapshot growth
            ],
            "candles": [
                {"open": c.open, "high": c.high, "low": c.low,
                 "close": c.close, "volume": c.volume, "timestamp": c.timestamp}
                for c in self.candles[-self.MAX_CANDLES:]
            ],
        }

    def restore_runtime(self, snapshot: Dict[str, Any]):
        """Restore engine runtime state from a snapshot produced by snapshot_runtime."""
        if not snapshot:
            return
        self.initial_balance = float(snapshot.get("initial_balance", self.initial_balance))
        self.balance = float(snapshot.get("balance", self.balance))
        p = snapshot.get("position", {})
        self.position = Position(
            asset=p.get("asset", self.asset),
            size=float(p.get("size", 0.0)),
            avg_entry=float(p.get("avg_entry", 0.0)),
            unrealized_pnl=float(p.get("unrealized_pnl", 0.0)),
            params_at_entry=p.get("params_at_entry"),
            realized_pnl=float(p.get("realized_pnl", 0.0)),
        )
        self.peak_equity = float(snapshot.get("peak_equity", self.initial_balance))
        self.max_drawdown = float(snapshot.get("max_drawdown", 0.0))
        self.win_count = int(snapshot.get("win_count", 0))
        self.loss_count = int(snapshot.get("loss_count", 0))
        self.total_trades = int(snapshot.get("total_trades", 0))
        self.tick_count = int(snapshot.get("tick_count", 0))
        self.halted = bool(snapshot.get("halted", False))
        self.halt_reason = str(snapshot.get("halt_reason", ""))
        self.gross_profit = float(snapshot.get("gross_profit", 0.0))
        self.gross_loss = float(snapshot.get("gross_loss", 0.0))
        self.equity_history = list(snapshot.get("equity_history", []))
        # HF-004 fix: restore trades list. Defensive: tolerate legacy snapshots
        # (no trades key), malformed rows, and missing optional fields.
        self.trades = []
        dropped_trades = 0
        for raw in snapshot.get("trades", []):
            if not isinstance(raw, dict):
                dropped_trades += 1
                continue
            try:
                self.trades.append(Trade(
                    action=str(raw.get("action", "")),
                    asset=str(raw.get("asset", self.asset)),
                    price=float(raw.get("price", 0.0)),
                    amount=float(raw.get("amount", 0.0)),
                    value=float(raw.get("value", 0.0)),
                    reason=str(raw.get("reason", "")),
                    confidence=float(raw.get("confidence", 0.0)),
                    strategy=str(raw.get("strategy", "MOMENTUM")),
                    timestamp=float(raw.get("timestamp", time.time())),
                    profit=raw.get("profit"),
                    params_at_entry=raw.get("params_at_entry"),
                ))
            except (TypeError, ValueError, KeyError):
                dropped_trades += 1
                continue  # drop malformed rows; don't crash tick loop
        if dropped_trades:
            print(
                f"  [restore_runtime] {self.asset}: dropped "
                f"{dropped_trades} malformed trade row(s) from snapshot",
                file=sys.stderr,
            )
        self.candles = []
        self.prices = []
        self.signed_volumes = []
        dropped_candles = 0
        for raw in snapshot.get("candles", []):
            if not isinstance(raw, dict):
                dropped_candles += 1
                continue
            # Skip candles without a timestamp rather than fabricating
            # time.time() — injecting "now" on restore corrupts the time
            # ordering the Sharpe calculation and ATR-series rely on.
            ts_raw = raw.get("timestamp")
            if ts_raw is None:
                dropped_candles += 1
                continue
            try:
                c = Candle(
                    open=float(raw.get("open", 0)), high=float(raw.get("high", 0)),
                    low=float(raw.get("low", 0)), close=float(raw.get("close", 0)),
                    volume=float(raw.get("volume", 0.0)),
                    timestamp=float(ts_raw),
                )
                self.candles.append(c)
                self.prices.append(c.close)
                # v2.14: rebuild signed-volume series on restore so CVD
                # divergence is available immediately after --resume,
                # not after another candle_interval × 8 of warmup.
                self.signed_volumes.append(_chaikin_signed_volume(c))
            except (TypeError, ValueError, KeyError):
                dropped_candles += 1
                continue  # drop malformed candle rows
        if dropped_candles:
            print(
                f"  [restore_runtime] {self.asset}: dropped "
                f"{dropped_candles} malformed candle row(s) from snapshot",
                file=sys.stderr,
            )

    def _candle_status(self) -> str:
        """Check if the latest candle is still forming or closed."""
        if not self.candles:
            return "unknown"
        age = time.time() - self.candles[-1].timestamp
        if age < self.candle_interval * 60:
            return "forming"
        return "closed"

    def _build_state(
        self,
        regime: Regime,
        strategy: Strategy,
        signal: Signal,
        trade: Optional[Trade] = None,
    ) -> Dict[str, Any]:
        """Build complete state dictionary for reporting."""
        current_price = self.prices[-1] if self.prices else 0
        equity = self.balance + (self.position.size * current_price)
        # Stable-quoted pairs (USD, USDC, USDT) report dollar values to 2 decimals;
        # crypto-quoted pairs need full 8.
        quote = self.asset.split("/")[1].upper() if "/" in self.asset else ""
        is_usd_pair = quote in STABLE_QUOTES
        value_decimals = 2 if is_usd_pair else 8
        pnl_pct = ((equity - self.initial_balance) / self.initial_balance * 100) if self.initial_balance > 0 else 0
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        # Sharpe estimate from equity curve
        sharpe = self._calc_sharpe()

        # Trend & volatility (same indicators RegimeDetector uses, surfaced for AI agents)
        atr_val = Indicators.atr(self.candles) if len(self.candles) > 14 else 0.0
        ema20 = Indicators.ema(self.prices, 20) if len(self.prices) >= 20 else current_price
        ema50 = Indicators.ema(self.prices, 50) if len(self.prices) >= 50 else current_price
        atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

        # Volume stats
        vol_current = self.candles[-1].volume if self.candles else 0.0
        vol_window = self.candles[-20:] if self.candles else []
        vol_avg = (sum(c.volume for c in vol_window) / len(vol_window)) if vol_window else 0.0

        state = {
            "tick": self.tick_count,
            "timestamp": time.time(),
            "asset": self.asset,
            "price": round(current_price, 8),
            "regime": regime.value,
            "strategy": strategy.value,
            "signal": {
                "action": signal.action.value,
                "confidence": round(signal.confidence, 4),
                "reason": signal.reason,
            },
            "position": {
                "size": round(self.position.size, 8),
                "avg_entry": round(self.position.avg_entry, 8),
                "unrealized_pnl": round(self.position.unrealized_pnl, value_decimals),
            },
            "portfolio": {
                "balance": round(self.balance, value_decimals),
                "equity": round(equity, value_decimals),
                "pnl_pct": round(pnl_pct, 4),
                "max_drawdown_pct": round(self.max_drawdown, 4),
                "peak_equity": round(self.peak_equity, value_decimals),
            },
            "performance": {
                "total_trades": self.total_trades,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate_pct": round(win_rate, 2),
                "sharpe_estimate": round(sharpe, 4),
            },
            "trend": {
                "ema20": round(ema20, 8),
                "ema50": round(ema50, 8),
            },
            "volatility": {
                "atr": round(atr_val, 8),
                "atr_pct": round(atr_pct, 4),
            },
            "volume": {
                "current": round(vol_current, 4),
                "avg_20": round(vol_avg, 4),
            },
            "candle_interval": self.candle_interval,
            "candle_status": self._candle_status(),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "indicators": signal.indicators if signal.indicators else {},
            "candles": [
                {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "t": c.timestamp}
                for c in self.candles[-100:]
            ],
        }

        if trade:
            # Prices and amounts always use full precision (8 decimals) — critical
            # for BTC-denominated pairs like SOL/BTC where price ≈ 0.0015.
            # Dollar values (value, profit) use 2 decimals for stable-quoted pairs,
            # 8 for crypto-denominated pairs.
            quote = self.asset.split("/")[1].upper() if "/" in self.asset else ""
            is_usd_pair = quote in STABLE_QUOTES
            value_decimals = 2 if is_usd_pair else 8
            state["last_trade"] = {
                "action": trade.action,
                "price": round(trade.price, 8),
                "amount": round(trade.amount, 8),
                "value": round(trade.value, value_decimals),
                "reason": trade.reason,
                "confidence": round(trade.confidence, 4),
                "profit": round(trade.profit, value_decimals) if trade.profit is not None else None,
                "params_at_entry": trade.params_at_entry,
            }

        return state

    def _calc_sharpe(self) -> float:
        """Estimate Sharpe ratio from equity history.

        Annualisation is derived from observed candle timestamp deltas (median),
        not the nominal candle_interval, so mismatches between configuration
        and exchange cadence do not skew the result.
        """
        if len(self.equity_history) < 30:
            return 0.0
        recent = self.equity_history[-60:]
        returns = [
            (recent[i] - recent[i - 1]) / recent[i - 1]
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        avg = sum(returns) / len(returns)
        var = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
        if var <= 0:
            return 0.0
        std = math.sqrt(var)
        # Observed period length — median of candle timestamp deltas.
        # Falls back to nominal candle_interval when observed cadence is
        # synthetic (sub-second) or unavailable (no candles).
        period_seconds = 0.0
        if len(self.candles) >= 3:
            deltas = sorted(
                self.candles[i].timestamp - self.candles[i - 1].timestamp
                for i in range(1, len(self.candles))
                if self.candles[i].timestamp > self.candles[i - 1].timestamp
            )
            if deltas:
                period_seconds = deltas[len(deltas) // 2]
        if period_seconds < 1.0:
            period_seconds = float(self.candle_interval) * 60.0
        periods_per_year = (365.25 * 24.0 * 3600.0) / period_seconds
        return (avg / std) * math.sqrt(periods_per_year)

    def get_performance_report(self) -> str:
        """Generate a formatted performance report."""
        if not self.prices:
            return "No data yet."

        current_price = self.prices[-1]
        equity = self.balance + self.position.size * current_price
        pnl = equity - self.initial_balance
        pnl_pct = (pnl / self.initial_balance) * 100
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        profit_factor = self.gross_profit / self.gross_loss if self.gross_loss > 0 else float("inf")

        w = 60  # inner width between ║ chars
        def row(label, value):
            content = f"  {label:<18}{value}"
            return f"  {content:<{w}}"
        def sep():
            return "  " + "-" * w

        status = f"HALTED -- {self.halt_reason[:40]}" if self.halted else "ACTIVE"
        base = self.asset.split("/")[0]
        quote = self.asset.split("/")[1] if "/" in self.asset else "USD"
        is_usd = quote in STABLE_QUOTES
        cur = "$" if is_usd else ""
        vd = 2 if is_usd else 8  # value decimals

        lines = [
            "",
            "  " + "=" * w,
            f"  {'HYDRA PERFORMANCE REPORT':^{w}}",
            "  " + "=" * w,
            row("Asset", self.asset),
            row("Duration", f"{self.tick_count} ticks"),
            row("Initial Balance", f"{cur}{self.initial_balance:,.{vd}f}" + ("" if is_usd else f" {quote}")),
            row("Final Balance", f"{cur}{equity:,.{vd}f}" + ("" if is_usd else f" {quote}")),
            sep(),
            row("Net P&L", f"{cur}{pnl:+,.{vd}f}  ({pnl_pct:+.2f}%)"),
            row("Max Drawdown", f"{self.max_drawdown:.2f}%"),
            row("Sharpe Ratio", f"{self._calc_sharpe():.4f}"),
            row("Profit Factor", f"{profit_factor:.2f}"),
            sep(),
            row("Total Trades", str(self.total_trades)),
            row("Wins", str(self.win_count)),
            row("Losses", str(self.loss_count)),
            row("Win Rate", f"{win_rate:.1f}%"),
            sep(),
            row("Open Position", f"{self.position.size:.6f} {base}"),
            row("Avg Entry", f"{cur}" + _fmt_price(self.position.avg_entry)),
            row("Unrealized P&L", f"{cur}{self.position.unrealized_pnl:+,.{vd}f}"),
            row("Cash Balance", f"{cur}{self.balance:,.{vd}f}"),
            sep(),
            row("Status", status),
            "  " + "=" * w,
            "",
        ]
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Demo: run with synthetic data
    import random

    engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
    price = 95000.0

    print("HYDRA Engine — Synthetic Demo")
    print("=" * 60)

    for i in range(300):
        # Random walk with slight upward drift
        price *= 1 + random.gauss(0.0001, 0.003)
        candle = {
            "open": price * (1 - random.random() * 0.002),
            "high": price * (1 + random.random() * 0.005),
            "low": price * (1 - random.random() * 0.005),
            "close": price,
            "volume": 50 + random.random() * 200,
        }
        engine.ingest_candle(candle)
        state = engine.tick()

        if i % 30 == 0 and i > 0:
            print(
                f"Tick {state['tick']:>4} | "
                f"${state['price']:>9,.2f} | "
                f"{state['regime']:<10} | "
                f"{state['strategy']:<15} | "
                f"{state['signal']['action']:<4} {state['signal']['confidence']:.2f} | "
                f"Equity: ${state['portfolio']['equity']:>10,.2f} | "
                f"P&L: {state['portfolio']['pnl_pct']:>+.2f}%"
            )

        if state.get("last_trade"):
            t = state["last_trade"]
            print(f"  >>> TRADE: {t['action']} {t['amount']:.6f} @ ${t['price']:,.2f} — {t['reason'][:60]}")

    print()
    print(engine.get_performance_report())
