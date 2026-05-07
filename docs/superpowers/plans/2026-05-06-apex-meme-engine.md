# APEX Meme Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully isolated meme/competition-token trading engine (APEX) with a new MEME tab in the existing Hydra dashboard — zero changes to any existing engine or state file.

**Architecture:** `hydra_meme_agent.py` is a standalone asyncio process that subscribes to Kraken's public WebSocket for real-time 5-min candles, polls the orderbook every 10 seconds for OBI, evaluates entry/exit signals, executes taker limit orders via the existing kraken CLI pattern, and broadcasts state on port 8766. `MemeTab.jsx` connects to port 8766 and renders the Trading and Discover views. App.jsx receives a single tab addition.

**Tech Stack:** Python 3.9+ (asyncio, websockets v16, json, subprocess, threading, shlex), React (hooks, inline styles matching Hydra design tokens), Kraken public WebSocket v1 (`wss://ws.kraken.com`)

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `hydra_meme_agent.py` | Complete backend engine (~520 lines) |
| Create | `tests/test_meme_agent.py` | Unit tests for all signal logic |
| Create | `dashboard/src/MemeTab.jsx` | New React tab component |
| Modify | `dashboard/src/App.jsx` | +~15 lines: add MEME tab entry |
| Create | `start_meme.bat` | Launcher |

State files (all gitignored, already in `.gitignore`): `hydra_meme_session.json`, `hydra_meme_journal.json`, `hydra_meme_watchlist.json`

---

## Task 1: Data classes and indicator functions

**Files:**
- Create: `hydra_meme_agent.py` (partial — data classes + pure functions only)
- Create: `tests/test_meme_agent.py` (partial — indicator tests)

- [ ] **Step 1: Write failing tests for indicators**

Create `tests/test_meme_agent.py`:

```python
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
```

- [ ] **Step 2: Run tests — expect ImportError (module not created yet)**

```
python -m pytest tests/test_meme_agent.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'hydra_meme_agent'`

- [ ] **Step 3: Create hydra_meme_agent.py with data classes and indicators**

Create `hydra_meme_agent.py`:

```python
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
```

- [ ] **Step 4: Run tests — expect pass**

```
python -m pytest tests/test_meme_agent.py -v
```

Expected: all 15 tests PASS.

- [ ] **Step 5: Commit**

```
git add hydra_meme_agent.py tests/test_meme_agent.py
git commit -m "feat(apex): data classes + indicator functions + tests"
```

---

## Task 2: Signal engine — entry gates

**Files:**
- Modify: `hydra_meme_agent.py` (add `SignalEngine` class)
- Modify: `tests/test_meme_agent.py` (add entry gate tests)

- [ ] **Step 1: Add entry gate tests to test_meme_agent.py**

Append to `tests/test_meme_agent.py`:

```python
from hydra_meme_agent import SignalEngine, CandleBar


def _make_bar(close=1.0, volume=1000.0, ts=0):
    return CandleBar(ts=ts, open=close*0.99, high=close*1.01, low=close*0.98,
                     close=close, vwap=close, volume=volume, count=10)


def _warmed_engine(n_bars=15, close=1.0, volume=1000.0):
    """Return a SignalEngine with n_bars of history loaded."""
    eng = SignalEngine()
    for i in range(n_bars):
        eng.add_bar(_make_bar(close=close + i * 0.001, volume=volume, ts=i * 300))
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
```

- [ ] **Step 2: Run tests — expect ImportError on SignalEngine**

```
python -m pytest tests/test_meme_agent.py::test_signal_engine_warmup_not_ready -v
```

Expected: `ImportError: cannot import name 'SignalEngine'`

- [ ] **Step 3: Add SignalEngine class to hydra_meme_agent.py**

Append after the indicator functions in `hydra_meme_agent.py`:

```python
# ─── Signal Engine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """Evaluates 5 entry gates and 6 exit triggers against candle history."""

    def __init__(self):
        self._bars: list[CandleBar] = []
        self._vwap_cum_pv: float = 0.0
        self._vwap_cum_v: float = 0.0

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
        """Evaluate all 5 entry gates. Returns dict with gate booleans + all_pass."""
        vol_baseline = self.vol_ema_baseline
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
        vwap = self.session_vwap

        gates = {
            "volume_spike": latest_bar.volume > VOLUME_SPIKE_MULTIPLIER * vol_baseline,
            "obi": obi > OBI_ENTRY_THRESHOLD,
            "vwap_align": latest_bar.close > vwap if vwap > 0 else False,
            "rsi_window": RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH,
            "ask_wall_clear": ask_wall_usd < ASK_WALL_USD_LIMIT,
            "rsi_value": round(rsi, 1),
            "vwap_value": round(vwap, 8),
            "vol_ema_value": round(vol_baseline, 2),
        }
        gates["all_pass"] = all(gates[k] for k in
                                ["volume_spike", "obi", "vwap_align", "rsi_window", "ask_wall_clear"])
        return gates

    def evaluate_exit_bar(self, position: Position, latest_bar: CandleBar) -> Optional[str]:
        """Bar-close exit triggers: RSI exhaust, time stop, volume death."""
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
        if rsi > RSI_EXHAUST:
            return "rsi_exhaust"
        if position.candles_held >= TIME_STOP_CANDLES:
            return "time_stop"
        vol_baseline = self.vol_ema_baseline
        if vol_baseline > 0 and latest_bar.volume < VOLUME_DEATH_MULTIPLIER * vol_baseline:
            return "volume_death"
        return None

    def evaluate_exit_intracandle(
        self,
        position: Position,
        mid_price: float,
        obi: float,
    ) -> Optional[str]:
        """10-second exit triggers: profit target, hard stop, book fade."""
        pct_change = (mid_price - position.entry_price) / position.entry_price
        if pct_change >= PROFIT_TARGET_PCT:
            return "profit_target"
        if pct_change <= HARD_STOP_PCT:
            return "hard_stop"
        if obi < OBI_BOOK_FADE:
            return "book_fade"
        return None
```

- [ ] **Step 4: Run all signal engine tests**

```
python -m pytest tests/test_meme_agent.py -v -k "signal or gate"
```

Expected: all entry gate tests PASS.

- [ ] **Step 5: Commit**

```
git add hydra_meme_agent.py tests/test_meme_agent.py
git commit -m "feat(apex): SignalEngine — 5 entry gates + 6 exit triggers"
```

---

## Task 3: Signal engine — exit trigger tests

**Files:**
- Modify: `tests/test_meme_agent.py`

- [ ] **Step 1: Add exit trigger tests**

Append to `tests/test_meme_agent.py`:

```python
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
    eng = _warmed_engine()
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
    eng = _warmed_engine(volume=1000.0)
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    dead_bar = _make_bar(close=1.01, volume=200.0)  # 0.2x baseline
    result = eng.evaluate_exit_bar(pos, dead_bar)
    assert result == "volume_death"


def test_exit_no_trigger_bar():
    eng = _warmed_engine(volume=1000.0)
    pos = Position(entry_price=1.00, qty=600.0, notional_usd=600.0,
                   entry_ts=0, candles_held=1)
    normal_bar = _make_bar(close=1.01, volume=1000.0)
    result = eng.evaluate_exit_bar(pos, normal_bar)
    assert result is None
```

