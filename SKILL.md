<!-- CLAUDE.md is the current authoritative reference. This file is the
     agent-readable trading spec (frontmatter + English narrative);
     formulas and pair lists are kept current, but architecture detail
     (AI brain, self-tuning, reconciler, snapshots, companions) lives in
     CLAUDE.md, not here. -->
---
name: hydra-regime-trader
description: >
  HYDRA (Hyper-adaptive Dynamic Regime-switching Universal Agent) is an autonomous
  crypto trading agent for Kraken CLI that detects market regimes and switches between
  four strategies: Momentum, Mean Reversion, Grid, and Defensive, all gated by a
  daily trend-ensemble overlay (v2.28). Trades BTC/USD, ETH/USD, and ZEC/USD by
  default (v2.29 — independent pairs, no triangle; explicit SOL pairs restore the
  legacy triangle with its SOL/BTC bridge signal-only; USDC/USDT
  variants and --pairs auto portfolio discovery available)
  using limit post-only orders. Use when: (1) running a live
  trading session via Kraken CLI (WSL), (2) analyzing current market regime from OHLC
  data, (3) generating trade signals with quarter-Kelly position sizing, (4) monitoring
  performance via the React dashboard. Requires kraken-cli installed in WSL. NOT for:
  non-Kraken exchanges, DeFi/on-chain trading, or strategies outside the HYDRA framework.
---

# HYDRA — Regime-Adaptive Trading Agent for Kraken CLI

## Overview

HYDRA selects a strategy from a four-regime matrix. That is a **design**, not a
profit claim. Default **hold-through** + **daily trend overlay**
(`HYDRA_HOLD_THROUGH=0` / `HYDRA_TREND_OVERLAY=0` to disable) are capital-
preservation rails; sqlite replays still show absolute losses on some windows.
**Research surfaces** (S3 bounce QI / optional shadow; heartbeat P(up) display)
never place orders — see `CLAUDE.md` product thesis and
`heartbeat/HONEST_FINDINGS.md`.

Matrix:

| Detected Regime | Selected Strategy | Logic |
|-----------------|-------------------|-------|
| TREND_UP        | MOMENTUM          | Ride the wave — MACD positive, price > EMA20, RSI 30–70 |
| TREND_DOWN      | DEFENSIVE         | Reduce exposure — sell rallies, only buy extreme oversold |
| RANGING         | MEAN_REVERSION    | Buy at lower Bollinger Band, sell at upper |
| VOLATILE        | GRID              | Split orders across Bollinger Band zones |

## Prerequisites

```bash
# Install Kraken CLI
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

# Verify installation
kraken --version

# For live trading only (paper trading needs no keys):
kraken setup
```

## Core Workflow

### Phase 1: Collect Market Data

```bash
# Get current ticker
kraken ticker BTC/USD -o json

# Product default: 60m OHLC (v2.28+; rails + friction calibrated on 1h)
kraken ohlc BTC/USD --interval 60 -o json

# Research-only shorter bars (off-calibration for hold-through / friction)
# kraken ohlc BTC/USD --interval 15 -o json

# Stream live ticks via WebSocket
kraken ws ticker BTC/USD -o json
```

### Phase 2: Detect Regime

Using the OHLC data, compute:
1. **EMA(20)** and **EMA(50)** — trend direction
2. **ATR(14)** — volatility measurement
3. **Bollinger Bands(20, 2)** — band width for regime classification

**Regime Rules:**
- ATR% > `volatile_atr_mult` (1.8) × median ATR% OR BB width > `volatile_bb_mult` (1.8) × median BB width → **VOLATILE** *(adaptive per-asset; floor 1.5% ATR, 0.03 BB width)*
- `EMA20 > EMA50 * 1.005` AND `price > EMA20` → **TREND_UP**
- `EMA20 < EMA50 * 0.995` AND `price < EMA20` → **TREND_DOWN**
- Otherwise → **RANGING**

### Phase 3: Generate Signal

Each strategy produces a signal: **BUY**, **SELL**, or **HOLD** with a confidence score (0–1).

**MOMENTUM Strategy:**
- BUY when: RSI 30–70, MACD histogram > noise floor, price > BB middle. Confidence scales with MACD strength.
- SELL when: symmetric stack (RSI in band, hist negative, price < BB mid) **or** extreme RSI > upper+15 (default >85).

