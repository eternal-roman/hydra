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
from dataclasses import dataclass, field, asdict
from typing import Optional
import websockets


# ─── Constants ────────────────────────────────────────────────────────────────

WS_PORT = 8766
CANDLE_INTERVAL = 5          # minutes
WARMUP_BARS = 15
CANDLE_BUFFER_SIZE = 20
OBI_POLL_INTERVAL = 10       # seconds
COMPETITION_SCAN_INTERVAL = 900  # 15 minutes
KRAKEN_REST_FLOOR = 2.0      # seconds between CLI calls
RSI_PERIOD = 9
VOL_EMA_PERIOD = 10
OBI_ENTRY_THRESHOLD = 0.20
OBI_BOOK_FADE = -0.20
RSI_ENTRY_LOW = 45
RSI_ENTRY_HIGH = 78
RSI_EXHAUST = 82
VOLUME_SPIKE_MULTIPLIER = 1.8
VOLUME_DEATH_MULTIPLIER = 0.4
ASK_WALL_USD_LIMIT = 500.0
PROFIT_TARGET_PCT = 0.025    # 2.5%
HARD_STOP_PCT = -0.013       # -1.3%
TIME_STOP_CANDLES = 3
OBI_LEVELS = 5
TAKER_SLIPPAGE_BPS = 5       # 0.05% — limit at ask+0.05% for BUY
SLIPPAGE_CAP_BPS = 10        # 0.10% — reject if book moves more

COMPETITION_ANOMALY_RATIO = 5.0
COMPETITION_EMA_ALPHA = 1 / 7

COMPETITION_SEED_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOT/USD", "LINK/USD", "AVAX/USD", "ATOM/USD", "NEAR/USD",
    "FIL/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
    "TIA/USD", "SEI/USD", "PYTH/USD", "WIF/USD", "POPCAT/USD",
    "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
    "MATIC/USD", "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
]


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
    """VWAP across all provided bars. Returns 0.0 for empty list."""
    total_pv = sum(b.close * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    return total_pv / total_v if total_v > 0.0 else 0.0