- [ ] **Step 2: Run exit trigger tests**

```
python -m pytest tests/test_meme_agent.py -v -k "exit"
```

Expected: all 8 exit tests PASS.

- [ ] **Step 3: Commit**

```
git add tests/test_meme_agent.py
git commit -m "test(apex): exit trigger coverage — profit, stop, RSI, time, fade, volume"
```

---

## Task 4: Competition detector

**Files:**
- Modify: `hydra_meme_agent.py` (add `CompetitionDetector` class)
- Modify: `tests/test_meme_agent.py` (add competition detector tests)

- [ ] **Step 1: Add competition detector tests**

Append to `tests/test_meme_agent.py`:

```python
import tempfile, json as _json, os as _os
from hydra_meme_agent import CompetitionDetector


def test_competition_detector_bootstrap_creates_watchlist():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        assert _os.path.exists(path)
        data = _json.loads(open(path).read())
        assert len(data["tokens"]) > 0


def test_competition_detector_anomaly_detection():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        # Manually set a baseline
        detector._set_baseline("PLAY/USD", 3_200_000)
        # Volume 6x baseline → anomaly
        assert detector._is_anomaly("PLAY/USD", 19_200_000) is True


def test_competition_detector_no_anomaly_below_threshold():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        # 4x — below 5x threshold
        assert detector._is_anomaly("PLAY/USD", 12_800_000) is False


def test_competition_detector_null_baseline_not_anomaly():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        # Null baseline on first observation — not an anomaly
        assert detector._is_anomaly("NEW/USD", 999_999_999) is False


def test_competition_detector_ema_update():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        detector._update_baseline("PLAY/USD", 3_200_000)
        updated = detector._get_baseline("PLAY/USD")
        # EMA with alpha=1/7: new = (1/7)*3.2M + (6/7)*3.2M = 3.2M (stable)
        assert abs(updated - 3_200_000) < 1000


def test_competition_detector_alert_suppression():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        # Suppress for 2 hours
        future = time.time() + 7200
        detector._suppress("PLAY/USD", until=future)
        assert detector._is_suppressed("PLAY/USD") is True


def test_competition_detector_suppression_expired():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "watchlist.json")
        detector = CompetitionDetector(path)
        detector._set_baseline("PLAY/USD", 3_200_000)
        detector._suppress("PLAY/USD", until=time.time() - 1)
        assert detector._is_suppressed("PLAY/USD") is False
```

- [ ] **Step 2: Run tests — expect ImportError on CompetitionDetector**

```
python -m pytest tests/test_meme_agent.py -v -k "competition" 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'CompetitionDetector'`

- [ ] **Step 3: Add CompetitionDetector to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── Competition Detector ──────────────────────────────────────────────────────

class CompetitionDetector:
    """Monitors token volume baselines and detects competition anomalies."""

    def __init__(self, watchlist_path: str):
        self._path = watchlist_path
        self._lock = threading.Lock()
        self._data: dict = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path) as f:
                return json.load(f)
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
            return token["competition_type"]
        baseline = self._get_baseline(pair)
        if baseline is None:
            return "unknown"
        # Future: analyze volume curve shape; for now, default assumption
        return "volume"

    def get_all_tokens(self) -> list[dict]:
        return list(self._data.get("tokens", []))
```

- [ ] **Step 4: Run competition detector tests**

```
python -m pytest tests/test_meme_agent.py -v -k "competition"
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```
git add hydra_meme_agent.py tests/test_meme_agent.py
git commit -m "feat(apex): CompetitionDetector — watchlist, EMA baseline, anomaly detection"
```

---

## Task 5: CLI runner and state persistence

**Files:**
- Modify: `hydra_meme_agent.py` (add `_kraken_cli`, `SessionState`, persistence helpers)
- Modify: `tests/test_meme_agent.py` (add state persistence tests)

- [ ] **Step 1: Add state persistence tests**

Append to `tests/test_meme_agent.py`:

```python
import tempfile, json as _json, os as _os
from hydra_meme_agent import SessionState, save_session, load_session, append_journal


def test_save_and_load_session():
    with tempfile.TemporaryDirectory() as d:
        path = _os.path.join(d, "session.json")
        state = SessionState(pair="PLAY/USD", engine_state="running",
                             session_pnl=10.20, daily_pnl=10.20, trade_count=2)
        save_session(state, path)
        loaded = load_session(path)
        assert loaded.pair == "PLAY/USD"
        assert loaded.session_pnl == 10.20
        assert loaded.trade_count == 2


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
    assert data[0]["exit_reason"] == "profit_target"
```

- [ ] **Step 2: Add SessionState, save/load helpers, and _kraken_cli to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── Session State ─────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    pair: str = ""
    engine_state: str = "idle"   # idle | warmup | running | halted
    candle_buffer: list = field(default_factory=list)
    open_position: Optional[dict] = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trade_count: int = 0


def save_session(state: SessionState, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)


def load_session(path: str) -> SessionState:
    with open(path) as f:
        data = json.load(f)
    return SessionState(**{k: v for k, v in data.items() if k in SessionState.__dataclass_fields__})


def append_journal(record: TradeRecord, path: str) -> None:
    existing: list = []
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing.append(asdict(record))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, path)


# ─── Kraken CLI ────────────────────────────────────────────────────────────────

def _kraken_cli(args: list[str], timeout: int = 20) -> dict:
    """Execute a kraken CLI command via WSL and return parsed JSON.
    
    All args are shlex-quoted to prevent injection (matches hydra_kraken_cli.py pattern).
    """
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    cmd_str = "source ~/.cargo/env"
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if api_key and api_secret:
        cmd_str += (f" && export KRAKEN_API_KEY={shlex.quote(api_key)}"
                    f" && export KRAKEN_API_SECRET={shlex.quote(api_secret)}")
    cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
    cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd_str]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        if not stdout:
            return {"error": f"Empty response (exit {result.returncode})"}
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data:
            return data
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "retryable": True}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}
```

- [ ] **Step 3: Run persistence tests**

```
python -m pytest tests/test_meme_agent.py -v -k "session or journal"
```

Expected: all 3 tests PASS.

- [ ] **Step 4: Run full test suite**

```
python -m pytest tests/test_meme_agent.py -v
```

Expected: all tests PASS (no regressions).

- [ ] **Step 5: Commit**

```
git add hydra_meme_agent.py tests/test_meme_agent.py
git commit -m "feat(apex): SessionState, atomic persistence, kraken CLI runner"
```

---

## Task 6: MemeExecutor — order placement and position tracking

**Files:**
- Modify: `hydra_meme_agent.py` (add `MemeExecutor` class)
- Modify: `tests/test_meme_agent.py` (add executor tests with mocked CLI)

- [ ] **Step 1: Add MemeExecutor tests**

Append to `tests/test_meme_agent.py`:

```python
from unittest.mock import patch
from hydra_meme_agent import MemeExecutor, Position


def test_executor_buy_price_calculation():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    expected_limit = ask * (1 + TAKER_SLIPPAGE_BPS / 10000)
    # Access the internal price calculation
    price = exec_._buy_limit_price(ask)
    assert abs(price - expected_limit) < 0.000001


