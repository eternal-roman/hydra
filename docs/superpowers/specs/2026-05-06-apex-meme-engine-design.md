# APEX Meme Engine — Design Spec

**Date:** 2026-05-06  
**Status:** Approved for implementation  
**Scope:** Standalone meme/competition-token trading engine + dashboard tab  
**Isolation guarantee:** Zero changes to hydra_engine.py, hydra_agent.py, hydra_brain.py, hydra_pair_registry.py, hydra_quant_rules.py, or any existing state file

---

## 1. Problem Statement

Kraken periodically runs volume competitions on meme/emerging tokens (e.g., PLAY/USD ending May 7 2026). These competitions create extraordinary volume and volatility windows (PLAY: 25M token/day, +66% intraday) that are profitable with a fast momentum strategy — but incompatible with Hydra's existing architecture (limit post-only, 50-candle warmup, derivatives signals, SOL/BTC triangle focus).

APEX is a purpose-built, fully isolated engine that activates during competition windows, trades any configurable token with taker orders and a 5-min OBI momentum strategy, and shuts down cleanly without affecting the triangle.

---

## 2. Architecture & Isolation Boundary

### New files (complete scope)

| File | Purpose |
|---|---|
| `hydra_meme_agent.py` | Standalone process; all engine logic inline; WS broadcast on port 8766 |
| `dashboard/src/MemeTab.jsx` | New React component; connects to ws://localhost:8766 |
| `start_meme.bat` | `python hydra_meme_agent.py --pair PLAY/USD` |

### Modified files (minimal)

| File | Change |
|---|---|
| `dashboard/src/App.jsx` | Add `MEME` tab to TabSwitcher; render `<MemeTab>` when active; second WS connection on port 8766 |

### New state files (all gitignored, no overlap with triangle)

| File | Contents |
|---|---|
| `hydra_meme_session.json` | Current session: candle buffer, open position, engine state |
| `hydra_meme_journal.json` | All closed trades: entry/exit/reason/P&L |
| `hydra_meme_watchlist.json` | Token list + 7-day rolling volume baselines |

### Untouched (zero changes)

`hydra_engine.py`, `hydra_agent.py`, `hydra_brain.py`, `hydra_quant_rules.py`, `hydra_companions/`, `hydra_pair_registry.py`, `hydra_config.py`, `hydra_session_snapshot.json`, `hydra_order_journal.json`, all existing tests.

### Circuit breaker independence

APEX has its own **−$30 daily loss cap** that halts APEX only. Hydra's 15% triangle circuit breaker is completely independent. If APEX blows up on a reversal, the triangle keeps running.

---

## 3. hydra_meme_agent.py — Internal Structure

Five components, all inline in one file (~500 lines):

```
MemeAgent
  ├── CompetitionDetector      polls watchlist every 15 min via `kraken ticker`
  ├── CandleAggregator         Kraken WS ohlc-5 subscription → 5-min bars (20 bars buffer)
  ├── SignalEngine              evaluates 5 entry gates + 6 exit triggers per bar close
  ├── OBIPoller                 polls `kraken orderbook` every 10s; caches last value
  └── MemeExecutor              places taker limit orders via WSL kraken CLI; tracks position
```

### CLI arguments

```
python hydra_meme_agent.py --pair PLAY/USD [--position-size 600] [--daily-cap 30]
```

`--pair` is the only required argument. Swap it to trade any competition token.

### WebSocket broadcast (port 8766)

Emits JSON messages on each significant state change:

| Message type | Trigger |
|---|---|
| `competition_alert` | Anomaly detected (volume > 5× baseline for new token) |
| `signal_state` | Every 5-min bar close: all gate values, current signal |
| `order_placed` | On taker order submission |
| `position_update` | Every 10s while position open: current price, unrealised P&L |
| `trade_closed` | On exit: entry, exit, net P&L after fees, exit reason |
| `session_stats` | After every closed trade: totals, win rate, daily P&L |
| `engine_halted` | Daily cap hit or fatal error |

---

## 4. Signal Engine

### Candle timeframe

**5-minute primary** — empirically validated on PLAY/USD OHLC data:
- Avg 5-min range: 2.64% (vs 0.88% on 1-min)
- 73% of 5-min candles exceed 2% range (well above 0.80% fee drag)
- Volume-spike 5-min candles average 3.41% range
- 1-min momentum persistence: 47% (coin flip — disqualified)

CandleAggregator subscribes to Kraken's public WebSocket (`wss://ws.kraken.com`, `ohlc-5` channel). Kraken streams the running candle on every trade; a bar is confirmed closed when the `etime` (end-time) in the update advances to the next 5-min boundary. This eliminates the ~60s lag of REST polling and lets entry fire at bar close, not up to a minute later. Buffer holds the last 20 closed bars.