**MEAN_REVERSION Strategy:**
- BUY when: price ≤ BB lower AND RSI < 35. Confidence scales with distance from middle band.
- SELL when: price ≥ BB upper AND RSI > 65.

**GRID Strategy:**
- Divide BB range into 5 zones. BUY in bottom zone, SELL in top zone.

**DEFENSIVE Strategy:**
- BUY only when RSI < 25 (extreme oversold); conf capped 0.75.
- SELL when RSI > 40 (reduce exposure). Conf floors at **0.65** from RSI 40 (maps to executable).
- **Exit guarantee:** SELL does **not** require min_confidence (entries still do). Soft exits flatten.

### Phase 4: Size Position (Kelly Criterion)

```
# Excess-over-threshold Kelly (not conf*2-1):
t = (confidence - min_confidence) / (1 - min_confidence)   # 0 at floor, 1 at 100%
edge = 0.10 + 0.90 * t                                      # small edge at 0.65, full at 1.0
kelly = edge * multiplier                                   # 0.25 conservative, 0.50 competition
position_value = kelly * balance
position_size = position_value / current_price
# Then clamp notional/equity ≤ max_position_pct AFTER any size_multiplier
```

**Sizing modes:**
- **Conservative** (default): quarter-Kelly (0.25), min confidence 65%, max position 30%
- **Competition** (`--mode competition`): half-Kelly (0.50), min confidence 65%, max position 40%

**Hard limits:**
- Minimum trade cost: pair-aware (Kraken costmin — 0.5 USDC, 0.00002 BTC)
- Minimum order size: pair-aware (Kraken ordermin — 0.02 SOL, 0.00005 BTC)
- Sell-side dust: positions below ordermin are **written off** (not left as permanent bags)
- Confidence threshold for **entries**: 0.65 (both modes); **exits ignore min_confidence**
- Gross inventory + size_mult cannot exceed max_position_pct of equity

### Phase 5: Execute Trade

```bash
# ALWAYS set dead man's switch first:
kraken order cancel-after 60

# Limit post-only orders (maker, sit on book, never cross spread):
# BUY at bid price:
kraken order buy BTC/USD 0.0001 --type limit --price 65000.0 --oflags post --yes
# SELL at ask price:
kraken order sell BTC/USD 0.0001 --type limit --price 65100.0 --oflags post --yes

# Validate without executing:
kraken order buy BTC/USD 0.0001 --type limit --price 65000.0 --oflags post --validate

# Cancel all open orders:
kraken order cancel-all --yes
```

### Phase 6: Monitor & Report

```bash
# Check open orders
kraken open-orders -o json

# Check trade history
kraken trades-history -o json

# Check balance
kraken balance -o json

# Check closed orders
kraken closed-orders -o json
```

## Agent Loop (Pseudocode)

```
INITIALIZE paper session
SET assets = ["BTC/USD", "ETH/USD", "ZEC/USD"]   # v2.29 defaults
SET candle_interval = 60 minutes                 # product default (v2.28+)
SET tick_interval = 60 seconds                   # agent loop cadence
SET max_position_pct = 0.30   # 0.40 in competition mode
SET min_confidence = 0.65     # quality filter — only ≥15% Kelly edge

LOOP every {tick_interval}:
  FOR each asset in assets:
    1. FETCH ohlc / WS candle at candle_interval (60m default)
    2. PARSE candles into arrays: opens, highs, lows, closes
    3. COMPUTE indicators: EMA20, EMA50, RSI14 (Wilder), ATR14 (Wilder), BB(20,2), MACD(12,26,9)
    4. DETECT regime using indicator values
    5. SELECT strategy from regime; apply hold-through + daily trend overlay
    6. GENERATE signal (action, confidence, reason); friction gate on BUY
    7. IF signal.action != HOLD AND signal.confidence >= min_confidence:
         a. COMPUTE position size via excess-over-threshold Kelly
         b. CHECK balance (per-quote pool in live)
         c. VALIDATE trade size against limits
         d. EXECUTE: limit post-only (paper or live)
         e. LOG trade with timestamp, price, reason, confidence, strategy
    8. LOG current state: regime, strategy, signal, position, equity (+ heartbeat P(up) if status file present)

  COMPUTE portfolio metrics:
    - Total equity = cash + sum(position_value)
    - P&L % = (equity - initial) / initial * 100
    - Max drawdown = max historical peak-to-trough
    - Win rate = wins / (wins + losses)
    - Sharpe estimate from rolling returns

  PRINT status summary

  IF max_drawdown > 15%:
    BLOCK new BUYs only; still allow SELL to flatten
END LOOP
```