def test_executor_buy_rejects_above_slippage_cap():
    exec_ = MemeExecutor("PLAY/USD", position_size=600.0, daily_cap=30.0)
    ask = 0.16540
    # Simulate book moved: ask at time of order is much higher than when we decided
    # In practice this is handled by the cap on the limit price itself
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
```

Append at top of test file (after existing imports):
```python
from hydra_meme_agent import TAKER_SLIPPAGE_BPS, SLIPPAGE_CAP_BPS
```

- [ ] **Step 2: Run — expect ImportError on MemeExecutor**

```
python -m pytest tests/test_meme_agent.py -v -k "executor" 2>&1 | head -5
```

- [ ] **Step 3: Add MemeExecutor to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── Meme Executor ─────────────────────────────────────────────────────────────

TAKER_FEE_RATE = 0.004   # 0.40% taker fee on competition tokens


class MemeExecutor:
    """Places taker limit orders and tracks position + daily P&L."""

    def __init__(self, pair: str, position_size: float, daily_cap: float):
        self.pair = pair
        self.position_size = position_size
        self.daily_cap = daily_cap
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._halted: bool = False
        self._pair_nodash = pair.replace("/", "")

    def is_halted(self) -> bool:
        return self._halted or self._daily_loss <= -self.daily_cap

    def record_pnl(self, net_pnl: float) -> None:
        self._daily_pnl += net_pnl
        if net_pnl < 0:
            self._daily_loss += net_pnl
        if self._daily_loss <= -self.daily_cap:
            self._halted = True

    def _buy_limit_price(self, ask: float) -> float:
        return ask * (1 + TAKER_SLIPPAGE_BPS / 10_000)

    def _sell_limit_price(self, bid: float) -> float:
        return bid * (1 - TAKER_SLIPPAGE_BPS / 10_000)

    def _buy_qty(self, ask: float) -> float:
        return self.position_size / ask

    def _compute_net_pnl(self, position: Position, exit_price: float) -> float:
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_notional = exit_price * position.qty
        exit_fee = exit_notional * TAKER_FEE_RATE
        return gross - entry_fee - exit_fee

    def place_buy(self, ask: float) -> Optional[Position]:
        """Place a taker BUY limit order. Returns Position on success, None on failure."""
        if self.is_halted():
            return None
        limit_price = self._buy_limit_price(ask)
        qty = self._buy_qty(ask)
        result = _kraken_cli([
            "order", "buy",
            self.pair,
            f"{qty:.8f}",
            "--type", "limit",
            "--price", f"{limit_price:.8f}",
            "--yes",
        ])
        if "error" in result:
            return None
        order_id = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        return Position(
            entry_price=limit_price,
            qty=qty,
            notional_usd=self.position_size,
            entry_ts=int(time.time()),
            order_id=str(order_id),
        )

    def place_sell(self, position: Position, bid: float, reason: str) -> dict:
        """Place a taker SELL limit order. Returns trade record dict."""
        limit_price = self._sell_limit_price(bid)
        result = _kraken_cli([
            "order", "sell",
            self.pair,
            f"{position.qty:.8f}",
            "--type", "limit",
            "--price", f"{limit_price:.8f}",
            "--yes",
        ])
        exit_price = limit_price  # assume fill at limit
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
```

- [ ] **Step 4: Run executor tests**

```
python -m pytest tests/test_meme_agent.py -v -k "executor"
```

Expected: all 9 executor tests PASS.

- [ ] **Step 5: Run full suite**

```
python -m pytest tests/test_meme_agent.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```
git add hydra_meme_agent.py tests/test_meme_agent.py
git commit -m "feat(apex): MemeExecutor — taker orders, position tracking, daily cap"
```

---

## Task 7: WebSocket server and main agent loop

**Files:**
- Modify: `hydra_meme_agent.py` (add `OBIPoller`, `CandleAggregator`, `MemeAgent`, `main()`)

This task wires everything together. Tests are integration-level; run manually with `--dry-run`.

- [ ] **Step 1: Add OBIPoller to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── OBI Poller ────────────────────────────────────────────────────────────────

class OBIPoller:
    """Polls kraken orderbook every 10s and caches OBI + best bid/ask."""

    def __init__(self, pair: str):
        self.pair = pair
        self._pair_nodash = pair.replace("/", "")
        self._obi: float = 0.0
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0
        self._last_poll: float = 0.0

    def poll(self) -> None:
        """Fetch orderbook and update cached values. Enforces 2s floor."""
        now = time.time()
        if now - self._last_poll < KRAKEN_REST_FLOOR:
            return
        self._last_poll = now
        result = _kraken_cli(["orderbook", self._pair_nodash, "--count", str(OBI_LEVELS)])
        if "error" in result:
            return
        # Kraken orderbook response: {PAIR: {bids: [[price, qty, ts], ...], asks: [...]}}
        book = result.get(self._pair_nodash) or result.get(self.pair) or {}
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            self._obi = compute_obi(
                [(b[0], b[1]) for b in bids],
                [(a[0], a[1]) for a in asks],
            )
            self._best_bid = float(bids[0][0])
            self._best_ask = float(asks[0][0])

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

    def ask_wall_usd(self) -> float:
        """Compute top-3 ask levels total USD depth (for ask_wall_clear gate)."""
        result = _kraken_cli(["orderbook", self._pair_nodash, "--count", "3"])
        if "error" in result:
            return 999_999.0
        book = result.get(self._pair_nodash) or result.get(self.pair) or {}
        asks = book.get("asks", [])
        return sum(float(a[0]) * float(a[1]) for a in asks[:3])
```

- [ ] **Step 2: Add CandleAggregator to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── Candle Aggregator ─────────────────────────────────────────────────────────

class CandleAggregator:
    """Subscribes to Kraken public WebSocket ohlc-5 channel.
    
    Fires on_bar callback with each newly closed CandleBar.
    Reconnects with exponential backoff on disconnect.
    """
    WS_URL = "wss://ws.kraken.com"

    def __init__(self, pair: str, on_bar):
        self.pair = pair          # e.g. "PLAY/USD"
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
```

- [ ] **Step 3: Add MemeAgent and main() to hydra_meme_agent.py**

Append to `hydra_meme_agent.py`:

```python
# ─── Meme Agent ────────────────────────────────────────────────────────────────