WebSocket reconnect: exponential backoff (5s, 10s, 20s, capped at 60s) on disconnect. Engine stays in warmup/hold state during reconnect; no trades placed without a live data feed.

Warmup: **15 bars** (75 minutes from cold start). Engine holds and accumulates data; no trades during warmup.

### Entry gates (all 5 must be true simultaneously)

| Gate | Condition | Rationale |
|---|---|---|
| **Volume spike** | Current 5-min volume > 1.8× EMA(volume, 10) | Active accumulation, not dead market |
| **OBI threshold** | (bid_depth₅ - ask_depth₅) / (bid_depth₅ + ask_depth₅) > +0.20 | Directional book pressure — buy side dominating |
| **VWAP alignment** | Close > engine-start VWAP | Trading with session bias |
| **RSI window** | RSI(9) between 45 and 78 | Not chasing overbought; not catching falling knife |
| **Ask wall clear** | Top-3 ask levels < $500 total USD | Room for price to run without hitting resistance |

**OBI** (`bid_depth₅`/`ask_depth₅` = cumulative USD depth across top-5 order book levels, fetched from `kraken orderbook --count 5`) is the strongest signal: near-linear relationship to short-horizon price changes (Cont, Kukanov & Stoikov 2010). Validated live during research — OBI = −0.22 called the PLAY pullback from $0.1704 to $0.1619 (-4.9%) during this session.

**VWAP** resets when the engine process starts (not at midnight). Crypto trades 24/7; "session" is defined as the engine's active run. VWAP = cumulative(price × volume) / cumulative(volume) across all bars since start. Useful from warmup bar 1 onward.

RSI uses **Wilder EMA** (consistent with hydra_engine.py convention). Overbought threshold set to **78** (not 70) — meme tokens sustain elevated RSI far longer than the SOL/BTC pairs the main engine was calibrated for.

### Exit triggers (first to fire wins)

Exit checks run on **two cadences**:

**Every 10 seconds** (on OBI price tick — uses best bid/ask from `kraken orderbook`):
| Exit | Trigger | Notes |
|---|---|---|
| **Profit target** | Mid-price ≥ fill_price × 1.025 | ~$10.20 net on $600 position after 0.80% fees |
| **Hard stop** | Mid-price ≤ fill_price × 0.987 | 1:1.9 R/R ratio; checked sub-candle to avoid riding through |
| **Book fade** | OBI drops below −0.20 | Sellers taking control |

**At 5-min bar close** (on each new closed candle):
| Exit | Trigger | Notes |
|---|---|---|
| **RSI exhaust** | RSI(9) > 82 | Momentum spent |
| **Time stop** | 3rd 5-min candle closes without target hit | Meme cycles have short half-lives |
| **Volume death** | Current candle volume < 0.4× baseline EMA | Move has stalled |

Monitoring profit target and hard stop at bar close only would expose a 5-minute blind window. The 10s OBI poll already fetches bid/ask, so stop/target checks add zero extra API calls.

### Position sizing

- **Fixed: $600 per trade** (not Kelly — insufficient history for PLAY)
- At 2.5% move: $15 gross − $4.80 fees (0.40% taker × 2) = **$10.20 net**
- At 3.5% move: $21 gross − $4.80 fees = **$16.20 net**
- Stop hit: −$7.80 − $4.80 fees = **−$12.60 max loss**
- Max 1 open position at a time

### Order execution

Taker limit orders placed slightly inside the book to guarantee fill while avoiding runaway gaps:
- **BUY:** limit at `ask + 0.05%` (crosses spread, fills immediately)
- **SELL:** limit at `bid − 0.05%` (crosses spread, fills immediately)

Kraken CLI via WSL (consistent with existing Hydra pattern):
```
wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken order ..."
```

---

## 5. Competition Detector

Runs in a background thread inside `hydra_meme_agent.py`. Also available as a standalone scan (powers the Discover tab).

### Detection algorithm

Every 15 minutes:
1. Load `hydra_meme_watchlist.json`. On first run (file absent), bootstrap from a hardcoded seed list of ~30 active Kraken spot pairs (top pairs by typical market cap: BTC, ETH, SOL, XRP, ADA, DOT, LINK, MATIC, AVAX, ATOM, NEAR, FIL, APT, OP, ARB, INJ, TIA, SEI, PYTH, WIF, POPCAT, BONK, PEPE, PLAY, LION, and others). Seed entries use `baseline_volume_7d: null` and receive their first real baseline on the initial poll.
2. For each token, fetch 24h volume via `kraken ticker <PAIR>`. Enforce **2s minimum gap between ticker calls** (same floor as Hydra) — a full 30-token scan takes ~60s.
3. Compare to stored 7-day rolling baseline.
4. If `current_volume / baseline_volume > 5.0` → emit `competition_alert` via WS.
5. Update baseline with EMA: `new_baseline = α × current_volume + (1 − α) × old_baseline`, α = 1/7 (7-day rolling average). Skip update if `baseline_volume_7d` is null (first observation — set directly).