## Risk Management Rules

1. **Circuit Breaker (per engine)**: Halt **new BUYs** if max drawdown exceeds 15%; **SELL still allowed** (flatten inventory). Portfolio-level 15% max DD also sticky-blocks BUYs. (Not a full session freeze of exits.)
2. **Dead Man's Switch**: Always run `kraken order cancel-after 60` before live orders
3. **Position Limits**: No single position notional > max_position_pct of equity (30%/40%), applied after brain size_multiplier
4. **Trade Threshold**: Entries only when confidence ≥ 0.65; exits do not use this floor
5. **Minimum Size**: Enforce Kraken ordermin per asset + costmin per quote currency; dust below ordermin written off
6. **Regime Warmup**: Require 50+ candles before generating **any** non-HOLD signal (`SignalGenerator.WARMUP_CANDLES`)
7. **Rate Limiting**: Respect Kraken API limits — minimum 2s between requests
8. **Fill true-up**: Engine books exchange `avg_fill_price` on FILLED/PARTIAL (not candle close)
9. **Quant R2**: Extreme negative funding force_holds **BUY** (bounce-chase), never spot **SELL** (long close)
10. **Friction gate (entries)**: BUY skipped when strategy-implied move cannot clear ~2× round-trip friction (SKIP, not BLOCK). Kill: `HYDRA_FRICTION_GATE_DISABLED=1`
11. **Hold-through (default on)**: TREND_UP BUY ≥0.65; flatten TREND_DOWN; ride mid-UP except extreme overbought. Kill: `HYDRA_HOLD_THROUGH=0`. See `docs/HOLD_THROUGH.md`.
12. **QFE (R11)**: Exit-only, profit-only SELL through force_hold when engine already wants SELL, unrealized P&L ≥ `QFE_MIN_PROFIT_PCT` (1.0%), and no deterministic squeeze catalyst; never opens a position; LLM `crowded_short` alone does not veto

## Indicator Reference

| Indicator | Formula | Purpose |
|-----------|---------|---------|
| EMA(n)    | close[i] * k + EMA[i-1] * (1-k), k = 2/(n+1) | Trend direction |
| RSI(14)   | 100 - 100/(1 + avg_gain/avg_loss) | Overbought/oversold |
| ATR(14)   | Wilder's exponential smoothing of True Range | Volatility measure |
| BB(20,2)  | middle ± 2*stddev(close, 20) | Price bands & regime |
| MACD      | EMA(12) - EMA(26), signal = EMA(9) of MACD | Momentum |

## Performance Metrics to Track

- **Net P&L** (realized + unrealized)
- **Sharpe Ratio** (annualized from tick returns)
- **Max Drawdown** (peak-to-trough %)
- **Win Rate** (winning trades / total trades)
- **Profit Factor** (gross profit / gross loss)
- **Trades per Hour** (activity level)
- **Regime Detection Accuracy** (compare detected vs. retrospective)

## Example Claude Code Session

```
> Install kraken-cli, then run HYDRA in paper mode on BTC/USD for 10 minutes.
> Use 60-minute OHLC candles (product default). Start with $10,000 paper balance.
> Print a status update every 60 seconds showing:
>   - Current regime and strategy
>   - Signal (action, confidence, reason)
>   - Position and unrealized P&L
>   - Total equity and drawdown
>   - Heartbeat P(up) if status surface is live
> At the end, print a full performance report with all metrics.
```

## File Structure

```
hydra/
├── SKILL.md              # This file — trading spec (agent-readable)
├── README.md / CLAUDE.md # Overview + authoritative invariants index
├── hydra_engine.py       # Indicators, regime, signals, sizing, hold-through
├── hydra_agent.py        # Live loop (Kraken CLI, WS, execution, journal)
├── hydra_brain.py        # Optional AI (Analyst + RM + Grok)
├── hydra_quant_rules.py  # R1–R11 deterministic rules + QFE
├── hydra_flywheel.py     # Paper allocator only (no live orders)
├── tools/                # Research: history, flywheel validation, causal retest
├── tests/                # pytest + live-execution harness
└── dashboard/            # React + Vite UI
```

## License

MIT