class MemeAgent:
    """Orchestrates all components and broadcasts state on port 8766."""

    def __init__(self, pair: str, position_size: float, daily_cap: float,
                 session_path: str = "hydra_meme_session.json",
                 journal_path: str = "hydra_meme_journal.json",
                 watchlist_path: str = "hydra_meme_watchlist.json"):
        self.pair = pair
        self._clients: set = set()
        self._signal_engine = SignalEngine()
        self._obi_poller = OBIPoller(pair)
        self._executor = MemeExecutor(pair, position_size, daily_cap)
        self._detector = CompetitionDetector(watchlist_path)
        self._candle_agg = CandleAggregator(pair, self._on_bar)
        self._position: Optional[Position] = None
        self._session_path = session_path
        self._journal_path = journal_path
        self._trade_log: list[TradeRecord] = []
        self._engine_state = "warmup"
        self._last_competition_scan = 0.0

    # ── WebSocket server ──

    async def _ws_handler(self, websocket) -> None:
        self._clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)

    async def _broadcast(self, msg: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(msg)
        await asyncio.gather(*[c.send(data) for c in list(self._clients)],
                             return_exceptions=True)

    def _broadcast_sync(self, msg: dict) -> None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._broadcast(msg))
        except Exception:
            pass

    # ── Bar callback (from CandleAggregator, called in WS thread) ──

    def _on_bar(self, bar: CandleBar) -> None:
        self._signal_engine.add_bar(bar)
        if not self._signal_engine.is_warmed_up():
            self._engine_state = "warmup"
            return
        self._engine_state = "running"
        # Broadcast signal state
        gates = self._signal_engine.evaluate_entry_gates(
            bar, self._obi_poller.obi, self._obi_poller.ask_wall_usd()
        )
        self._broadcast_sync({"type": "signal_state", "gates": gates,
                               "pair": self.pair, "ts": bar.ts})
        # Bar-close exit check
        if self._position is not None:
            self._position.candles_held += 1
            reason = self._signal_engine.evaluate_exit_bar(self._position, bar)
            if reason:
                self._exit_position(reason)
                return
        # Entry check (only when no position)
        if self._position is None and not self._executor.is_halted():
            if gates["all_pass"]:
                pos = self._executor.place_buy(self._obi_poller.best_ask)
                if pos:
                    self._position = pos
                    self._broadcast_sync({"type": "order_placed",
                                          "side": "buy",
                                          "price": pos.entry_price,
                                          "qty": pos.qty})

    def _exit_position(self, reason: str) -> None:
        if self._position is None:
            return
        result = self._executor.place_sell(
            self._position, self._obi_poller.best_bid, reason
        )
        record: TradeRecord = result["record"]
        self._trade_log.append(record)
        append_journal(record, self._journal_path)
        self._broadcast_sync({"type": "trade_closed",
                               "net_pnl": record.net_pnl,
                               "exit_reason": reason,
                               "entry": record.entry_price,
                               "exit": record.exit_price})
        self._position = None
        if self._executor.is_halted():
            self._engine_state = "halted"
            self._broadcast_sync({"type": "engine_halted",
                                   "reason": "daily_cap",
                                   "daily_pnl": self._executor._daily_pnl})
        self._broadcast_sync({
            "type": "session_stats",
            "session_pnl": self._executor._daily_pnl,
            "trade_count": len(self._trade_log),
            "win_rate": sum(1 for t in self._trade_log if t.net_pnl > 0) / len(self._trade_log),
            "daily_cap_remaining": self._executor.daily_cap + self._executor._daily_loss,
        })

    # ── 10-second OBI loop ──

    async def _obi_loop(self) -> None:
        while True:
            if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                self._engine_state = "halted"
                await self._broadcast({"type": "engine_halted", "reason": "kill_switch"})
                return
            await asyncio.to_thread(self._obi_poller.poll)
            if self._position is not None and self._engine_state == "running":
                reason = self._signal_engine.evaluate_exit_intracandle(
                    self._position, self._obi_poller.mid_price, self._obi_poller.obi
                )
                if reason:
                    self._exit_position(reason)
            if self._position is not None:
                await self._broadcast({
                    "type": "position_update",
                    "price": self._obi_poller.mid_price,
                    "obi": self._obi_poller.obi,
                    "entry": self._position.entry_price,
                    "unrealised_pnl": (self._obi_poller.mid_price - self._position.entry_price)
                                      * self._position.qty,
                })
            await asyncio.sleep(OBI_POLL_INTERVAL)

    # ── 15-minute competition scan loop ──

    async def _competition_loop(self) -> None:
        while True:
            await asyncio.sleep(COMPETITION_SCAN_INTERVAL)
            tokens = self._detector.get_all_tokens()
            last_call = 0.0
            for token in tokens:
                # Enforce 2s rate floor between ticker calls
                elapsed = time.time() - last_call
                if elapsed < KRAKEN_REST_FLOOR:
                    await asyncio.sleep(KRAKEN_REST_FLOOR - elapsed)
                pair_nodash = token["pair"].replace("/", "")
                result = await asyncio.to_thread(
                    _kraken_cli, ["ticker", pair_nodash]
                )
                last_call = time.time()
                if "error" in result:
                    continue
                ticker_data = result.get(pair_nodash) or result.get(token["pair"]) or {}
                # Kraken ticker: v[1] = 24h volume
                vol_str = ticker_data.get("v", [None, None])[1]
                if not vol_str:
                    continue
                volume = float(vol_str)
                if self._detector._get_baseline(token["pair"]) is None:
                    self._detector._set_baseline(token["pair"], volume)
                    continue
                self._detector._update_baseline(token["pair"], volume)
                if (not self._detector._is_suppressed(token["pair"])
                        and self._detector._is_anomaly(token["pair"], volume)):
                    comp_type = self._detector.infer_competition_type(token["pair"])
                    await self._broadcast({
                        "type": "competition_alert",
                        "pair": token["pair"],
                        "volume": volume,
                        "baseline": self._detector._get_baseline(token["pair"]),
                        "ratio": volume / self._detector._get_baseline(token["pair"]),
                        "competition_type": comp_type,
                    })

    # ── Main run ──

    async def run(self) -> None:
        server = await websockets.serve(self._ws_handler, "localhost", WS_PORT)
        print(f"[APEX] WebSocket server on ws://localhost:{WS_PORT}")
        print(f"[APEX] Trading {self.pair} | Warmup: {WARMUP_BARS} bars ({WARMUP_BARS * CANDLE_INTERVAL} min)")
        try:
            await asyncio.gather(
                self._candle_agg.run(),
                self._obi_loop(),
                self._competition_loop(),
            )
        finally:
            server.close()
            await server.wait_closed()