### Alert suppression

- Same token suppressed for **2 hours** after user dismisses the modal
- Alert fires once per session per token; re-fires if ratio increases another 2×

### Capital check

Before enabling the opt-in toggle for a token, the Discover tab queries the agent for available balance. Toggle is disabled if `available_balance < position_size ($600)`.

---

## 6. Dashboard — Tab Structure

### Main tab bar (App.jsx TabSwitcher)

```
LIVE  |  MEME  |  SETTINGS
```

RESEARCH and THESIS tabs are removed from the bar. Their existing functionality (backtesting, thesis management) is accessible via the LIVE view's internal controls — the tabs were Hydra-only features not relevant to the APEX workflow.

> **Note for implementation:** The ResearchTab and ThesisPanel components remain in App.jsx and are not deleted — they are simply removed from the TabSwitcher array. If the user wants them back later, it's a one-line change.

### MEME tab — sub-navigation

Two views toggle via an underline sub-nav below the main tab bar:

```
⚡ Trading  |  🔍 Discover
```

Default view on first open: **Discover** (so user sees what's available before the engine starts).  
Default view when engine is running: **Trading**.

### MemeTab.jsx components

**Trading view:**
- Control row: pair name + price + 24h change + spread + competition deadline badge + pair selector dropdown + STOP button
- 3-column grid:
  - Left: 5-min candle chart (SVG) + volume histogram + OBI gauge (−1 to +1 bar)
  - Middle: 5 entry gate indicators (green/red dot + value) + BUY/HOLD/SELL signal banner + exit watch levels
  - Right: Open position panel (P&L, hold time, progress bar to target) + session stats (net P&L, win rate, trades, daily cap remaining)
- Trade log: TIME / ENTRY / EXIT / NET P&L / REASON / HOLD columns

**Discover view:**
- Header: scan status (last scan time, next scan countdown) + "Scan Now" button
- Competition table columns: Token / Current Vol / 7d Baseline / Anomaly ratio / Est. Deadline / Capital needed / Trade toggle
  - Ratio pills: red (>7×), amber (4–7×), blue (3–4×)
  - Capital bar: green = sufficient, red = insufficient; toggle disabled when insufficient
  - Only one token active at a time (activating a new one prompts to stop current)
- Capital summary panel: Available balance / Locked in active token / Daily loss cap / Used today

**Competition opt-in modal:**
- Fires automatically when `competition_alert` WS message received
- Shows: token, current vol vs baseline, anomaly ratio, estimated deadline, strategy parameters
- Buttons: [Start APEX Engine] / [Dismiss (2h)]
- Does not fire if engine already running on a different token (shows "engine busy" state instead)

---

## 7. State Management

### hydra_meme_session.json

```json
{
  "pair": "PLAY/USD",
  "engine_state": "running",
  "candle_buffer": [...],
  "open_position": {
    "entry_price": 0.1581,
    "qty": 3662,
    "notional_usd": 600.0,
    "entry_ts": 1778107200,
    "candles_held": 1
  },
  "session_pnl": 23.40,
  "daily_pnl": 23.40,
  "trade_count": 4
}
```

Written atomically (`tmp → os.replace`) on every state change.

### hydra_meme_watchlist.json

```json
{
  "tokens": [
    {
      "pair": "PLAY/USD",
      "baseline_volume_7d": 3200000,
      "last_updated": 1778100000,
      "competition_type": "volume",
      "competition_type_confirmed": false,
      "alert_suppressed_until": null
    },
    {
      "pair": "LION/USD",
      "baseline_volume_7d": null,
      "last_updated": null,
      "competition_type": null,
      "competition_type_confirmed": false,
      "alert_suppressed_until": null
    }
  ],
  "last_scan": 1778107200
}
```

`competition_type_confirmed: true` is set when the user manually confirms via the modal. `alert_suppressed_until` holds a Unix timestamp when Dismiss (2h) is clicked.

---

## 8. Risk Controls

| Control | Value | Scope |
|---|---|---|
| Daily loss cap | −$30 | Halts APEX for session; triangle unaffected |
| Per-trade stop | −1.3% | Hard stop, taker exit |
| Max position | 1 open at a time | No pyramiding |
| Max hold | 3 × 5-min candles (15 min) | Time stop |
| Taker slippage cap | BUY limit ≤ `ask × 1.001` at order time; SELL limit ≥ `bid × 0.999` | Prevents runaway fill if book moves >0.1% between signal and execution |
| Capital gate | `available_balance ≥ $600` | Toggle disabled below threshold |
| APEX kill switch | `HYDRA_APEX_DISABLED=1` env flag | Instant halt, no redeploy needed |

---

## 9. Revised Probability Estimates (with APEX)

| Target | During active competition | After competition ends |
|---|---|---|
| $25 | ~52% | ~20% |
| $50 | ~30% | ~10% |
| $100 | ~15% | ~5% |

Estimates based on: 5-min avg range 2.64%, volume-spike avg 3.41%, OBI validation, 0.40% taker fee structure. Competition windows (high volume, thin books) are the primary activation condition.

---

## 10. New Files Summary

```
hydra_meme_agent.py              ~500 lines, standalone
dashboard/src/MemeTab.jsx        ~350 lines, new component  
start_meme.bat                   3 lines
docs/superpowers/specs/
  2026-05-06-apex-meme-engine-design.md   this file
```

Modified:
```
dashboard/src/App.jsx            +~30 lines (tab + WS + MemeTab render)
.gitignore                       +3 lines (meme state files + .superpowers/)
```

---

## 11. Competition Intelligence

When a competition alert fires, APEX runs a one-time intelligence pass to answer three questions: what type of competition is it, what tier is realistically achievable, and is the prize worth the trading cost.

### Competition type detection

Kraken has no public competitions API, and the Python engine has no external search capability. APEX infers type entirely from the **volume-pattern heuristic**:

- Anomaly ratio >5× sustained >12h with a smooth ramp → likely **volume competition** (reward based on total volume traded)
- Anomaly ratio spiky with heavy reversals, short bursts → likely **price prediction / P&L competition** (harder to act on)
- Anomaly ratio moderate (3–5×) with thin spreads → possibly **fee rebate promo**

Default assumption when pattern is ambiguous: **volume competition**.

The Discover tab modal displays an "Unverified — check Kraken promotions page" notice alongside the heuristic result, prompting the user to confirm manually before committing capital. If the user has previously confirmed a type (stored in the watchlist entry under `competition_type`), that value is displayed instead.

### Tier probability estimate

For a **volume competition** (most common Kraken format), the metric is total USD value traded. APEX computes:

| Input | Source |
|---|---|
| Market daily volume (USD) | `kraken ticker` — 24h volume × price |
| User projected daily volume | `(expected_trades_per_day × position_size × 2)` — 2 for round trip |
| User volume share | `user_volume / market_volume` |
| Competition participants (est.) | Heuristic: 1 participant per 0.2% market share → estimate total pool |

Example with PLAY/USD (real numbers from this session):
- Market: 24.9M PLAY × $0.165 = **$4.1M/day**
- User at 5 trades/day × $600 × 2 = **$6,000/day**
- Share: `$6,000 / $4,100,000` = **0.15%**
- Estimated participant pool: ~500 traders
- User rank estimate: **top 30–40%** (mid-tier, likely below prize threshold for top competitions)

This is shown honestly in the modal with a tier gauge — so the user knows upfront whether prize-hunting is viable vs just riding the volatility for P&L.

### Tier display (modal + Discover tab)

```
Competition type:  VOLUME  (heuristic — verify on Kraken promotions page)
Prize tiers:       Not confirmed — showing estimate
─────────────────────────────────────────────────
Your projected volume/day:  $6,000  (5 trades × $600 × 2)
Market total volume/day:    $4.1M
Your share:                 0.15%
─────────────────────────────────────────────────
Tier estimate:  ⚠  Top 30–40%  (mid-field)
Next tier at:   ~$18,000/day  →  15 trades or $1,800 position
─────────────────────────────────────────────────
Verdict:  Trading for P&L is the primary edge.
          Prize tier achievable with larger position.
```

The `position_size` and `daily_cap` CLI args are surfaced here so the user can see the lever to pull for better tier positioning.

### Position size / tier lever

The Discover tab shows a **"Tier lever"** slider under each competition row:

- Default position: $600 (current config)
- Slider range: $600 → $3,000 (capped by available balance)
- As slider moves: re-computes projected daily volume, share %, and tier estimate in real time
- Changing the slider pre-fills the `--position-size` arg when starting the engine

This makes the trade-off explicit: bigger position = better tier, but larger per-trade risk. User decides.

---

## 12. Out of Scope

- No AI brain (no Claude/Grok calls) — pure signal rules, keeps latency low
- No Hydra pair registry changes — APEX resolves pair metadata directly from `kraken pairs`
- No backtest integration — APEX is live-only; backtest tab unchanged
- No derivatives signals (PLAY has no Kraken Futures perp)
- No multi-position (one token active at a time)
- No short selling (spot-only, consistent with Hydra)
