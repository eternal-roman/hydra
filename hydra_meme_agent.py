"""APEX Meme Engine — standalone competition-token trading agent.

Isolation guarantee: imports nothing from hydra_engine, hydra_agent,
hydra_brain, hydra_quant_rules, or hydra_pair_registry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional
import websockets

# Load .env file if present (same loader as hydra_agent.py — no dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                    _v = _v[1:-1]
                if _v and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v


# ─── Constants ────────────────────────────────────────────────────────────────

WS_PORT_BASE = 8770
WS_PORT_RANGE = 10  # try 8770-8779
PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_meme_prefs.json")
CANDLE_INTERVAL = 15         # minutes — 15m bars proven viable for ALT ATR
WARMUP_BARS = 0              # hot-load history, no warmup gate
CANDLE_BUFFER_SIZE = 100
OBI_POLL_INTERVAL = 10       # seconds
COMPETITION_SCAN_INTERVAL = 900  # 15 minutes
KRAKEN_REST_FLOOR = 2.0      # seconds between CLI calls
RSI_PERIOD = 9
VOL_EMA_PERIOD = 10
OBI_ENTRY_THRESHOLD = 0.20
OBI_BOOK_FADE = -0.20
RSI_ENTRY_LOW = 35
RSI_ENTRY_HIGH = 72
RSI_EXHAUST = 75
VOLUME_SPIKE_MULTIPLIER = 1.5
VOLUME_DEATH_MULTIPLIER = 0.4
ASK_WALL_USD_LIMIT = 500.0
PROFIT_TARGET_PCT = 0.020    # 2.0% — achievable on 15m bars (avg 8-12 bars)
HARD_STOP_PCT = -0.012       # -1.2% — cut losers fast, asymmetric R:R
TIME_STOP_CANDLES = 12       # 12 bars × 15min = 3h — if no move, exit
OBI_LEVELS = 5
TAKER_SLIPPAGE_BPS = 5       # 0.05% — limit at ask+0.05% for BUY
SLIPPAGE_CAP_BPS = 10        # 0.10% — reject if book moves more
SELL_MAX_RETRIES = 5         # abandon after N failed sell attempts
TRAILING_ACTIVATE_PCT = 0.008  # activate trailing stop after 0.8% gain
TRAILING_OFFSET_PCT = 0.005   # trail 0.5% below peak — lock gains early

COMPETITION_ANOMALY_RATIO = 5.0
COMPETITION_EMA_ALPHA = 1 / 7

EXTENSION_MAX_PCT = 0.08   # block entry when price is >8% above slow EMA
REENTRY_COOLDOWN_BARS = 2  # bars to wait after exit before re-entering
ATR_MIN_PCT = 0.003  # minimum ATR as fraction of price (0.3% — realistic for 15m)

# Bounce-mode entry thresholds
BOUNCE_RSI_THRESHOLD = 28    # enter bounce when RSI < 28 (deeply oversold)
BOUNCE_VOL_SPIKE_MULT = 1.8  # require 1.8x vol EMA (capitulation selling)
BOUNCE_MAX_EMA50_DIST = -0.12  # reject if price >12% below EMA(50) (freefall)
BOUNCE_PROFIT_PCT = 0.015    # 1.5% profit target for bounce trades
BOUNCE_STOP_PCT = -0.010     # -1.0% stop for bounce trades — tight
BOUNCE_RSI_EXIT = 48         # exit bounce when RSI recovers above 48
BOUNCE_TIME_STOP = 10        # 10-bar timeout (2.5h on 15m bars)

BOUNCE_REVERSAL_REQUIRED = True
MOMENTUM_MAX_RED_BARS = 3
STALE_PROFIT_BARS = 5        # 5 bars (1.25h) — more patience on 15m
STALE_PROFIT_MIN_PCT = 0.004  # 0.4% minimum gain to qualify

# Consecutive-loss halt: after N stops in a row, pause for this many bars
CONSEC_LOSS_HALT_THRESHOLD = 3  # 3 stops → halt
CONSEC_LOSS_HALT_BARS = 8       # sit out 2h (8×15m)

# Macro regime: pair's own EMA50 direction over N bars must not be declining
MACRO_EMA50_LOOKBACK = 6  # check EMA50 slope over last 6 bars (1.5h)

# BTC regime awareness: polls BTC/USD to detect market-wide dumps.
BTC_REGIME_POLL_INTERVAL = 300  # 5 minutes
BTC_REGIME_CANDLES = 4          # look back 4 x 15-min bars = 1 hour
BTC_CRASH_THRESHOLD = -0.02    # -2% BTC move in 1h = risk-off for alts
BTC_DUMP_RSI_CEILING = 35      # BTC RSI below this = alt entries gated

# Half-Kelly position sizing
KELLY_DEFAULT_WIN_RATE = 0.45  # conservative cold-start assumption
KELLY_DEFAULT_PAYOFF = 1.5     # target/stop ratio for the pair profile
KELLY_MIN_FRACTION = 0.05      # floor: never less than 5% of base
KELLY_MAX_FRACTION = 0.50      # ceiling: never more than 50% of base
BASE_CAPITAL = 600.0           # total capital pool for Half-Kelly computation


# ─── Per-Pair Tuning Profiles ────────────────────────────────────────────────

@dataclass
class PairProfile:
    """Per-pair parameter tuning. Defaults match the original module constants."""
    rsi_entry_low: float = RSI_ENTRY_LOW
    rsi_entry_high: float = RSI_ENTRY_HIGH
    rsi_exhaust: float = RSI_EXHAUST
    volume_spike_mult: float = VOLUME_SPIKE_MULTIPLIER
    volume_death_mult: float = VOLUME_DEATH_MULTIPLIER
    obi_entry_threshold: float = OBI_ENTRY_THRESHOLD
    obi_book_fade: float = OBI_BOOK_FADE
    ask_wall_usd_limit: float = ASK_WALL_USD_LIMIT
    atr_min_pct: float = ATR_MIN_PCT
    profit_target_pct: float = PROFIT_TARGET_PCT
    hard_stop_pct: float = HARD_STOP_PCT
    time_stop_candles: int = TIME_STOP_CANDLES
    trailing_activate_pct: float = TRAILING_ACTIVATE_PCT
    trailing_offset_pct: float = TRAILING_OFFSET_PCT
    extension_max_pct: float = EXTENSION_MAX_PCT
    bounce_rsi_threshold: float = BOUNCE_RSI_THRESHOLD
    bounce_vol_spike_mult: float = BOUNCE_VOL_SPIKE_MULT
    bounce_max_ema50_dist: float = BOUNCE_MAX_EMA50_DIST
    bounce_profit_pct: float = BOUNCE_PROFIT_PCT
    bounce_stop_pct: float = BOUNCE_STOP_PCT
    bounce_rsi_exit: float = BOUNCE_RSI_EXIT
    bounce_time_stop: int = BOUNCE_TIME_STOP
    bounce_reversal_required: bool = BOUNCE_REVERSAL_REQUIRED
    momentum_max_red_bars: int = MOMENTUM_MAX_RED_BARS
    stale_profit_bars: int = STALE_PROFIT_BARS
    stale_profit_min_pct: float = STALE_PROFIT_MIN_PCT


PROFILES: dict[str, PairProfile] = {
    # NIGHT/USD (Midnight/Cardano L2): low-cap, thin book, spiky.
    # 15m ATR ~0.48%. Strategy: enter on RSI dips + volume, tight stop (-1.5%),
    # trail early (0.8% → 0.5% offset), let runners reach 2.5%.
    # R:R = 2.5:1.5 = 1.67:1 → profitable at 40% WR.
    "NIGHT/USD": PairProfile(
        rsi_entry_low=30,
        rsi_entry_high=72,
        rsi_exhaust=72,
        volume_spike_mult=2.0,
        volume_death_mult=0.35,
        obi_entry_threshold=0.12,
        obi_book_fade=-0.20,
        ask_wall_usd_limit=500.0,
        atr_min_pct=0.003,
        profit_target_pct=0.025,
        hard_stop_pct=-0.015,
        time_stop_candles=12,
        trailing_activate_pct=0.010,
        trailing_offset_pct=0.006,
        extension_max_pct=0.08,
        bounce_rsi_threshold=25,
        bounce_vol_spike_mult=2.0,
        bounce_max_ema50_dist=-0.12,
        bounce_profit_pct=0.018,
        bounce_stop_pct=-0.012,
        bounce_rsi_exit=45,
        bounce_time_stop=10,
        bounce_reversal_required=True,
        momentum_max_red_bars=2,
        stale_profit_bars=5,
        stale_profit_min_pct=0.004,
    ),
    # AAVE/USD (DeFi blue-chip): deeper book, steadier trends.
    # 15m ATR ~0.38%. Strategy: buy confirmed dips with trend, tight stop (-1.2%),
    # trail at 0.6% with 0.4% offset. Target 1.5%.
    # R:R = 1.5:1.2 = 1.25:1 → profitable at 45% WR.
    "AAVE/USD": PairProfile(
        rsi_entry_low=32,
        rsi_entry_high=70,
        rsi_exhaust=70,
        volume_spike_mult=1.5,
        volume_death_mult=0.4,
        obi_entry_threshold=0.08,
        obi_book_fade=-0.15,
        ask_wall_usd_limit=2000.0,
        atr_min_pct=0.002,
        profit_target_pct=0.015,
        hard_stop_pct=-0.012,
        time_stop_candles=14,
        trailing_activate_pct=0.006,
        trailing_offset_pct=0.004,
        extension_max_pct=0.06,
        bounce_rsi_threshold=28,
        bounce_vol_spike_mult=1.8,
        bounce_max_ema50_dist=-0.10,
        bounce_profit_pct=0.012,
        bounce_stop_pct=-0.010,
        bounce_rsi_exit=46,
        bounce_time_stop=12,
        bounce_reversal_required=True,
        momentum_max_red_bars=3,
        stale_profit_bars=6,
        stale_profit_min_pct=0.003,
    ),
    # AAVE/BTC: illiquid BTC-quoted pair. 15m ATR ~0.04% but spikes to 0.2%+.
    # Strategy: only enter on volume with very tight stop. Patient hold.
    # R:R = 1.2:1.0 = 1.2:1 → profitable at 45% WR.
    "AAVE/BTC": PairProfile(
        rsi_entry_low=32,
        rsi_entry_high=68,
        rsi_exhaust=68,
        volume_spike_mult=1.8,
        volume_death_mult=0.4,
        obi_entry_threshold=0.10,
        obi_book_fade=-0.15,
        ask_wall_usd_limit=1500.0,
        atr_min_pct=0.001,
        profit_target_pct=0.012,
        hard_stop_pct=-0.010,
        time_stop_candles=16,
        trailing_activate_pct=0.005,
        trailing_offset_pct=0.0035,
        extension_max_pct=0.05,
        bounce_rsi_threshold=28,
        bounce_vol_spike_mult=2.0,
        bounce_max_ema50_dist=-0.08,
        bounce_profit_pct=0.010,
        bounce_stop_pct=-0.008,
        bounce_rsi_exit=44,
        bounce_time_stop=14,
        bounce_reversal_required=True,
        momentum_max_red_bars=3,
        stale_profit_bars=6,
        stale_profit_min_pct=0.003,
    ),
}

DEFAULT_PROFILE = PairProfile()

COMPETITION_SEED_PAIRS = [
    "NIGHT/USD", "AAVE/USD", "AAVE/BTC",
]


# ─── Three-Quarter Kelly Position Sizing ──────────────────────────────────────

KELLY_FRACTION = 0.75  # 3/4 Kelly — more aggressive than half, still bounded

def half_kelly_size(win_rate: float, avg_payoff: float, confidence: float) -> float:
    """Compute 3/4-Kelly position size.

    Kelly% = (p * b - q) / b  where p=win_rate, b=avg_payoff, q=1-p
    Fractional Kelly = Kelly% * KELLY_FRACTION (0.75)
    Final size = BASE_CAPITAL * clamp(frac_kelly, MIN, MAX) * confidence
    """
    if win_rate <= 0 or avg_payoff <= 0:
        return BASE_CAPITAL * KELLY_MIN_FRACTION * confidence
    q = 1.0 - win_rate
    kelly = (win_rate * avg_payoff - q) / avg_payoff
    if kelly <= 0:
        return BASE_CAPITAL * KELLY_MIN_FRACTION * confidence
    fk = kelly * KELLY_FRACTION
    fk = max(KELLY_MIN_FRACTION, min(KELLY_MAX_FRACTION, fk))
    return BASE_CAPITAL * fk * confidence


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class CandleBar:
    ts: int           # Unix timestamp of bar open
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


@dataclass
class Position:
    entry_price: float
    qty: float
    notional_usd: float
    entry_ts: int
    candles_held: int = 0
    order_id: str = ""
    peak_price: float = 0.0
    entry_mode: str = "momentum"  # "momentum" or "bounce"
    low_vol_bars: int = 0  # consecutive bars below volume_death threshold


@dataclass
class TradeRecord:
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees_usd: float
    net_pnl: float
    exit_reason: str
    hold_candles: int


# ─── Pure Indicator Functions ──────────────────────────────────────────────────

def wilder_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    """Wilder EMA RSI. Returns 50.0 when insufficient data (neutral)."""
    if len(closes) < period + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def vol_ema(values: list[float], period: int = VOL_EMA_PERIOD) -> float:
    """Standard EMA with alpha = 2/(period+1). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def compute_obi(
    bids: list[tuple],
    asks: list[tuple],
    levels: int = OBI_LEVELS,
) -> float:
    """Order Book Imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth).

    Each entry is (price, qty) as floats or strings. Returns 0.0 on empty book.
    """
    bid_depth = sum(float(p) * float(q) for p, q in bids[:levels])
    ask_depth = sum(float(p) * float(q) for p, q in asks[:levels])
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 0.0 else 0.0