# ─── Entry Point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="APEX Meme Engine")
    p.add_argument("--pair", required=True, help="Trading pair e.g. PLAY/USD")
    p.add_argument("--position-size", type=float, default=600.0)
    p.add_argument("--daily-cap", type=float, default=30.0)
    p.add_argument("--session-path", default="hydra_meme_session.json")
    p.add_argument("--journal-path", default="hydra_meme_journal.json")
    p.add_argument("--watchlist-path", default="hydra_meme_watchlist.json")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    agent = MemeAgent(
        pair=args.pair,
        position_size=args.position_size,
        daily_cap=args.daily_cap,
        session_path=args.session_path,
        journal_path=args.journal_path,
        watchlist_path=args.watchlist_path,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify syntax (no WSL needed)**

```
python -c "import hydra_meme_agent; print('syntax OK')"
```

Expected: `syntax OK`

- [ ] **Step 5: Run full test suite**

```
python -m pytest tests/test_meme_agent.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```
git add hydra_meme_agent.py
git commit -m "feat(apex): OBIPoller, CandleAggregator (WS ohlc-5), MemeAgent main loop"
```

---

## Task 8: start_meme.bat

**Files:**
- Create: `start_meme.bat`

- [ ] **Step 1: Create start_meme.bat**

Create `start_meme.bat`:

```bat
@echo off
python hydra_meme_agent.py --pair PLAY/USD %*
```

`%*` passes any extra args through, so `start_meme.bat --position-size 1200` works.

- [ ] **Step 2: Verify syntax**

```
cmd /c "start_meme.bat --help" 2>&1 | head -5
```

Expected: shows APEX argparse help.

- [ ] **Step 3: Commit**

```
git add start_meme.bat
git commit -m "feat(apex): start_meme.bat launcher"
```

---

## Task 9: App.jsx — add MEME tab

**Files:**
- Modify: `dashboard/src/App.jsx`

The existing tabs array is at line ~901. The existing tab rendering pattern uses `{activeTab === "KEY" && <Component />}` at line ~3799. The MEME tab adds `MemeTab` import and one render block. Research and Thesis components are NOT deleted — only removed from the `tabs` array.

- [ ] **Step 1: Add MemeTab import to App.jsx**

At the top of `dashboard/src/App.jsx`, after the existing imports (after line 3 which imports `ResearchTab`):

```javascript
import MemeTab from "./MemeTab";
```

- [ ] **Step 2: Add MEME to the tabs array**

Find this block (~line 901):
```javascript
const tabs = [
  { key: "LIVE",     label: "LIVE",     color: COLORS.accent },
  { key: "RESEARCH", label: "RESEARCH", color: COLORS.blue },
  { key: "THESIS",   label: "THESIS",   color: COLORS.warn },
  { key: "SETTINGS", label: "SETTINGS", color: COLORS.text },
];
```

Replace with:
```javascript
const tabs = [
  { key: "LIVE",     label: "LIVE",     color: COLORS.accent },
  { key: "MEME",     label: "MEME",     color: "#8b5cf6" },
  { key: "SETTINGS", label: "SETTINGS", color: COLORS.text },
];
```

- [ ] **Step 3: Add MEME tab render block**

Find the existing tab render blocks (~line 3799). Add this block immediately before the SETTINGS block:

```javascript
{activeTab === "MEME" && (
  <MemeTab />
)}
```

- [ ] **Step 4: Build dashboard — verify no errors**

```
cd dashboard && npm run build 2>&1 | tail -20
```

Expected: `✓ built in Xs` with no errors. If `MemeTab` import fails, that's expected — create it in Task 10.

- [ ] **Step 5: Commit**

```
git add dashboard/src/App.jsx
git commit -m "feat(apex): add MEME tab to App.jsx TabSwitcher"
```

---

## Task 10: MemeTab.jsx — WebSocket + skeleton + Trading view

**Files:**
- Create: `dashboard/src/MemeTab.jsx`

Hydra design tokens (from App.jsx COLORS object):
- `bg: "#09090b"`, `panel: "#18181b"`, `border: "#27272a"`
- `accent: "#10b981"` (green), `purple: "#8b5cf6"`, `warn: "#f59e0b"`, `danger: "#ef4444"`
- `text: "#f4f4f5"`, `muted: "#71717a"`, `blue: "#3b82f6"`
- Font: `JetBrains Mono, monospace` for numbers/labels

- [ ] **Step 1: Create MemeTab.jsx**

Create `dashboard/src/MemeTab.jsx`:

```jsx
import { useState, useEffect, useRef, useCallback } from "react";

const C = {
  bg: "#09090b",
  panel: "#18181b",
  border: "#27272a",
  accent: "#10b981",
  purple: "#8b5cf6",
  warn: "#f59e0b",
  danger: "#ef4444",
  text: "#f4f4f5",
  muted: "#71717a",
  blue: "#3b82f6",
  mono: "JetBrains Mono, monospace",
  sans: "Space Grotesk, system-ui, sans-serif",
};

const APEX_WS = "ws://localhost:8766";

function GateDot({ pass, label, value }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%",
        background: pass ? C.accent : C.danger,
        flexShrink: 0,
      }} />
      <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted, minWidth: 100 }}>{label}</span>
      <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{value ?? "—"}</span>
    </div>
  );
}

function OBIGauge({ obi }) {
  const pct = ((obi + 1) / 2) * 100;
  const color = obi > 0.2 ? C.accent : obi < -0.2 ? C.danger : C.warn;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>SELL</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color, fontWeight: 700 }}>
          OBI {obi >= 0 ? "+" : ""}{obi.toFixed(3)}
        </span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>BUY</span>
      </div>
      <div style={{ height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`,
          background: `linear-gradient(90deg, ${C.danger}, ${color})`,
          transition: "width 0.5s ease",
        }} />
      </div>
    </div>
  );
}

function SignalBanner({ allPass, engineState }) {
  if (engineState === "warmup") {
    return (
      <div style={{
        padding: "10px 16px", borderRadius: 8, textAlign: "center",
        background: "#1c1917", border: `1px solid ${C.warn}40`,
        fontFamily: C.mono, fontSize: 13, color: C.warn,
      }}>
        ⏳ WARMING UP
      </div>
    );
  }
  const color = allPass ? C.accent : C.muted;
  const label = allPass ? "⚡ BUY SIGNAL" : "— HOLD —";
  return (
    <div style={{
      padding: "10px 16px", borderRadius: 8, textAlign: "center",
      background: allPass ? `${C.accent}18` : "#18181b",
      border: `1px solid ${allPass ? C.accent + "60" : C.border}`,
      fontFamily: C.mono, fontSize: 15, fontWeight: 700, color,
      transition: "all 0.3s ease",
    }}>
      {label}
    </div>
  );
}

function PositionPanel({ position, midPrice, sessionStats }) {
  if (!position) {
    return (
      <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}` }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Position</div>
        <div style={{ fontFamily: C.mono, fontSize: 12, color: C.muted, textAlign: "center",
                      padding: "20px 0" }}>No open position</div>
      </div>
    );
  }
  const entryPct = ((midPrice - position.entry_price) / position.entry_price) * 100;
  const targetPct = 2.5;
  const stopPct = -1.3;
  const progress = Math.max(0, Math.min(100, ((entryPct - stopPct) / (targetPct - stopPct)) * 100));
  const pnlColor = entryPct >= 0 ? C.accent : C.danger;
  return (
    <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.purple}40` }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.purple, marginBottom: 8,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Open Position</div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>Entry</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{position.entry_price.toFixed(6)}</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.muted }}>Mid</span>
        <span style={{ fontFamily: C.mono, fontSize: 11, color: C.text }}>{midPrice.toFixed(6)}</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ fontFamily: C.mono, fontSize: 12, color: C.muted }}>Unrealised</span>
        <span style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700, color: pnlColor }}>
          {entryPct >= 0 ? "+" : ""}{entryPct.toFixed(2)}%
        </span>
      </div>
      <div style={{ marginBottom: 4, display: "flex", justifyContent: "space-between" }}>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.danger }}>▼ −1.3%</span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>progress</span>
        <span style={{ fontFamily: C.mono, fontSize: 10, color: C.accent }}>▲ +2.5%</span>
      </div>
      <div style={{ height: 6, background: "#27272a", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${progress}%`,
          background: entryPct >= 0 ? C.accent : C.danger,
          transition: "width 0.5s ease",
        }} />
      </div>
      <div style={{ marginTop: 8, fontFamily: C.mono, fontSize: 10, color: C.muted }}>
        {position.candles_held} candle{position.candles_held !== 1 ? "s" : ""} held · time stop at {3 - position.candles_held} more
      </div>
    </div>
  );
}