def compute_vwap(bars: list[CandleBar]) -> float:
    """Close-price VWAP across all provided bars (close * volume weighted).

    Uses close price, not typical price (H+L+C)/3 — intentional for
    compatibility with Kraken OHLC candle format. Returns 0.0 for empty list.
    """
    total_pv = sum(b.close * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    return total_pv / total_v if total_v > 0.0 else 0.0


EMA_TREND_FAST = 8
EMA_TREND_SLOW = 21


def ema(values: list[float], period: int) -> float:
    """Standard EMA with alpha = 2/(period+1). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def atr_pct(bars: list, period: int = 5) -> float:
    """ATR as a fraction of current price. Returns 0.0 when insufficient data."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(len(bars) - period, len(bars)):
        prev_close = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low,
                 abs(bars[i].high - prev_close),
                 abs(bars[i].low - prev_close))
        trs.append(tr)
    atr_val = sum(trs) / len(trs)
    price = bars[-1].close
    return atr_val / price if price > 0 else 0.0


# ─── Signal Engine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """Evaluates 5 entry gates and 6 exit triggers against candle history."""

    def __init__(self, profile: PairProfile | None = None):
        self._bars: list[CandleBar] = []
        self._vwap_cum_pv: float = 0.0
        self._vwap_cum_v: float = 0.0
        self._p: PairProfile = profile or DEFAULT_PROFILE

    def add_bar(self, bar: CandleBar) -> None:
        """Add a closed bar to the buffer. Trims to CANDLE_BUFFER_SIZE."""
        self._bars.append(bar)
        self._vwap_cum_pv += bar.close * bar.volume
        self._vwap_cum_v += bar.volume
        if len(self._bars) > CANDLE_BUFFER_SIZE:
            oldest = self._bars.pop(0)
            self._vwap_cum_pv -= oldest.close * oldest.volume
            self._vwap_cum_v -= oldest.volume

    def is_warmed_up(self) -> bool:
        return len(self._bars) >= WARMUP_BARS

    @property
    def session_vwap(self) -> float:
        return self._vwap_cum_pv / self._vwap_cum_v if self._vwap_cum_v > 0 else 0.0

    @property
    def current_rsi(self) -> float:
        closes = [b.close for b in self._bars]
        return wilder_rsi(closes)

    @property
    def vol_ema_baseline(self) -> float:
        volumes = [b.volume for b in self._bars]
        return vol_ema(volumes)

    def evaluate_entry_gates(
        self,
        latest_bar: CandleBar,
        obi: float,
        ask_wall_usd: float,
    ) -> dict:
        """Evaluate dual-mode entry gates. Returns dict with gate booleans + entry_mode.

        Mode A (momentum): uptrend + RSI sweet-spot + volume spike + extension guard
        Mode B (bounce): deeply oversold + capitulation volume + not in freefall
        """
        p = self._p
        vol_baseline = self.vol_ema_baseline
        closes = [b.close for b in self._bars]
        rsi = wilder_rsi(closes)
        vwap = self.session_vwap

        ema_fast_val = ema(closes, EMA_TREND_FAST) if len(self._bars) >= EMA_TREND_FAST else 0
        ema_slow_val = ema(closes, EMA_TREND_SLOW) if len(self._bars) >= EMA_TREND_SLOW else 0
        ema50_val = ema(closes, 50) if len(self._bars) >= 50 else ema_slow_val

        trend_aligned = ema_fast_val > ema_slow_val if ema_slow_val > 0 else True
        extension = (latest_bar.close - ema_slow_val) / ema_slow_val if ema_slow_val > 0 else 0
        not_extended = extension <= p.extension_max_pct

        cur_atr_pct = atr_pct(self._bars)
        vol_regime_pass = cur_atr_pct >= p.atr_min_pct

        # Momentum gate checks
        mom_volume = latest_bar.volume > p.volume_spike_mult * vol_baseline
        mom_obi = obi > p.obi_entry_threshold
        mom_vwap = latest_bar.close > vwap if vwap > 0 else False
        mom_rsi = p.rsi_entry_low <= rsi <= p.rsi_entry_high
        mom_wall = ask_wall_usd < p.ask_wall_usd_limit
        mom_pass = all([mom_volume, mom_obi, mom_vwap, mom_rsi,
                        mom_wall, trend_aligned, not_extended, vol_regime_pass])

        # Bounce gate checks
        ema50_dist = (latest_bar.close - ema50_val) / ema50_val if ema50_val > 0 else -1
        bounce_rsi = rsi < p.bounce_rsi_threshold and rsi > 0
        bounce_vol = latest_bar.volume > p.bounce_vol_spike_mult * vol_baseline
        bounce_floor = ema50_dist > p.bounce_max_ema50_dist
        # Reversal confirmation: latest bar must close above prior bar's close
        # (proves buying pressure arrested the slide — blocks falling knives)
        if p.bounce_reversal_required and len(self._bars) >= 2:
            bounce_reversal = latest_bar.close > self._bars[-2].close
        else:
            bounce_reversal = True
        bounce_pass = all([bounce_rsi, bounce_vol, bounce_floor,
                           vol_regime_pass, bounce_reversal])

        # Momentum consecutive red-bar filter: block entry when the last N bars
        # are all red (close < open) — trend may look aligned via EMA lag but
        # actual price action is selling off
        if len(self._bars) >= p.momentum_max_red_bars:
            recent = self._bars[-p.momentum_max_red_bars:]
            all_red = all(b.close < b.open for b in recent)
        else:
            all_red = False
        not_bleeding = not all_red

        # Macro EMA50 slope gate: if EMA50 is declining over recent bars,
        # the pair is in a macro downtrend — don't enter longs regardless
        # of short-term RSI dips (they're falling knives in this context)
        macro_ok = True
        if len(self._bars) >= 50 + MACRO_EMA50_LOOKBACK:
            ema50_now = ema(closes, 50)
            closes_earlier = closes[:-MACRO_EMA50_LOOKBACK]
            ema50_earlier = ema(closes_earlier, 50)
            macro_ok = ema50_now >= ema50_earlier

        entry_mode = "none"
        if mom_pass and not_bleeding and macro_ok:
            entry_mode = "momentum"
        elif bounce_pass and macro_ok:
            entry_mode = "bounce"

        # Confidence score (0.0-1.0): only meaningful when entry_mode != "none"
        if entry_mode != "none":
            # RSI depth: how far RSI is from the threshold (deeper = higher conviction)
            if entry_mode == "momentum":
                rsi_range = p.rsi_entry_high - p.rsi_entry_low
                rsi_depth = ((p.rsi_entry_high - rsi) / rsi_range
                             if rsi_range > 0 else 0.0)
            else:  # bounce
                rsi_depth = ((p.bounce_rsi_threshold - rsi) / p.bounce_rsi_threshold
                             if p.bounce_rsi_threshold > 0 else 0.0)
            rsi_depth = max(0.0, min(1.0, rsi_depth))
            # OBI strength: obi / 1.0 clamped 0-1
            obi_strength = max(0.0, min(1.0, obi / 1.0))
            # Volume surge: caps at 2x the required threshold
            spike_mult = (p.bounce_vol_spike_mult if entry_mode == "bounce"
                          else p.volume_spike_mult)
            vol_surge = (min(latest_bar.volume / (vol_baseline * spike_mult), 2.0) / 2.0
                         if vol_baseline > 0 and spike_mult > 0 else 0.0)
            # Trend strength: (ema_fast - ema_slow) / ema_slow * 100, clamped 0-5, /5
            if ema_slow_val > 0:
                trend_pct = (ema_fast_val - ema_slow_val) / ema_slow_val * 100
                trend_strength = max(0.0, min(5.0, trend_pct)) / 5.0
            else:
                trend_strength = 0.0
            confidence = (rsi_depth * 0.30
                          + obi_strength * 0.20
                          + vol_surge * 0.25
                          + trend_strength * 0.25)
            confidence = max(0.0, min(1.0, confidence))
        else:
            confidence = 0.0

        gates = {
            "volume_spike": mom_volume,
            "obi": mom_obi,
            "vwap_align": mom_vwap,
            "rsi_window": mom_rsi,
            "ask_wall_clear": mom_wall,
            "trend_aligned": trend_aligned,
            "not_extended": not_extended,
            "not_bleeding": not_bleeding,
            "macro_trend": macro_ok,
            "vol_regime": vol_regime_pass,
            "bounce_rsi": bounce_rsi,
            "bounce_vol": bounce_vol,
            "bounce_floor": bounce_floor,
            "bounce_reversal": bounce_reversal,
            "rsi_value": round(rsi, 1),
            "vwap_value": round(vwap, 8),
            "vol_ema_value": round(vol_baseline, 2),
            "atr_pct": round(cur_atr_pct, 4),
            "ema50_dist": round(ema50_dist, 4),
            "extension_pct": round(extension, 4),
            "entry_mode": entry_mode,
            "all_pass": entry_mode != "none",
            "confidence": round(confidence, 4),
        }
        return gates

    def evaluate_exit_bar(self, position, latest_bar: CandleBar) -> Optional[str]:
        """Bar-close exit triggers: mode-aware RSI exit, time stop, volume death, trailing stop.

        position is a Position dataclass. Returns exit reason string or None.
        """
        p = self._p
        rsi = wilder_rsi([b.close for b in self._bars])

        if position.entry_mode == "bounce":
            if rsi > p.bounce_rsi_exit:
                return "rsi_exit"
            if position.candles_held >= p.bounce_time_stop:
                return "time_stop"
        else:
            if rsi > p.rsi_exhaust:
                return "rsi_exhaust"
            if position.candles_held >= p.time_stop_candles:
                return "time_stop"

        # Trailing stop (both modes): if peak gain >= activation, trail from peak
        if position.peak_price > 0 and position.entry_price > 0:
            peak_pct = (position.peak_price - position.entry_price) / position.entry_price
            if peak_pct >= p.trailing_activate_pct:
                trail_level = position.peak_price * (1 - p.trailing_offset_pct)
                if latest_bar.close <= trail_level:
                    return "trailing_stop"

        # Stale-profit exit: in profit for N bars but never hit trailing
        # activation — the move is exhausted, take what's there
        if position.entry_price > 0 and position.candles_held >= p.stale_profit_bars:
            unrealised_pct = (latest_bar.close - position.entry_price) / position.entry_price
            peak_pct = ((position.peak_price - position.entry_price) / position.entry_price
                        if position.peak_price > 0 else 0.0)
            if (unrealised_pct >= p.stale_profit_min_pct
                    and peak_pct < p.trailing_activate_pct):
                return "stale_profit"

        # Volume death: require 6+ bars held AND 2 consecutive low-volume bars.
        # Single quiet bars are normal mean-reversion on 15m — not a signal to exit.
        if position.candles_held >= 6:
            vol_baseline = self.vol_ema_baseline
            if vol_baseline > 0 and latest_bar.volume < p.volume_death_mult * vol_baseline:
                position.low_vol_bars += 1
                if position.low_vol_bars >= 2:
                    return "volume_death"
            else:
                position.low_vol_bars = 0
        return None

    def evaluate_exit_intracandle(
        self,
        position,
        mid_price: float,
        obi: float,
    ) -> Optional[str]:
        """10-second exit triggers: mode-aware profit target, hard stop, trailing, book fade.

        position is a Position dataclass. Returns exit reason string or None.
        """
        p = self._p
        pct_change = (mid_price - position.entry_price) / position.entry_price

        if position.entry_mode == "bounce":
            if pct_change >= p.bounce_profit_pct:
                return "profit_target"
            if pct_change <= p.bounce_stop_pct:
                return "hard_stop"
        else:
            if pct_change >= p.profit_target_pct:
                return "profit_target"
            if pct_change <= p.hard_stop_pct:
                return "hard_stop"

        # Trailing stop check on mid-price
        if position.peak_price > 0 and position.entry_price > 0:
            peak_pct = (position.peak_price - position.entry_price) / position.entry_price
            if peak_pct >= p.trailing_activate_pct:
                trail_level = position.peak_price * (1 - p.trailing_offset_pct)
                if mid_price <= trail_level:
                    return "trailing_stop"

        if obi < p.obi_book_fade:
            return "book_fade"
        return None


# ─── BTC Regime Monitor ───────────────────────────────────────────────────────

class BtcRegimeMonitor:
    """Polls BTC/USD OHLC to detect market-wide risk-off regimes.

    ALTs are strongly correlated with BTC.  When BTC drops >2% in 1h or
    its RSI is below 35, new alt entries are gated — avoids buying into
    a broad-market dump that will drag alts down harder.
    """

    def __init__(self):
        self._bars: list[CandleBar] = []
        self._lock = threading.Lock()
        self._risk_off: bool = False
        self._btc_rsi: float = 50.0
        self._btc_1h_chg: float = 0.0
        self._last_poll: float = 0.0

    @property
    def is_risk_off(self) -> bool:
        with self._lock:
            return self._risk_off

    @property
    def btc_rsi(self) -> float:
        with self._lock:
            return self._btc_rsi

    @property
    def btc_1h_change(self) -> float:
        with self._lock:
            return self._btc_1h_chg

    def poll(self) -> None:
        """Fetch BTC/USD 5-min candles and update regime state."""
        now = time.time()
        if now - self._last_poll < BTC_REGIME_POLL_INTERVAL:
            return
        result = _kraken_cli(["ohlc", "XBTUSD", "--interval", str(CANDLE_INTERVAL)])
        self._last_poll = time.time()
        if "error" in result:
            return
        key = next((k for k in result if k != "last"), None)
        if not key:
            return
        raw = result[key]
        closed = raw[-(BTC_REGIME_CANDLES + 1):-1] if len(raw) > BTC_REGIME_CANDLES else raw[:-1]
        bars = []
        for b in closed:
            bars.append(CandleBar(
                ts=int(b[0]), open=float(b[1]), high=float(b[2]),
                low=float(b[3]), close=float(b[4]), vwap=float(b[5]),
                volume=float(b[6]), count=int(b[7]),
            ))
        if not bars:
            return
        closes = [b.close for b in bars]
        rsi = wilder_rsi(closes)
        first_close = bars[0].close
        last_close = bars[-1].close
        pct_chg = (last_close - first_close) / first_close if first_close > 0 else 0.0
        risk_off = pct_chg <= BTC_CRASH_THRESHOLD or rsi < BTC_DUMP_RSI_CEILING
        with self._lock:
            self._bars = bars
            self._btc_rsi = rsi
            self._btc_1h_chg = pct_chg
            self._risk_off = risk_off
        if risk_off:
            print(f"[APEX] BTC RISK-OFF: 1h Δ={pct_chg:+.2%}  RSI={rsi:.1f} — alt entries gated")


# ─── Competition Detector ──────────────────────────────────────────────────────