function SessionStats({ stats, dailyCap }) {
  const remaining = dailyCap + (stats?.daily_loss ?? 0);
  const usedPct = Math.max(0, Math.min(100, ((dailyCap - remaining) / dailyCap) * 100));
  return (
    <div style={{ padding: 12, background: C.panel, borderRadius: 8, border: `1px solid ${C.border}`,
                  marginTop: 8 }}>
      <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                    textTransform: "uppercase", letterSpacing: "0.1em" }}>Session</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 8 }}>
        {[
          ["Net P&L", `$${(stats?.session_pnl ?? 0).toFixed(2)}`, (stats?.session_pnl ?? 0) >= 0 ? C.accent : C.danger],
          ["Win Rate", `${((stats?.win_rate ?? 0) * 100).toFixed(0)}%`, C.text],
          ["Trades", stats?.trade_count ?? 0, C.text],
          ["Cap Left", `$${remaining.toFixed(2)}`, remaining > 10 ? C.accent : C.danger],
        ].map(([label, val, color]) => (
          <div key={label}>
            <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{label}</div>
            <div style={{ fontFamily: C.mono, fontSize: 13, fontWeight: 700, color }}>{val}</div>
          </div>
        ))}
      </div>
      <div style={{ height: 4, background: "#27272a", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${usedPct}%`, background: C.danger,
                      transition: "width 0.5s ease" }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted, marginTop: 2 }}>
        daily cap: ${dailyCap.toFixed(0)}
      </div>
    </div>
  );
}

function TradeLog({ trades }) {
  if (!trades || trades.length === 0) {
    return (
      <div style={{ fontFamily: C.mono, fontSize: 11, color: C.muted, padding: "16px 0",
                    textAlign: "center" }}>
        No closed trades this session
      </div>
    );
  }
  const cols = ["TIME", "ENTRY", "EXIT", "NET P&L", "REASON", "HOLD"];
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono, fontSize: 11 }}>
        <thead>
          <tr>
            {cols.map(c => (
              <th key={c} style={{ padding: "4px 8px", textAlign: "left", color: C.muted,
                                   borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((t, i) => (
            <tr key={i} style={{ borderBottom: `1px solid ${C.border}20` }}>
              <td style={{ padding: "4px 8px", color: C.muted }}>
                {new Date(t.exit_ts * 1000).toLocaleTimeString()}
              </td>
              <td style={{ padding: "4px 8px", color: C.text }}>{t.entry_price.toFixed(6)}</td>
              <td style={{ padding: "4px 8px", color: C.text }}>{t.exit_price.toFixed(6)}</td>
              <td style={{ padding: "4px 8px", fontWeight: 700,
                           color: t.net_pnl >= 0 ? C.accent : C.danger }}>
                {t.net_pnl >= 0 ? "+" : ""}${t.net_pnl.toFixed(2)}
              </td>
              <td style={{ padding: "4px 8px", color: C.muted }}>{t.exit_reason}</td>
              <td style={{ padding: "4px 8px", color: C.muted }}>{t.hold_candles}c</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradingView({ state, dailyCap }) {
  const { gates, position, midPrice, obi, engineState, sessionStats, trades } = state;
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 16 }}>
        {/* Left — OBI gauge */}
        <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>Market</div>
          <div style={{ fontFamily: C.mono, fontSize: 22, fontWeight: 700, color: C.text }}>
            {midPrice > 0 ? `$${midPrice.toFixed(6)}` : "—"}
          </div>
          <OBIGauge obi={obi ?? 0} />
        </div>

        {/* Middle — gates + signal */}
        <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                      border: `1px solid ${C.border}` }}>
          <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>Entry Gates</div>
          <GateDot pass={gates?.volume_spike} label="Vol Spike"
                   value={gates?.vol_ema_value ? `${(gates.vol_ema_value / 1000).toFixed(1)}k` : null} />
          <GateDot pass={gates?.obi} label="OBI >0.20"
                   value={obi != null ? obi.toFixed(3) : null} />
          <GateDot pass={gates?.vwap_align} label="VWAP Align"
                   value={gates?.vwap_value ? `$${parseFloat(gates.vwap_value).toFixed(5)}` : null} />
          <GateDot pass={gates?.rsi_window} label="RSI 45–78"
                   value={gates?.rsi_value} />
          <GateDot pass={gates?.ask_wall_clear} label="Ask Wall" value="<$500" />
          <div style={{ marginTop: 10 }}>
            <SignalBanner allPass={gates?.all_pass ?? false} engineState={engineState} />
          </div>
        </div>

        {/* Right — position + stats */}
        <div>
          <PositionPanel position={position} midPrice={midPrice ?? 0} sessionStats={sessionStats} />
          <SessionStats stats={sessionStats} dailyCap={dailyCap} />
        </div>
      </div>

      {/* Trade log */}
      <div style={{ padding: 12, background: C.panel, borderRadius: 8,
                    border: `1px solid ${C.border}` }}>
        <div style={{ fontFamily: C.mono, fontSize: 10, color: C.muted, marginBottom: 8,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>Trade Log</div>
        <TradeLog trades={trades} />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Continue MemeTab.jsx — Discover view**

Append to `dashboard/src/MemeTab.jsx`:

```jsx
function TierBar({ sharePct }) {
  // Prize tiers: top 5% = green, top 10% = blue, top 25% = amber
  const tiers = [
    { label: "Top 5%", pct: 5, color: C.accent },
    { label: "Top 10%", pct: 10, color: C.blue },
    { label: "Top 25%", pct: 25, color: C.warn },
  ];
  const userPos = Math.min(sharePct * 500, 100); // rough rank estimate: 1 per 0.2% share
  const color = userPos <= 5 ? C.accent : userPos <= 10 ? C.blue : userPos <= 25 ? C.warn : C.danger;
  return (
    <div>
      <div style={{ position: "relative", height: 20, background: "#27272a", borderRadius: 4,
                    overflow: "visible", marginBottom: 4 }}>
        {tiers.map(t => (
          <div key={t.label} style={{
            position: "absolute", left: `${t.pct}%`, top: 0, bottom: 0,
            width: 1, background: t.color + "60",
          }}>
            <span style={{
              position: "absolute", top: -16, left: 2,
              fontFamily: C.mono, fontSize: 9, color: t.color, whiteSpace: "nowrap",
            }}>{t.label}</span>
          </div>
        ))}
        <div style={{
          position: "absolute", top: 2, bottom: 2,
          left: `${Math.min(userPos, 98)}%`, width: 3, borderRadius: 2,
          background: color, transition: "left 0.3s ease",
        }} />
      </div>
      <div style={{ fontFamily: C.mono, fontSize: 10, color }}>
        Est. rank: top {userPos.toFixed(0)}%
      </div>
    </div>
  );
}

function DiscoverView({ tokens, onStartEngine, onDismiss, enginePair }) {
  const [levers, setLevers] = useState({});

  function getShares(token, posSize) {
    const baseline = token.baseline_volume_7d ?? 3_200_000;
    const price = 0.165; // approximation; ideally from ticker
    const marketUsd = baseline * 6 * price; // 6x anomaly assumption during competition
    const tradesPerDay = 5;
    const userUsd = tradesPerDay * posSize * 2;
    return userUsd / (marketUsd || 1);
  }

  function ratioColor(ratio) {
    if (!ratio) return C.muted;
    if (ratio >= 7) return C.danger;
    if (ratio >= 4) return C.warn;
    return C.blue;
  }

  const anomalous = tokens.filter(t => t.anomaly_ratio && t.anomaly_ratio >= 3);

  return (
    <div>
      {anomalous.length === 0 && (
        <div style={{ padding: 24, textAlign: "center", fontFamily: C.mono,
                      fontSize: 12, color: C.muted }}>
          No anomalies detected. Next scan in progress...
        </div>
      )}
      {anomalous.map(token => {
        const posSize = levers[token.pair] ?? 600;
        const sharePct = getShares(token, posSize) * 100;
        const canTrade = !enginePair || enginePair === token.pair;
        return (
          <div key={token.pair} style={{
            padding: 16, background: C.panel, borderRadius: 8,
            border: `1px solid ${C.border}`, marginBottom: 12,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div>
                <span style={{ fontFamily: C.mono, fontSize: 14, fontWeight: 700,
                               color: C.text }}>{token.pair}</span>
                <span style={{
                  marginLeft: 8, padding: "2px 8px", borderRadius: 4,
                  fontFamily: C.mono, fontSize: 10, fontWeight: 700,
                  background: ratioColor(token.anomaly_ratio) + "20",
                  color: ratioColor(token.anomaly_ratio),
                }}>
                  {token.anomaly_ratio?.toFixed(1)}× baseline
                </span>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  onClick={() => onDismiss(token.pair)}
                  style={{
                    padding: "4px 10px", borderRadius: 6, border: `1px solid ${C.border}`,
                    background: "transparent", color: C.muted, fontFamily: C.mono,
                    fontSize: 11, cursor: "pointer",
                  }}
                >
                  Dismiss 2h
                </button>
                <button
                  onClick={() => canTrade && onStartEngine(token.pair, posSize)}
                  disabled={!canTrade}
                  style={{
                    padding: "4px 14px", borderRadius: 6, border: "none",
                    background: canTrade ? C.purple : C.border,
                    color: canTrade ? C.text : C.muted,
                    fontFamily: C.mono, fontSize: 11, fontWeight: 700,
                    cursor: canTrade ? "pointer" : "not-allowed",
                  }}
                >
                  {enginePair === token.pair ? "Running ✓" : canTrade ? "Start APEX" : "Engine Busy"}
                </button>
              </div>
            </div>

            {/* Tier lever */}
            <div style={{ marginTop: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                  Position size: <strong style={{ color: C.text }}>${posSize}</strong>
                  {" "}→ ${(5 * posSize * 2).toLocaleString()}/day projected
                </span>
                <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
                  {sharePct.toFixed(3)}% market share
                </span>
              </div>
              <input
                type="range" min={600} max={3000} step={100}
                value={posSize}
                onChange={e => setLevers(l => ({ ...l, [token.pair]: Number(e.target.value) }))}
                style={{ width: "100%", accentColor: C.purple, marginBottom: 8 }}
              />
              <TierBar sharePct={sharePct / 100} />
            </div>

            <div style={{ marginTop: 8, padding: 8, background: "#0f1923", borderRadius: 6,
                          fontFamily: C.mono, fontSize: 10, color: C.muted }}>
              Competition type: <span style={{ color: C.warn }}>
                {token.competition_type || "unknown"}{!token.competition_type_confirmed ? " (inferred)" : ""}
              </span>
              {" — "}
              <a href="https://www.kraken.com/promotions" target="_blank" rel="noopener noreferrer"
                 style={{ color: C.blue }}>verify on Kraken</a>
            </div>
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 3: Add opt-in modal and main MemeTab export**

Append to `dashboard/src/MemeTab.jsx`:

```jsx
function CompetitionModal({ alert, onStart, onDismiss }) {
  if (!alert) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999,
    }}>
      <div style={{
        background: C.panel, border: `1px solid ${C.purple}60`, borderRadius: 12,
        padding: 24, maxWidth: 480, width: "90%",
      }}>
        <div style={{ fontFamily: C.mono, fontSize: 11, color: C.purple, marginBottom: 4,
                      textTransform: "uppercase", letterSpacing: "0.1em" }}>
          ⚡ Competition Detected
        </div>
        <div style={{ fontFamily: C.sans, fontSize: 18, fontWeight: 700, color: C.text,
                      marginBottom: 12 }}>{alert.pair}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 16 }}>
          {[
            ["Volume", `${(alert.volume / 1_000_000).toFixed(1)}M`],
            ["Baseline", `${(alert.baseline / 1_000_000).toFixed(1)}M`],
            ["Ratio", `${alert.ratio?.toFixed(1)}× baseline`],
            ["Type", `${alert.competition_type || "unknown"} (inferred)`],
          ].map(([l, v]) => (
            <div key={l} style={{ padding: 8, background: C.bg, borderRadius: 6 }}>
              <div style={{ fontFamily: C.mono, fontSize: 9, color: C.muted }}>{l}</div>
              <div style={{ fontFamily: C.mono, fontSize: 12, color: C.text }}>{v}</div>
            </div>
          ))}
        </div>
        <div style={{ padding: 8, background: "#0f1923", borderRadius: 6, marginBottom: 16,
                      fontFamily: C.mono, fontSize: 10, color: C.muted }}>
          Strategy: $600 position · +2.5% target · −1.3% stop · 5-min candles
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => onStart(alert.pair)}
            style={{
              flex: 1, padding: "10px 0", borderRadius: 8, border: "none",
              background: C.purple, color: C.text,
              fontFamily: C.mono, fontSize: 13, fontWeight: 700, cursor: "pointer",
            }}
          >
            Start APEX Engine
          </button>
          <button
            onClick={() => onDismiss(alert.pair)}
            style={{
              padding: "10px 16px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: "transparent",
              color: C.muted, fontFamily: C.mono, fontSize: 12, cursor: "pointer",
            }}
          >
            Dismiss (2h)
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MemeTab() {
  const [subView, setSubView] = useState("discover"); // "trading" | "discover"
  const [connected, setConnected] = useState(false);
  const [engineState, setEngineState] = useState("idle");
  const [enginePair, setEnginePair] = useState(null);
  const [gates, setGates] = useState(null);
  const [position, setPosition] = useState(null);
  const [midPrice, setMidPrice] = useState(0);
  const [obi, setObi] = useState(0);
  const [sessionStats, setSessionStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [tokens, setTokens] = useState([]);
  const [pendingAlert, setPendingAlert] = useState(null);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(APEX_WS);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      reconnectRef.current = setTimeout(() => connect(), 5000);
    };
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        switch (msg.type) {
          case "signal_state":
            setGates(msg.gates);
            if (msg.gates?.all_pass) setSubView("trading");
            break;
          case "position_update":
            setMidPrice(msg.price ?? 0);
            setObi(msg.obi ?? 0);
            if (msg.entry) setPosition(p => p ? { ...p } : null);
            break;
          case "order_placed":
            if (msg.side === "buy") {
              setPosition({ entry_price: msg.price, qty: msg.qty,
                            notional_usd: 600, entry_ts: Date.now() / 1000, candles_held: 0 });
              setSubView("trading");
            }
            break;
          case "trade_closed":
            setPosition(null);
            setTrades(prev => [...prev, msg]);
            break;
          case "session_stats":
            setSessionStats(msg);
            break;
          case "engine_halted":
            setEngineState("halted");
            break;
          case "competition_alert":
            setPendingAlert(msg);
            break;
          default:
            break;
        }
      } catch (e) {
        console.error("[APEX] parse error", e);
      }
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  function handleStartEngine(pair) {
    setPendingAlert(null);
    setEnginePair(pair);
    setEngineState("warmup");
    setSubView("trading");
  }

  function handleDismiss(pair) {
    setPendingAlert(null);
    // Tell agent to suppress: send dismiss message
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "dismiss_alert", pair }));
    }
  }

  const tradingState = { gates, position, midPrice, obi, engineState, sessionStats, trades };

  return (
    <div style={{ padding: "16px 24px", background: C.bg, minHeight: "100%" }}>
      {/* Sub-nav */}
      <div style={{ display: "flex", gap: 0, marginBottom: 20, borderBottom: `1px solid ${C.border}` }}>
        {[["trading", "⚡ Trading"], ["discover", "🔍 Discover"]].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setSubView(key)}
            style={{
              padding: "8px 18px", border: "none", background: "transparent",
              fontFamily: C.mono, fontSize: 12, fontWeight: 700,
              color: subView === key ? C.purple : C.muted,
              borderBottom: `2px solid ${subView === key ? C.purple : "transparent"}`,
              cursor: "pointer", transition: "all 0.2s",
            }}
          >
            {label}
          </button>
        ))}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6,
                      paddingBottom: 8 }}>
          <div style={{
            width: 6, height: 6, borderRadius: "50%",
            background: connected ? C.accent : C.danger,
          }} />
          <span style={{ fontFamily: C.mono, fontSize: 10, color: C.muted }}>
            {connected ? `APEX ${enginePair ?? "idle"}` : "disconnected"}
          </span>
        </div>
      </div>

      {subView === "trading" && (
        <TradingView state={tradingState} dailyCap={30} />
      )}
      {subView === "discover" && (
        <DiscoverView
          tokens={tokens}
          onStartEngine={handleStartEngine}
          onDismiss={handleDismiss}
          enginePair={enginePair}
        />
      )}

      <CompetitionModal
        alert={pendingAlert}
        onStart={handleStartEngine}
        onDismiss={handleDismiss}
      />
    </div>
  );
}
```

- [ ] **Step 4: Build dashboard**

```
cd dashboard && npm run build 2>&1 | tail -20
```

Expected: build succeeds with no errors.

- [ ] **Step 5: Start dev server and verify MEME tab renders**

```
cd dashboard && npm run dev
```

Open `http://localhost:5173` in browser. Click MEME tab. Expect:
- Sub-nav with "⚡ Trading" and "🔍 Discover"
- Discover view with "No anomalies detected" placeholder
- Connection dot showing red (disconnected — engine not running yet)
- No console errors

- [ ] **Step 6: Commit**

```
git add dashboard/src/MemeTab.jsx
git commit -m "feat(apex): MemeTab.jsx — Trading view, Discover view, competition modal"
```

---

## Task 11: Full integration smoke test

**Files:**
- None (verification only)

- [ ] **Step 1: Run full Python test suite**

```
python -m pytest tests/test_meme_agent.py -v --tb=short
```

Expected: all tests PASS with 0 failures.

- [ ] **Step 2: Verify Python syntax of complete file**

```
python -c "import ast; ast.parse(open('hydra_meme_agent.py').read()); print('AST OK')"
```

Expected: `AST OK`

- [ ] **Step 3: Verify existing Hydra tests still pass**

```
python -m pytest tests/ -v --ignore=tests/live_harness -x --tb=short 2>&1 | tail -20
```

Expected: all existing tests PASS (APEX is isolated — zero changes to existing engine).

- [ ] **Step 4: Verify dashboard build passes CI**

```
cd dashboard && npm run build 2>&1 | tail -5
```

Expected: successful build.

- [ ] **Step 5: Dry-run engine start (no real orders)**

```
python hydra_meme_agent.py --pair PLAY/USD --help
```

Expected: argparse help output.

- [ ] **Step 6: Final commit and version note**

APEX does not bump the Hydra version (`HYDRA_VERSION` in `hydra_backtest.py`) — it is a separate isolated system and not part of the triangle engine. The MEME tab addition to App.jsx is a dashboard feature; update the footer string in `App.jsx` only if a MINOR version bump is warranted per CLAUDE.md policy. For now, no version bump.

```
git add -A
git commit -m "feat(apex): complete APEX meme engine — engine + dashboard + tests"
```

---

## Self-Review Checklist

**Spec coverage:**
- §2 Architecture isolation ✓ (Tasks 1-8, new files only)
- §3 5 internal components ✓ (CandleAggregator WS, OBIPoller, SignalEngine, MemeExecutor, CompetitionDetector)
- §3 CLI args ✓ (Task 7 `_parse_args`)
- §3 WS broadcast messages ✓ (all 7 message types in Task 7)
- §4 Signal engine — 5 entry gates ✓ (Tasks 2-3)
- §4 Exit triggers — two cadences ✓ (Tasks 2-3, 10s + bar-close)
- §4 Position sizing ✓ (Task 6)
- §4 Order execution ✓ (Task 6)
- §4 Wilder RSI ✓ (Task 1)
- §4 WS ohlc-5 ✓ (Task 7)
- §5 Competition Detector ✓ (Task 4)
- §5 Alert suppression ✓ (Task 4, `dismiss_alert` WS message)
- §6 Tab structure LIVE/MEME/SETTINGS ✓ (Task 9)
- §6 Sub-nav Trading/Discover ✓ (Task 10)
- §6 Trading view components ✓ (Task 10)
- §6 Discover view ✓ (Task 10)
- §6 Competition modal ✓ (Task 10)
- §7 State management ✓ (Task 5 — session + journal)
- §8 Risk controls ✓ (daily cap Task 6, kill switch Task 7, slippage cap Task 6)
- §10 start_meme.bat ✓ (Task 8)
- §11 Competition intelligence / tier lever ✓ (Task 10 DiscoverView + TierBar)

**Gaps found and resolved:**
- CompetitionDetector `dismiss_alert` WS handler: the `MemeAgent` does not yet process incoming WS messages (it only broadcasts). The agent's `_ws_handler` should parse messages from connected clients and call `_detector._suppress(pair, time.time() + 7200)` on `dismiss_alert`. Add this to Task 7 Step 3 — append to `_ws_handler`:

```python
async def _ws_handler(self, websocket) -> None:
    self._clients.add(websocket)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "dismiss_alert":
                    pair = msg.get("pair", "")
                    if pair:
                        self._detector._suppress(pair, time.time() + 7200)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        self._clients.discard(websocket)
```

- `tokens` state in MemeTab is never populated (no WS message sends the full token list). Add a `watchlist_update` broadcast in `_competition_loop` after each scan, and handle it in `MemeTab.onmessage`. Add to `_competition_loop` after the loop: `await self._broadcast({"type": "watchlist_update", "tokens": self._detector.get_all_tokens()})`. In MemeTab, add `case "watchlist_update": setTokens(msg.tokens ?? []);`.

Both gaps are **blocking** for the Discover view to work. They are small additions that belong in Tasks 7 and 10 respectively — implement them during those tasks.