class CompetitionDetector:
    """Monitors token volume baselines and detects competition anomalies."""

    def __init__(self, watchlist_path: str):
        self._path = watchlist_path
        self._lock = threading.Lock()
        self._data: dict = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> dict:
        seed_set = set(COMPETITION_SEED_PAIRS)
        if os.path.exists(self._path):
            with open(self._path) as f:
                data = json.load(f)
            old_pairs = {t["pair"] for t in data.get("tokens", [])}
            data["tokens"] = [t for t in data.get("tokens", []) if t["pair"] in seed_set]
            for p in COMPETITION_SEED_PAIRS:
                if not any(t["pair"] == p for t in data["tokens"]):
                    data["tokens"].append({
                        "pair": p, "baseline_volume_7d": None, "last_updated": None,
                        "competition_type": None, "competition_type_confirmed": False,
                        "alert_suppressed_until": None,
                    })
            new_pairs = {t["pair"] for t in data["tokens"]}
            if old_pairs != new_pairs:
                print(f"[APEX] Watchlist synced: {len(old_pairs)} → {len(new_pairs)} tokens")
                self._save(data)
            return data
        data = {
            "tokens": [
                {
                    "pair": p,
                    "baseline_volume_7d": None,
                    "last_updated": None,
                    "competition_type": None,
                    "competition_type_confirmed": False,
                    "alert_suppressed_until": None,
                }
                for p in COMPETITION_SEED_PAIRS
            ],
            "last_scan": None,
        }
        self._save(data)
        return data

    def _save(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def _find_token(self, pair: str) -> Optional[dict]:
        for t in self._data["tokens"]:
            if t["pair"] == pair:
                return t
        return None

    def _find_or_add_token(self, pair: str) -> dict:
        token = self._find_token(pair)
        if token is None:
            token = {
                "pair": pair,
                "baseline_volume_7d": None,
                "last_updated": None,
                "competition_type": None,
                "competition_type_confirmed": False,
                "alert_suppressed_until": None,
            }
            self._data["tokens"].append(token)
        return token

    def _set_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["baseline_volume_7d"] = volume
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _get_baseline(self, pair: str) -> Optional[float]:
        token = self._find_token(pair)
        return token["baseline_volume_7d"] if token else None

    def _update_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            old = token["baseline_volume_7d"]
            if old is None:
                token["baseline_volume_7d"] = volume
            else:
                token["baseline_volume_7d"] = (
                    COMPETITION_EMA_ALPHA * volume + (1 - COMPETITION_EMA_ALPHA) * old
                )
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _is_anomaly(self, pair: str, current_volume: float) -> bool:
        baseline = self._get_baseline(pair)
        if baseline is None or baseline <= 0:
            return False
        return (current_volume / baseline) >= COMPETITION_ANOMALY_RATIO

    def _suppress(self, pair: str, until: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["alert_suppressed_until"] = until
            self._save(self._data)

    def _is_suppressed(self, pair: str) -> bool:
        token = self._find_token(pair)
        if token is None:
            return False
        until = token.get("alert_suppressed_until")
        return until is not None and time.time() < until

    def infer_competition_type(self, pair: str) -> str:
        """Volume-pattern heuristic. Returns 'volume', 'pnl', 'rebate', or 'unknown'."""
        token = self._find_token(pair)
        if token and token.get("competition_type_confirmed"):
            return token.get("competition_type") or "unknown"
        baseline = self._get_baseline(pair)
        if baseline is None:
            return "unknown"
        return "volume"

    def get_all_tokens(self) -> list[dict]:
        return list(self._data.get("tokens", []))


# ─── Session State ─────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    pair: str = ""
    engine_state: str = "idle"   # idle | running | halted
    open_position: Optional[dict] = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trade_count: int = 0


def save_session(state: SessionState, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)


def load_pair_prefs() -> dict:
    """Load persistent pair preferences (disabled_pairs set)."""
    if not os.path.exists(PREFS_PATH):
        return {}
    try:
        with open(PREFS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_pair_prefs(prefs: dict) -> None:
    """Atomic write of pair preferences."""
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prefs, f, indent=2)
    os.replace(tmp, PREFS_PATH)


def load_session_state(path: str) -> Optional[dict]:
    """Load session state from file. Returns dict or None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


_journal_lock = threading.Lock()


def append_journal(record: TradeRecord, path: str) -> None:
    with _journal_lock:
        existing: list = []
        try:
            if os.path.exists(path):
                with open(path) as f:
                    existing = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[APEX] Warning: journal read failed, appending to fresh list: {e}")
        existing.append(asdict(record))
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[APEX] ERROR: journal write failed — trade record may be lost: {e}")


def load_journal(path: str) -> list[TradeRecord]:
    """Load trade records from journal file. Skips corrupt entries, keeps valid ones."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[APEX] Warning: could not load journal {path}: {e}")
        return []
    records: list[TradeRecord] = []
    for i, entry in enumerate(entries):
        try:
            records.append(TradeRecord(**entry))
        except (TypeError, KeyError) as e:
            print(f"[APEX] Warning: skipping corrupt journal entry {i}: {e}")
    return records


# ─── Kraken CLI ────────────────────────────────────────────────────────────────

_cli_lock = threading.Lock()
_cli_last_call: float = 0.0


def _kraken_cli(args: list[str], timeout: int = 20) -> dict:
    """Execute a kraken CLI command via WSL and return parsed JSON.

    All args are shlex-quoted to prevent injection (matches hydra_kraken_cli.py pattern).
    Global lock + 2s floor enforces rate limit across all concurrent callers.
    """
    global _cli_last_call
    with _cli_lock:
        now = time.time()
        wait = KRAKEN_REST_FLOOR - (now - _cli_last_call)
        if wait > 0:
            time.sleep(wait)
        _cli_last_call = time.time()
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    cmd_str = "source ~/.cargo/env"
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if api_key and api_secret:
        cmd_str += (f" && export KRAKEN_API_KEY={shlex.quote(api_key)}"
                    f" && export KRAKEN_API_SECRET={shlex.quote(api_secret)}")
    cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
    cmd = ["wsl", "-d", os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu"), "--", "bash", "-c", cmd_str]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        rc = result.returncode
        if not stdout:
            return {"error": f"Empty response (exit code {rc})"}
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data:
            return data
        if rc != 0:
            return {"error": f"Non-zero exit code {rc}", "partial": data}
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "retryable": True}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}


def _query_fill(txid: str) -> Optional[dict]:
    """Query order fill status via CLI. Returns {status, avg_price, vol_exec} or None."""
    if not txid:
        return None
    result = _kraken_cli(["query-orders", txid])
    if "error" in result:
        return None
    order_data = result.get(txid)
    if not order_data:
        order_data = next(iter(result.values()), None) if result else None
    if not order_data or not isinstance(order_data, dict):
        return None
    status = order_data.get("status", "")
    return {
        "status": "filled" if status == "closed" else status,
        "avg_price": float(order_data.get("price", 0)),
        "vol_exec": float(order_data.get("vol_exec", 0)),
    }


def _cancel_order(txid: str) -> dict:
    """Cancel a specific order by txid."""
    if not txid:
        return {"error": "no txid"}
    return _kraken_cli(["order", "cancel", txid, "--yes"])


# ─── Meme Executor ─────────────────────────────────────────────────────────────

TAKER_FEE_RATE = 0.004   # 0.40% taker fee on competition tokens
MAKER_FEE_RATE = 0.0016  # 0.16% maker fee — for backtest comparison only (not used in live orders)


def _query_pair_precision(pair: str) -> tuple[int, int, float, float]:
    """Query Kraken for pair decimals. Returns (price_dec, lot_dec, ordermin, costmin)."""
    pair_nodash = pair.replace("/", "")
    result = _kraken_cli(["pairs", "--pair", pair_nodash])
    if "error" not in result:
        pdata = result.get(pair_nodash) or next(iter(result.values()), {})
        return (
            int(pdata.get("pair_decimals", 8)),
            int(pdata.get("lot_decimals", 8)),
            float(pdata.get("ordermin", 0)),
            float(pdata.get("costmin", 0)),
        )
    return (8, 8, 0.0, 0.0)


class MemeExecutor:
    """Places taker limit orders and tracks position + daily P&L."""

    def __init__(self, pair: str, position_size: float, daily_cap: float,
                 price_decimals: int = 8, lot_decimals: int = 8,
                 ordermin: float = 0.0, costmin: float = 0.0):
        if daily_cap <= 0:
            raise ValueError(f"daily_cap must be positive, got {daily_cap}")
        self.pair = pair
        self.position_size = position_size
        self.daily_cap = daily_cap
        self.price_decimals = price_decimals
        self.lot_decimals = lot_decimals
        self.ordermin = ordermin
        self.costmin = costmin
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._halted: bool = False
        self._last_reset_date: str = time.strftime("%Y-%m-%d", time.gmtime())
        self._pair_nodash = pair.replace("/", "")

    def is_halted(self) -> bool:
        return self._halted or self._daily_loss <= -self.daily_cap

    def record_pnl(self, net_pnl: float) -> None:
        self._daily_pnl += net_pnl
        if net_pnl < 0:
            self._daily_loss += net_pnl
        if self._daily_loss <= -self.daily_cap:
            self._halted = True

    def maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._last_reset_date:
            self._daily_pnl = 0.0
            self._daily_loss = 0.0
            self._halted = False
            self._last_reset_date = today

    def _buy_limit_price(self, ask: float) -> float:
        return ask * (1 + TAKER_SLIPPAGE_BPS / 10_000)

    def _sell_limit_price(self, bid: float) -> float:
        return bid * (1 - TAKER_SLIPPAGE_BPS / 10_000)

    def _buy_qty(self, ask: float, size_override: Optional[float] = None) -> float:
        size = size_override if size_override is not None else self.position_size
        return size / ask

    def _compute_net_pnl(self, position: Position, exit_price: float) -> float:
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_notional = exit_price * position.qty
        exit_fee = exit_notional * TAKER_FEE_RATE
        return gross - entry_fee - exit_fee

    def place_buy(self, ask: float, mid: Optional[float] = None,
                  entry_mode: str = "momentum",
                  size_override: Optional[float] = None) -> Optional[Position]:
        """Place a taker BUY limit order. Returns Position on success, None on failure.

        If size_override is provided, it replaces self.position_size for qty computation.
        """
        if self.is_halted():
            return None
        limit_price = self._buy_limit_price(ask)
        if mid and mid > 0:
            slippage_bps = (limit_price - mid) / mid * 10_000
            if slippage_bps > SLIPPAGE_CAP_BPS:
                return None
        qty = self._buy_qty(ask, size_override)
        pfmt = f"{{:.{self.price_decimals}f}}"
        qfmt = f"{{:.{self.lot_decimals}f}}"
        result = _kraken_cli([
            "order", "buy",
            self.pair,
            qfmt.format(qty),
            "--type", "limit",
            "--price", pfmt.format(limit_price),
            "--yes",
        ])
        if "error" in result:
            return None
        order_id = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        fill = _query_fill(str(order_id))
        if fill and fill["status"] == "filled" and fill["avg_price"] > 0:
            actual_price = fill["avg_price"]
            actual_qty = fill["vol_exec"] if fill["vol_exec"] > 0 else qty
        else:
            actual_price = limit_price
            actual_qty = qty
            if fill:
                print(f"[APEX] BUY fill check: status={fill['status']} — using limit price as estimate")
        return Position(
            entry_price=actual_price,
            qty=actual_qty,
            notional_usd=actual_price * actual_qty,
            entry_ts=int(time.time()),
            order_id=str(order_id),
            peak_price=actual_price,
            entry_mode=entry_mode,
        )

    def place_sell(self, position: Position, bid: float, reason: str,
                   mid: Optional[float] = None) -> Optional[dict]:
        """Place a taker SELL limit order. Returns trade record dict, or None on failure."""
        limit_price = self._sell_limit_price(bid)
        if mid and mid > 0:
            slippage_bps = (mid - limit_price) / mid * 10_000
            if slippage_bps > SLIPPAGE_CAP_BPS:
                limit_price = mid * (1 - SLIPPAGE_CAP_BPS / 10_000)
        pfmt = f"{{:.{self.price_decimals}f}}"
        qfmt = f"{{:.{self.lot_decimals}f}}"
        result = _kraken_cli([
            "order", "sell",
            self.pair,
            qfmt.format(position.qty),
            "--type", "limit",
            "--price", pfmt.format(limit_price),
            "--yes",
        ])
        if "error" in result:
            return None
        sell_txid = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        fill = _query_fill(str(sell_txid))
        if fill and fill["status"] == "filled" and fill["avg_price"] > 0:
            exit_price = fill["avg_price"]
        else:
            exit_price = limit_price
            if fill:
                print(f"[APEX] SELL fill check: status={fill['status']} — using limit price as estimate")
        net_pnl = self._compute_net_pnl(position, exit_price)
        self.record_pnl(net_pnl)
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_fee = exit_price * position.qty * TAKER_FEE_RATE
        record = TradeRecord(
            entry_ts=position.entry_ts,
            exit_ts=int(time.time()),
            entry_price=position.entry_price,
            exit_price=exit_price,
            qty=position.qty,
            gross_pnl=gross,
            fees_usd=entry_fee + exit_fee,
            net_pnl=net_pnl,
            exit_reason=reason,
            hold_candles=position.candles_held,
        )
        return {"record": record, "order_result": result}


# ─── OBI Poller ────────────────────────────────────────────────────────────────

class OBIPoller:
    """Polls kraken orderbook every 10s and caches OBI + best bid/ask."""

    def __init__(self, pair: str):
        self.pair = pair
        self._pair_nodash = pair.replace("/", "")
        self._obi: float = 0.0
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0
        self._ask_wall: float = 999_999.0
        self._last_success: float = 0.0

    @property
    def is_stale(self) -> bool:
        """True if last successful poll was >60s ago."""
        return self._last_success == 0.0 or (time.time() - self._last_success > 60)

    def poll(self) -> None:
        """Fetch orderbook and update cached values."""
        result = _kraken_cli(["orderbook", self._pair_nodash, "--count", str(OBI_LEVELS)])
        if "error" in result:
            return
        # Kraken book response: {PAIR: {bids: [[price, qty, ts], ...], asks: [...]}}
        # Key may be Kraken-normalized (e.g. XBTUSDT) — fall back to first key.
        book = (result.get(self._pair_nodash)
                or result.get(self.pair)
                or (next(iter(result.values())) if result else {}))
        if not isinstance(book, dict):
            return
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            self._obi = compute_obi(
                [(b[0], b[1]) for b in bids],
                [(a[0], a[1]) for a in asks],
            )
            self._best_bid = float(bids[0][0])
            self._best_ask = float(asks[0][0])
            self._ask_wall = sum(float(a[0]) * float(a[1]) for a in asks[:3])
            self._last_success = time.time()

    @property
    def obi(self) -> float:
        return self._obi

    @property
    def mid_price(self) -> float:
        return (self._best_bid + self._best_ask) / 2 if self._best_bid else 0.0

    @property
    def best_bid(self) -> float:
        return self._best_bid

    @property
    def best_ask(self) -> float:
        return self._best_ask

    @property
    def ask_wall_usd(self) -> float:
        return self._ask_wall


# ─── Candle Aggregator ─────────────────────────────────────────────────────────

class CandleAggregator:
    """Subscribes to Kraken public WebSocket ohlc-5 channel.

    Fires on_bar callback with each newly closed CandleBar.
    Reconnects with exponential backoff on disconnect.
    """
    WS_URL = "wss://ws.kraken.com"

    def __init__(self, pair: str, on_bar):
        self.pair = pair
        self._on_bar = on_bar
        self._last_etime: str = ""
        self._last_bar: Optional[CandleBar] = None
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        backoff = 5
        while self._running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    backoff = 5  # reset on successful connect
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": [self.pair],
                        "subscription": {"name": "ohlc", "interval": CANDLE_INTERVAL},
                    }))
                    async for raw in ws:
                        if not self._running:
                            return
                        self._handle(raw)
            except Exception:
                if not self._running:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        # Subscription confirmation / heartbeat — skip
        if not isinstance(msg, list) or len(msg) < 4:
            return
        channel_name = msg[2] if len(msg) > 2 else ""
        if not str(channel_name).startswith("ohlc"):
            return
        ohlc = msg[1]  # [time, etime, open, high, low, close, vwap, volume, count]
        if len(ohlc) < 9:
            return
        etime = str(ohlc[1])
        # New etime means previous bar closed; emit that bar
        if etime != self._last_etime and self._last_bar is not None:
            self._on_bar(self._last_bar)
        # Update running bar
        self._last_etime = etime
        self._last_bar = CandleBar(
            ts=int(float(ohlc[0])),
            open=float(ohlc[2]),
            high=float(ohlc[3]),
            low=float(ohlc[4]),
            close=float(ohlc[5]),
            vwap=float(ohlc[6]),
            volume=float(ohlc[7]),
            count=int(ohlc[8]),
        )


# ─── Meme Agent ────────────────────────────────────────────────────────────────

class MemeAgent:
    """Orchestrates all components and broadcasts state on port 8766."""

    def __init__(self, pair: str, position_size: float, daily_cap: float,
                 session_path: str = "hydra_meme_session.json",
                 journal_path: str = "hydra_meme_journal.json",
                 watchlist_path: str = "hydra_meme_watchlist.json",
                 btc_monitor: BtcRegimeMonitor | None = None,
                 ws_port: int | None = None):
        self.pair = pair
        self._ws_port_fixed = ws_port
        self._position_size = position_size
        self._daily_cap = daily_cap
        self._clients: set = set()
        self._profile = PROFILES.get(pair, DEFAULT_PROFILE)
        self._signal_engine = SignalEngine(self._profile)
        self._obi_poller = OBIPoller(pair)
        self._btc_monitor = btc_monitor or BtcRegimeMonitor()
        price_dec, lot_dec, ordermin, costmin = _query_pair_precision(pair)
        print(f"[APEX] {pair}: price_decimals={price_dec}  lot_decimals={lot_dec}  ordermin={ordermin}  costmin=${costmin}")
        self._executor = MemeExecutor(pair, position_size, daily_cap,
                                      price_dec, lot_dec, ordermin, costmin)
        self._detector = CompetitionDetector(watchlist_path)
        self._candle_agg = CandleAggregator(pair, self._on_bar)
        self._position: Optional[Position] = None
        self._sell_pending_reason: Optional[str] = None
        self._sell_retry_count: int = 0
        self._exit_lock: asyncio.Lock = asyncio.Lock()
        self._session_path = session_path
        self._journal_path = journal_path
        self._trade_log: list[TradeRecord] = load_journal(journal_path)
        if self._trade_log:
            for t in self._trade_log:
                self._executor.record_pnl(t.net_pnl)
            total_pnl = sum(t.net_pnl for t in self._trade_log)
            print(f"[APEX] Loaded {len(self._trade_log)} trades from journal (net P&L: ${total_pnl:+.2f})")
        prev_session = load_session_state(session_path)
        if prev_session and prev_session.get("open_position"):
            op = prev_session["open_position"]
            print(f"[APEX] WARNING: previous session had open position -- "
                  f"qty={op.get('qty')} {pair} @ entry {op.get('entry_price')}")
            print(f"[APEX] WARNING: verify on Kraken that position is closed before continuing")
            print(f"[APEX] WARNING: engine will trade normally -- close stale position manually if needed")
        self._engine_state = "running"
        prefs = load_pair_prefs()
        parked = prefs.get("parked_pairs", [])
        self._enabled: bool = pair not in parked
        self._parked: bool = pair in parked  # persistent disable survives restarts
        if self._parked:
            print(f"[APEX] {pair} is PARKED (persistent disable) — entries blocked until re-enabled")
        self._sibling_agents: list["MemeAgent"] = []  # populated in multi-pair mode
        self._last_exit_bar_count: int = -REENTRY_COOLDOWN_BARS
        self._bar_count: int = 0
        self._consec_stops: int = 0
        self._halt_until_bar: int = 0

    # ── History seed ──

    async def _seed_history(self) -> None:
        """Hot-load history via CLI — engine goes straight to running."""
        pair_nodash = self.pair.replace("/", "")
        result = await asyncio.to_thread(
            _kraken_cli,
            ["ohlc", pair_nodash, "--interval", str(CANDLE_INTERVAL)],
        )
        if "error" in result:
            print(f"[APEX] History seed failed: {result.get('error')} — falling back to live warmup")
            return
        key = next((k for k in result if k != "last"), None)
        if not key:
            return
        raw_bars = result[key]
        # Seed up to CANDLE_BUFFER_SIZE closed bars (exclude the last — still open)
        seed_count = CANDLE_BUFFER_SIZE
        closed = raw_bars[-(seed_count + 1):-1] if len(raw_bars) > seed_count else raw_bars[:-1]
        for b in closed:
            bar = CandleBar(
                ts=int(b[0]), open=float(b[1]), high=float(b[2]),
                low=float(b[3]), close=float(b[4]), vwap=float(b[5]),
                volume=float(b[6]), count=int(b[7]),
            )
            self._signal_engine.add_bar(bar)
        n = len(self._signal_engine._bars)
        print(f"[APEX] Seeded {n} historical bars — hot, ready to trade")
        self._engine_state = "running"

    # ── WebSocket server ──

    async def _ws_handler(self, websocket) -> None:
        self._clients.add(websocket)
        pos_data = None
        if self._position is not None:
            p = self._position
            pos_data = {"entry_price": p.entry_price, "qty": p.qty,
                        "notional_usd": p.notional_usd, "entry_ts": p.entry_ts,
                        "candles_held": p.candles_held,
                        "entry_mode": p.entry_mode, "peak_price": p.peak_price}
        win_count = sum(1 for t in self._trade_log if t.net_pnl > 0)
        await websocket.send(json.dumps({
            "type": "initial_state",
            "engine_state": self._engine_state,
            "pair": self.pair,
            "enabled": self._enabled,
            "parked": self._parked,
            "candle_interval": CANDLE_INTERVAL,
            "position": pos_data,
            "session_pnl": self._executor._daily_pnl,
            "trade_count": len(self._trade_log),
            "daily_loss": self._executor._daily_loss,
            "win_rate": win_count / max(len(self._trade_log), 1),
            "trades": [{"entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
                        "entry_price": t.entry_price, "exit_price": t.exit_price,
                        "net_pnl": t.net_pnl, "exit_reason": t.exit_reason,
                        "hold_candles": t.hold_candles} for t in self._trade_log],
        }))
        bars = self._signal_engine._bars
        if bars:
            await websocket.send(json.dumps({
                "type": "candle_history",
                "bars": [{"ts": b.ts, "open": b.open, "high": b.high,
                           "low": b.low, "close": b.close, "volume": b.volume,
                           "vwap": b.vwap} for b in bars],
            }))
        tokens = self._detector.get_all_tokens()
        if any(t.get("current_volume") is not None for t in tokens):
            await websocket.send(json.dumps({
                "type": "watchlist_update",
                "tokens": tokens,
            }))
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "dismiss_alert":
                        pair = msg.get("pair", "")
                        if pair:
                            self._detector._suppress(pair, time.time() + 7200)
                    elif msg.get("type") == "stop_engine":
                        if self._position is not None:
                            await self._exit_position("manual_stop")
                        self._engine_state = "idle"
                        await self._broadcast({"type": "engine_state", "state": "idle", "pair": None})
                    elif msg.get("type") == "switch_pair":
                        new_pair = msg.get("pair")
                        if new_pair:
                            asyncio.ensure_future(self._switch_pair(new_pair))
                    elif msg.get("type") == "scan_now":
                        asyncio.ensure_future(self._run_competition_scan())
                    elif msg.get("type") == "enable_pair":
                        self._enabled = True
                        if self._parked:
                            self._parked = False
                            prefs = load_pair_prefs()
                            parked = set(prefs.get("parked_pairs", []))
                            parked.discard(self.pair)
                            prefs["parked_pairs"] = sorted(parked)
                            save_pair_prefs(prefs)
                        await self._broadcast({
                            "type": "pair_enabled", "pair": self.pair,
                            "enabled": True, "parked": False,
                        })
                    elif msg.get("type") == "disable_pair":
                        self._enabled = False
                        await self._broadcast({
                            "type": "pair_enabled", "pair": self.pair,
                            "enabled": False, "parked": self._parked,
                        })
                    elif msg.get("type") == "park_pair":
                        self._enabled = False
                        self._parked = True
                        prefs = load_pair_prefs()
                        parked = set(prefs.get("parked_pairs", []))
                        parked.add(self.pair)
                        prefs["parked_pairs"] = sorted(parked)
                        save_pair_prefs(prefs)
                        print(f"[APEX] {self.pair} PARKED (persistent disable)")
                        await self._broadcast({
                            "type": "pair_enabled", "pair": self.pair,
                            "enabled": False, "parked": True,
                        })
                    elif msg.get("type") == "unpark_pair":
                        self._parked = False
                        self._enabled = True
                        prefs = load_pair_prefs()
                        parked = set(prefs.get("parked_pairs", []))
                        parked.discard(self.pair)
                        prefs["parked_pairs"] = sorted(parked)
                        save_pair_prefs(prefs)
                        print(f"[APEX] {self.pair} UNPARKED (re-enabled)")
                        await self._broadcast({
                            "type": "pair_enabled", "pair": self.pair,
                            "enabled": True, "parked": False,
                        })
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)

    async def _broadcast(self, msg: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(msg)
        await asyncio.gather(*[c.send(data) for c in list(self._clients)],
                             return_exceptions=True)

    def _get_sibling_states(self) -> list[dict]:
        """Return summary of enabled sibling agents for cross-pair awareness."""
        states = []
        for s in self._sibling_agents:
            states.append({
                "pair": s.pair,
                "enabled": s._enabled,
                "has_position": s._position is not None,
                "engine_state": s._engine_state,
            })
        return states

    # ── Pair switching ──

    async def _switch_pair(self, new_pair: str) -> None:
        """Tear down current pair infrastructure and rebuild for new_pair."""
        if self._engine_state == "switching":
            return
        if new_pair == self.pair and self._engine_state != "idle":
            return
        if new_pair == self.pair and self._engine_state == "idle":
            self._engine_state = "running"
            await self._broadcast({
                "type": "engine_state", "state": self._engine_state, "pair": self.pair,
            })
            return
        self._engine_state = "switching"
        print(f"[APEX] Switching {self.pair} → {new_pair}")
        if self._position is not None:
            await self._exit_position("pair_switch")
            if self._position is not None:
                print(f"[APEX] WARNING: position on {self.pair} could not be closed — "
                      f"qty={self._position.qty} @ entry {self._position.entry_price}. "
                      f"Close manually on Kraken before switching.")
                self._engine_state = "running"
                self._candle_agg = CandleAggregator(self.pair, self._on_bar)
                task = asyncio.create_task(self._candle_agg.run())
                task.add_done_callback(self._task_error_cb)
                await self._broadcast({
                    "type": "engine_state", "state": "running", "pair": self.pair,
                })
                return
        self._candle_agg.stop()
        old_daily_pnl = self._executor._daily_pnl
        old_daily_loss = self._executor._daily_loss
        try:
            price_dec, lot_dec, ordermin, costmin = await asyncio.to_thread(
                _query_pair_precision, new_pair
            )
        except Exception as e:
            print(f"[APEX] Switch failed — cannot query {new_pair}: {e}")
            self._engine_state = "running"
            self._candle_agg = CandleAggregator(self.pair, self._on_bar)
            task = asyncio.create_task(self._candle_agg.run())
            task.add_done_callback(self._task_error_cb)
            await self._broadcast({
                "type": "engine_state", "state": self._engine_state, "pair": self.pair,
            })
            return
        self.pair = new_pair
        tag = new_pair.replace("/", "_").lower()
        self._session_path = f"hydra_meme_session_{tag}.json"
        self._journal_path = f"hydra_meme_journal_{tag}.json"
        self._profile = PROFILES.get(new_pair, DEFAULT_PROFILE)
        self._signal_engine = SignalEngine(self._profile)
        self._obi_poller = OBIPoller(new_pair)
        print(f"[APEX] {new_pair}: price_decimals={price_dec}  lot_decimals={lot_dec}  "
              f"ordermin={ordermin}  costmin=${costmin}")
        self._executor = MemeExecutor(new_pair, self._position_size, self._daily_cap,
                                      price_dec, lot_dec, ordermin, costmin)
        self._executor._daily_pnl = old_daily_pnl
        self._executor._daily_loss = old_daily_loss
        self._candle_agg = CandleAggregator(new_pair, self._on_bar)
        self._position = None
        self._sell_pending_reason = None
        self._sell_retry_count = 0
        self._bar_count = 0
        self._last_exit_bar_count = -REENTRY_COOLDOWN_BARS
        self._engine_state = "running"
        await self._seed_history()
        bars = self._signal_engine._bars
        if bars:
            await self._broadcast({
                "type": "candle_history",
                "bars": [{"ts": b.ts, "open": b.open, "high": b.high,
                           "low": b.low, "close": b.close, "volume": b.volume,
                           "vwap": b.vwap} for b in bars],
            })
        task = asyncio.create_task(self._candle_agg.run())
        task.add_done_callback(self._task_error_cb)
        prefs = load_pair_prefs()
        parked_set = set(prefs.get("parked_pairs", []))
        self._parked = new_pair in parked_set
        self._enabled = new_pair not in parked_set
        win_count = sum(1 for t in self._trade_log if t.net_pnl > 0)
        await self._broadcast({
            "type": "initial_state",
            "pair": self.pair,
            "engine_state": self._engine_state,
            "candle_interval": CANDLE_INTERVAL,
            "position": None,
            "trades": [],
            "session_pnl": self._executor._daily_pnl,
            "daily_loss": self._executor._daily_loss,
            "trade_count": len(self._trade_log),
            "win_rate": win_count / max(len(self._trade_log), 1),
            "enabled": self._enabled,
            "parked": self._parked,
        })
        print(f"[APEX] Switched to {new_pair} — hot, ready to trade")

    # ── Bar callback (from CandleAggregator, scheduled into event loop) ──

    def _on_bar(self, bar: CandleBar) -> None:
        """Called by CandleAggregator; schedules async work into the event loop."""
        task = asyncio.ensure_future(self._handle_bar(bar))
        task.add_done_callback(self._task_error_cb)

    @staticmethod
    def _task_error_cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[APEX] Task error: {exc}")

    async def _handle_bar(self, bar: CandleBar) -> None:
        if os.environ.get("HYDRA_APEX_DISABLED") == "1":
            return
        if self._engine_state in ("idle", "switching"):
            return
        self._signal_engine.add_bar(bar)
        self._bar_count += 1
        self._executor.maybe_reset_daily()
        # Broadcast bar so frontend chart updates on every close
        await self._broadcast({
            "type": "bar_update",
            "bar": {"ts": bar.ts, "open": bar.open, "high": bar.high,
                    "low": bar.low, "close": bar.close, "volume": bar.volume,
                    "vwap": bar.vwap},
        })
        self._engine_state = "running"
        # Broadcast signal state
        gates = self._signal_engine.evaluate_entry_gates(
            bar, self._obi_poller.obi, self._obi_poller.ask_wall_usd
        )
        gates["btc_risk_off"] = self._btc_monitor.is_risk_off
        gates["btc_rsi"] = round(self._btc_monitor.btc_rsi, 1)
        gates["btc_1h_chg"] = round(self._btc_monitor.btc_1h_change, 4)
        # Compute Half-Kelly size for this bar (0 when no entry signal)
        confidence = gates["confidence"]
        if confidence > 0 and len(self._trade_log) >= 5:
            wins = [t for t in self._trade_log if t.net_pnl > 0]
            losses = [t for t in self._trade_log if t.net_pnl < 0]
            wr = len(wins) / len(self._trade_log)
            avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
            avg_loss = abs(sum(t.net_pnl for t in losses) / len(losses)) if losses else 1.0
            payoff = avg_win / avg_loss if avg_loss > 0 else KELLY_DEFAULT_PAYOFF
            computed_size = half_kelly_size(wr, payoff, confidence)
        elif confidence > 0:
            computed_size = half_kelly_size(
                KELLY_DEFAULT_WIN_RATE, KELLY_DEFAULT_PAYOFF, confidence
            )
        else:
            computed_size = 0.0
        gates["kelly_size"] = round(computed_size, 2)
        gates["enabled"] = self._enabled
        await self._broadcast({"type": "signal_state", "gates": gates,
                                "pair": self.pair, "ts": bar.ts,
                                "siblings": self._get_sibling_states()})
        # Bar-close exit check + peak tracking
        if self._position is not None:
            self._position.candles_held += 1
            if bar.high > self._position.peak_price:
                self._position.peak_price = bar.high
            reason = self._signal_engine.evaluate_exit_bar(self._position, bar)
            if reason:
                await self._exit_position(reason)
                return
        # Entry check (only when enabled, no position, no pending sell, OBI fresh, BTC not dumping)
        btc_ok = not self._btc_monitor.is_risk_off
        loss_halted = self._bar_count < self._halt_until_bar
        if (self._enabled and self._position is None and not self._executor.is_halted()
                and not self._sell_pending_reason and not self._obi_poller.is_stale
                and btc_ok and not loss_halted
                and (self._bar_count - self._last_exit_bar_count) >= REENTRY_COOLDOWN_BARS):
            if gates["all_pass"] and computed_size >= self._executor.costmin:
                entry_mode = gates.get("entry_mode", "momentum")
                mid = self._obi_poller.mid_price or None
                pos = await asyncio.to_thread(
                    self._executor.place_buy, self._obi_poller.best_ask, mid,
                    entry_mode, computed_size,
                )
                if pos:
                    self._position = pos
                    await self._broadcast({"type": "order_placed",
                                           "side": "buy",
                                           "price": pos.entry_price,
                                           "qty": pos.qty,
                                           "entry_mode": entry_mode})
                    await asyncio.to_thread(save_session, SessionState(
                        pair=self.pair, engine_state=self._engine_state,
                        session_pnl=self._executor._daily_pnl,
                        daily_pnl=self._executor._daily_pnl,
                        trade_count=len(self._trade_log),
                        open_position={"entry_price": pos.entry_price, "qty": pos.qty,
                                       "notional_usd": pos.notional_usd, "entry_ts": pos.entry_ts,
                                       "order_id": pos.order_id},
                    ), self._session_path)

    async def _exit_position(self, reason: str) -> None:
        """Exit current position. Lock prevents double-exit from concurrent OBI/bar tasks."""
        async with self._exit_lock:
            if self._position is None:
                return
            result = await asyncio.to_thread(
                self._executor.place_sell,
                self._position, self._obi_poller.best_bid, reason,
                self._obi_poller.mid_price or None,
            )
            if result is None:
                self._sell_retry_count += 1
                if self._sell_retry_count >= SELL_MAX_RETRIES:
                    print(f"[APEX] SELL FAILED after {SELL_MAX_RETRIES} retries — "
                          f"abandoning auto-sell for {self.pair} "
                          f"(qty={self._position.qty}, entry={self._position.entry_price})")
                    print(f"[APEX] WARNING: position remains open on exchange — close manually")
                    self._sell_pending_reason = None
                    self._sell_retry_count = 0
                    await self._broadcast({"type": "sell_abandoned",
                                           "reason": reason, "pair": self.pair,
                                           "retries": SELL_MAX_RETRIES})
                else:
                    self._sell_pending_reason = reason
                    await self._broadcast({"type": "sell_failed", "reason": reason,
                                           "pair": self.pair,
                                           "retry": self._sell_retry_count})
                return
            record: TradeRecord = result["record"]
            self._trade_log.append(record)
            await asyncio.to_thread(append_journal, record, self._journal_path)
            self._position = None
            self._sell_pending_reason = None
            self._sell_retry_count = 0
            self._last_exit_bar_count = self._bar_count
            if record.net_pnl < 0:
                self._consec_stops += 1
                if self._consec_stops >= CONSEC_LOSS_HALT_THRESHOLD:
                    self._halt_until_bar = self._bar_count + CONSEC_LOSS_HALT_BARS
                    print(f"[APEX] {self.pair}: {self._consec_stops} consecutive losses "
                          f"— halting entries for {CONSEC_LOSS_HALT_BARS} bars")
            else:
                self._consec_stops = 0
        await self._broadcast({"type": "trade_closed",
                               "net_pnl": record.net_pnl,
                               "exit_reason": reason,
                               "exit_ts": record.exit_ts,
                               "entry_price": record.entry_price,
                               "exit_price": record.exit_price,
                               "hold_candles": record.hold_candles})
        if self._executor.is_halted():
            self._engine_state = "halted"
            await self._broadcast({"type": "engine_halted",
                                   "reason": "daily_cap",
                                   "daily_pnl": self._executor._daily_pnl})
        win_count = sum(1 for t in self._trade_log if t.net_pnl > 0)
        await self._broadcast({
            "type": "session_stats",
            "session_pnl": self._executor._daily_pnl,
            "daily_loss": self._executor._daily_loss,
            "trade_count": len(self._trade_log),
            "win_rate": win_count / max(len(self._trade_log), 1),
            "daily_cap_remaining": self._executor.daily_cap + self._executor._daily_loss,
        })
        state = SessionState(
            pair=self.pair,
            engine_state=self._engine_state,
            session_pnl=self._executor._daily_pnl,
            daily_pnl=self._executor._daily_pnl,
            trade_count=len(self._trade_log),
        )
        await asyncio.to_thread(save_session, state, self._session_path)

    # ── 10-second OBI loop ──

    async def _obi_loop(self) -> None:
        while True:
            try:
                if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                    self._engine_state = "halted"
                    await self._broadcast({"type": "engine_halted", "reason": "kill_switch"})
                    await asyncio.sleep(OBI_POLL_INTERVAL)
                    continue
                await asyncio.to_thread(self._obi_poller.poll)
                await asyncio.to_thread(self._btc_monitor.poll)
                mid = self._obi_poller.mid_price
                # Retry failed sell with fresh bid data
                if self._sell_pending_reason and self._position is not None and mid > 0:
                    await self._exit_position(self._sell_pending_reason)
                if self._position is not None and self._engine_state == "running" and mid > 0:
                    if mid > self._position.peak_price:
                        self._position.peak_price = mid
                    reason = self._signal_engine.evaluate_exit_intracandle(
                        self._position, mid, self._obi_poller.obi
                    )
                    if reason:
                        await self._exit_position(reason)
                bid = self._obi_poller.best_bid
                ask = self._obi_poller.best_ask
                spread_bps = ((ask - bid) / mid * 10_000) if mid > 0 else 0.0
                if self._position is not None:
                    pos = self._position
                    await self._broadcast({
                        "type": "position_update",
                        "price": mid,
                        "obi": self._obi_poller.obi,
                        "spread_bps": round(spread_bps, 1),
                        "entry": {
                            "entry_price": pos.entry_price,
                            "qty": pos.qty,
                            "candles_held": pos.candles_held,
                            "notional_usd": pos.notional_usd,
                            "entry_ts": pos.entry_ts,
                            "entry_mode": pos.entry_mode,
                            "peak_price": pos.peak_price,
                        },
                        "unrealised_pnl": (mid - pos.entry_price) * pos.qty if mid > 0 else 0.0,
                    })
                else:
                    await self._broadcast({
                        "type": "ticker",
                        "price": mid,
                        "obi": self._obi_poller.obi,
                        "spread_bps": round(spread_bps, 1),
                        "btc_risk_off": self._btc_monitor.is_risk_off,
                        "btc_rsi": round(self._btc_monitor.btc_rsi, 1),
                        "btc_1h_chg": round(self._btc_monitor.btc_1h_change, 4),
                    })
            except Exception as e:
                print(f"[APEX] OBI loop error: {e}")
            await asyncio.sleep(OBI_POLL_INTERVAL)

    async def _run_competition_scan(self) -> None:
        """Single competition scan pass: fetch ticker for seed-list tokens only."""
        seed_set = set(COMPETITION_SEED_PAIRS)
        tokens = [t for t in self._detector.get_all_tokens() if t["pair"] in seed_set]
        await self._broadcast({"type": "scan_started", "token_count": len(tokens)})
        last_call = 0.0
        for token in tokens:
            elapsed = time.time() - last_call
            if elapsed < KRAKEN_REST_FLOOR:
                await asyncio.sleep(KRAKEN_REST_FLOOR - elapsed)
            pair_nodash = token["pair"].replace("/", "")
            result = await asyncio.to_thread(_kraken_cli, ["ticker", pair_nodash])
            last_call = time.time()
            if "error" in result:
                continue
            # Kraken ticker key may be normalized — fall back to first key.
            ticker_data = (result.get(pair_nodash)
                           or result.get(token["pair"])
                           or (next(iter(result.values())) if result else {}))
            if not isinstance(ticker_data, dict):
                continue
            # Kraken ticker: v[1] = 24h volume
            vol_str = ticker_data.get("v", [None, None])[1]
            if not vol_str:
                continue
            volume = float(vol_str)
            first_scan = self._detector._get_baseline(token["pair"]) is None
            if first_scan:
                self._detector._set_baseline(token["pair"], volume)
                baseline = volume
                ratio = 1.0
                is_anomaly = False
            else:
                baseline = self._detector._get_baseline(token["pair"])
                ratio = volume / baseline if baseline else 0.0
                is_anomaly = self._detector._is_anomaly(token["pair"], volume)
                self._detector._update_baseline(token["pair"], volume)
            # Store live data on token dict for watchlist_update
            with self._detector._lock:
                t = self._detector._find_token(token["pair"])
                if t is not None:
                    t["current_volume"] = volume
                    t["anomaly_ratio"] = ratio
            comp_type = self._detector.infer_competition_type(token["pair"])
            token_obj = self._detector._find_token(token["pair"]) or {}
            # Broadcast individual token immediately — don't make frontend wait 36 s
            await self._broadcast({
                "type": "token_update",
                "pair": token["pair"],
                "current_volume": volume,
                "baseline_volume_7d": baseline,
                "anomaly_ratio": ratio,
                "competition_type": comp_type,
                "competition_type_confirmed": token_obj.get("competition_type_confirmed", False),
            })
            if (is_anomaly
                    and not self._detector._is_suppressed(token["pair"])):
                await self._broadcast({
                    "type": "competition_alert",
                    "pair": token["pair"],
                    "volume": volume,
                    "baseline": baseline,
                    "ratio": ratio,
                    "competition_type": comp_type,
                    "competition_type_confirmed": token_obj.get("competition_type_confirmed", False),
                })
        # Final authoritative snapshot after full scan
        await self._broadcast({
            "type": "watchlist_update",
            "tokens": self._detector.get_all_tokens(),
        })

    # ── 15-minute competition scan loop ──

    async def _competition_loop(self) -> None:
        await self._run_competition_scan()
        while True:
            await asyncio.sleep(COMPETITION_SCAN_INTERVAL)
            if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                continue
            await self._run_competition_scan()

    # ── Test fire ──

    async def _test_fire(self) -> bool:
        """Execute one BUY→SELL cycle to verify the full pipeline.

        Queries pair minimums from Kraken to ensure the order clears both
        ordermin (token qty) and costmin (USD notional).
        """
        ex = self._executor
        ordermin = ex.ordermin
        costmin = ex.costmin
        pfmt = f"{{:.{ex.price_decimals}f}}"
        qfmt = f"{{:.{ex.lot_decimals}f}}"
        print(f"[APEX] TEST-FIRE: starting round-trip on {self.pair}")
        print(f"[APEX] TEST-FIRE: ordermin={ordermin}  costmin=${costmin}  price_dec={ex.price_decimals}  lot_dec={ex.lot_decimals}")
        print("[APEX] TEST-FIRE: polling orderbook for fresh bid/ask...")
        for attempt in range(6):
            await asyncio.to_thread(self._obi_poller.poll)
            if self._obi_poller.best_ask > 0:
                break
            print(f"[APEX] TEST-FIRE: no book data yet (attempt {attempt + 1}/6)")
            await asyncio.sleep(3)
        ask = self._obi_poller.best_ask
        bid = self._obi_poller.best_bid
        mid = self._obi_poller.mid_price
        obi = self._obi_poller.obi
        if ask <= 0 or bid <= 0:
            print("[APEX] TEST-FIRE: FAILED — could not get orderbook data")
            return False
        print(f"[APEX] TEST-FIRE: ask={ask:.8f}  bid={bid:.8f}  mid={mid:.8f}  OBI={obi:.4f}")
        # Compute qty that clears both minimums with 20% headroom
        qty_from_min = ordermin * 1.2
        qty_from_cost = (costmin * 1.2) / ask
        qty = max(qty_from_min, qty_from_cost)
        test_notional = qty * ask
        limit_buy = ask * (1 + TAKER_SLIPPAGE_BPS / 10_000)
        print(f"[APEX] TEST-FIRE: placing BUY  qty={qfmt.format(qty)}  limit={pfmt.format(limit_buy)}  (~${test_notional:.2f})")
        buy_result = await asyncio.to_thread(
            _kraken_cli,
            ["order", "buy", self.pair, qfmt.format(qty),
             "--type", "limit", "--price", pfmt.format(limit_buy),
             "--yes"],
        )
        if "error" in buy_result:
            print(f"[APEX] TEST-FIRE: BUY FAILED — {buy_result}")
            return False
        txid = buy_result.get("txid", "?")
        print(f"[APEX] TEST-FIRE: BUY OK — txid={txid}")
        await self._broadcast({"type": "order_placed", "side": "buy",
                                "price": limit_buy, "qty": qty,
                                "test_fire": True})
        # Wait for fill
        await asyncio.sleep(3)
        # SELL
        await asyncio.to_thread(self._obi_poller.poll)
        bid = self._obi_poller.best_bid
        limit_sell = bid * (1 - TAKER_SLIPPAGE_BPS / 10_000)
        print(f"[APEX] TEST-FIRE: placing SELL qty={qfmt.format(qty)}  limit={pfmt.format(limit_sell)}")
        sell_result = await asyncio.to_thread(
            _kraken_cli,
            ["order", "sell", self.pair, qfmt.format(qty),
             "--type", "limit", "--price", pfmt.format(limit_sell),
             "--yes"],
        )
        if "error" in sell_result:
            print(f"[APEX] TEST-FIRE: SELL FAILED — {sell_result}")
            print("[APEX] TEST-FIRE: WARNING — position still open on exchange — close manually")
            return False
        gross = (limit_sell - limit_buy) * qty
        fees = test_notional * TAKER_FEE_RATE * 2
        net = gross - fees
        print(f"[APEX] TEST-FIRE: SELL OK — txid={sell_result.get('txid', '?')}")
        print(f"[APEX] TEST-FIRE: gross={gross:+.6f}  fees={fees:.6f}  net={net:+.6f}")
        print("[APEX] TEST-FIRE: PASS — full pipeline verified — continuing normal operation")
        record = TradeRecord(
            entry_ts=int(time.time()) - 3,
            exit_ts=int(time.time()),
            entry_price=limit_buy,
            exit_price=limit_sell,
            qty=qty,
            gross_pnl=gross,
            fees_usd=fees,
            net_pnl=net,
            exit_reason="test_fire",
            hold_candles=0,
        )
        self._trade_log.append(record)
        await asyncio.to_thread(append_journal, record, self._journal_path)
        await self._broadcast({"type": "trade_closed", "net_pnl": net,
                                "exit_reason": "test_fire",
                                "exit_ts": record.exit_ts,
                                "entry_price": limit_buy,
                                "exit_price": limit_sell,
                                "hold_candles": 0,
                                "test_fire": True})
        return True

    # ── Main run ──

    async def run(self, test_fire: bool = False) -> None:
        if os.environ.get("HYDRA_APEX_DISABLED") == "1":
            print("[APEX] Kill switch HYDRA_APEX_DISABLED=1 — not starting")
            return
        await self._seed_history()
        server = None
        ws_port = None
        port_range = ([self._ws_port_fixed] if self._ws_port_fixed is not None
                      else range(WS_PORT_BASE, WS_PORT_BASE + WS_PORT_RANGE))
        for port in port_range:
            try:
                server = await websockets.serve(
                    self._ws_handler, "127.0.0.1", port,
                )
                ws_port = port
                break
            except OSError:
                continue
        if server is None:
            print(f"[APEX] FATAL: could not bind WS on ports {WS_PORT_BASE}-{WS_PORT_BASE + WS_PORT_RANGE - 1}")
            return
        print(f"[APEX] WebSocket server on ws://127.0.0.1:{ws_port}")
        if test_fire:
            await self._test_fire()
        print(f"[APEX] Trading {self.pair} | State: {self._engine_state}")
        try:
            results = await asyncio.gather(
                self._candle_agg.run(),
                self._obi_loop(),
                self._competition_loop(),
                return_exceptions=True,
            )
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    task_names = ["candle_agg", "obi_loop", "competition_loop"]
                    print(f"[APEX] Task {task_names[i]} crashed: {r}")
        except asyncio.CancelledError:
            print("[APEX] Shutting down...")
        finally:
            self._candle_agg.stop()
            if self._position is not None:
                print(f"[APEX] Shutdown with open position — attempting exit sell")
                try:
                    await asyncio.to_thread(self._obi_poller.poll)
                    if self._obi_poller.best_bid > 0:
                        await self._exit_position("shutdown")
                except Exception as e:
                    print(f"[APEX] Shutdown sell failed: {e}")
                if self._position is not None:
                    if self._position.order_id:
                        try:
                            await asyncio.to_thread(_cancel_order, self._position.order_id)
                        except Exception:
                            pass
                    print(f"[APEX] WARNING: open position remains — "
                          f"{self._position.qty} {self.pair} @ entry "
                          f"{self._position.entry_price} — close manually on Kraken")
            open_pos = None
            if self._position is not None:
                p = self._position
                open_pos = {"entry_price": p.entry_price, "qty": p.qty,
                            "notional_usd": p.notional_usd, "entry_ts": p.entry_ts,
                            "order_id": p.order_id}
            save_session(SessionState(
                pair=self.pair, engine_state="idle",
                session_pnl=self._executor._daily_pnl,
                daily_pnl=self._executor._daily_pnl,
                trade_count=len(self._trade_log),
                open_position=open_pos,
            ), self._session_path)
            server.close()
            await server.wait_closed()
            print("[APEX] Shutdown complete")


# ─── Entry Point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="APEX Meme Engine")
    p.add_argument("--pair", help="Single trading pair e.g. NIGHT/USD")
    p.add_argument("--pairs", help="Comma-separated pairs e.g. NIGHT/USD,AAVE/USD (multi-pair)")
    p.add_argument("--position-size", type=float, default=300.0)
    p.add_argument("--daily-cap", type=float, default=30.0)
    p.add_argument("--session-path", default="hydra_meme_session.json")
    p.add_argument("--journal-path", default="hydra_meme_journal.json")
    p.add_argument("--watchlist-path", default="hydra_meme_watchlist.json")
    p.add_argument("--test-fire", action="store_true",
                   help="Execute one $5 BUY→SELL cycle on startup to verify pipeline")
    args = p.parse_args()
    if not args.pair and not args.pairs:
        p.error("--pair or --pairs required")
    return args


def main() -> None:
    args = _parse_args()
    # Write PID file for clean restarts
    pid_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_meme.pid")
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(pid_path) and os.remove(pid_path))

    if not os.environ.get("KRAKEN_API_KEY") or not os.environ.get("KRAKEN_API_SECRET"):
        print("[APEX] WARNING: KRAKEN_API_KEY/SECRET not found — orders will fail")

    if args.pairs:
        pairs = [p.strip() for p in args.pairs.split(",")]
    else:
        pairs = [args.pair]

    shared_btc = BtcRegimeMonitor()

    if len(pairs) == 1:
        agent = MemeAgent(
            pair=pairs[0],
            position_size=args.position_size,
            daily_cap=args.daily_cap,
            session_path=args.session_path,
            journal_path=args.journal_path,
            watchlist_path=args.watchlist_path,
            btc_monitor=shared_btc,
        )
        asyncio.run(agent.run(test_fire=args.test_fire))
    else:
        async def run_all():
            agents = []
            for i, pair in enumerate(pairs):
                tag = pair.replace("/", "_").lower()
                agent = MemeAgent(
                    pair=pair,
                    position_size=args.position_size,
                    daily_cap=args.daily_cap,
                    session_path=f"hydra_meme_session_{tag}.json",
                    journal_path=f"hydra_meme_journal_{tag}.json",
                    watchlist_path=args.watchlist_path,
                    btc_monitor=shared_btc,
                    ws_port=WS_PORT_BASE + i,
                )
                agents.append(agent)
            # Wire cross-pair awareness: each agent knows its siblings
            for a in agents:
                a._sibling_agents = [s for s in agents if s is not a]
            print(f"[APEX] Multi-pair mode: {len(agents)} agents — "
                  f"{', '.join(pairs)}")
            await asyncio.gather(
                *[a.run(test_fire=args.test_fire) for a in agents]
            )
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
