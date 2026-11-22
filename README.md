# HYDRA

[![CI](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml/badge.svg)](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**Regime-adaptive Kraken spot trading agent.** It detects trending, ranging and volatile markets, switches among Momentum, Mean Reversion, Grid and Defensive strategies, and places **limit post-only** orders only. A live React dashboard is included.

> **Not financial advice.** Experimental research software; crypto trading can lose money, and neither backtests nor rails are guarantees. The live engine path is **capital-preservation rails** (hold-through, daily trend overlay, friction, a 15% BUY-only breaker), not a proven growth-alpha claim. Research surfaces (the S3 shadow book, heartbeat P(up)) display and shadow only; they never place orders.

## Highlights

- **Evidence-gated daily trend sleeve**, the strategy with the best evidence here. A pair trades it only after a pre-registered gate passes on **your own** history; see [What Hydra trades](#what-hydra-trades-and-how-to-prove-it).
- **Regime switching** on pure-Python indicators (Wilder RSI/ATR, Bollinger, MACD, EMAs).
- **Pairs:** `BTC/USD`, `ETH/USD`, `ZEC/USD` by default, each independent. `--pairs auto` seeds those cores in your funded stable quote and adds a satellite per other held Kraken asset. Explicit SOL pairs restore the legacy triangle, with the bridge signal-only.
- **Hold-through rails** (default on, `HYDRA_HOLD_THROUGH=0` off): TREND_UP entries at ≥0.65 confidence, flatten downs, ride mid-trends. Defense and capture discipline, not a profit claim.
- **Daily trend-ensemble overlay** (default on, `HYDRA_TREND_OVERLAY=0` off): SMA200, EMA20×100 and Donchian 55 on daily closes gate entries, flatten on a regime flip and size by volatility target. Gated on real tape (`.hydra-flywheel/trend_overlay_gate.json`).
- **AI quant pipeline** (optional): Market Quant, Risk Manager, Grok, and the deterministic R1–R11 rules.
- **Research stack:** backtests, walk-forward, a paper flywheel (never live orders), the S3 bounce surface (shadow with `HYDRA_S3_STRATEGY=1`) and the heartbeat P(up) sparkline (`HYDRA_HEARTBEAT_SURFACE=0` off). None of them places orders.
- **Companions:** chat and proposals; live execution is opt-in.

## Safety (non-negotiable)

| Rule | Detail |
|------|--------|
| Spot only | No futures, margin or options orders |
| Limit post-only | `--type limit --oflags post`, never market |
| Rate limit | ≥ 2s between Kraken REST calls |
| Drawdown | 15% → **no new BUYs**; inventory SELL still allowed |
| Companion live | `HYDRA_COMPANION_LIVE_EXECUTION` default **off** |
| Hold-through | `HYDRA_HOLD_THROUGH` default **on** |

## Quick start

```bash
git clone https://github.com/eternal-roman/hydra.git && cd hydra
pip install -r requirements.txt

# Offline, no keys, no WSL: synthetic candles, paper fills, WS :8765
python hydra_agent.py --demo --duration 30 --balance 1000
python hydra_engine.py              # engine synthetic walk (stdlib only)
python hydra_backtest.py            # backtest on a synthetic series
python hydra_flywheel.py --report   # paper flywheel report
```

`--demo` never calls kraken-cli or loads API keys.

**Paper and live** need WSL Ubuntu with [kraken-cli](https://github.com/krakenfx/kraken-cli) 0.4.1+ (paper uses public OHLC and `kraken paper`, no keys). Live also needs Kraken API keys with spot trade and **no withdraw**. The dashboard needs Node.js 18+.

```bash
cp .env.example .env                    # keys for live only; never commit .env
cd dashboard && npm install && cd ..

python hydra_agent.py --mode competition --paper              # paper
python hydra_agent.py --balance 100                           # live: BTC/USD, ETH/USD, ZEC/USD
python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD         # live: legacy SOL triangle
cd dashboard && npm run dev             # http://localhost:3000 → ws://127.0.0.1:8765

start_all.bat                           # Windows: agent + dashboard
start_hydra.bat                         # production: --pairs auto --mode competition --resume
```

### What Hydra trades, and how to prove it

Each pair runs one of two strategies, decided by evidence at start-up:

| Strategy | When | What it does |
|---|---|---|
| Daily trend sleeve | Its pre-registered gate PASSED, significantly, on your data for that asset | Long-or-flat on the daily trend ensemble (SMA200, EMA20/100, Donchian 55/20) on completed closes, vol-targeted, re-sized every 30 days; no LLM or rule vetoes |
| 1h rails engine | Otherwise (the default with no gate result) | Capital preservation; in practice it almost never trades |

```bash
run_strategy_gate.bat                             # Windows: refresh history, run the gate, show the result
# or step by step:
python -m tools.refresh_history                   # update hydra_history.sqlite
python tools/trend_sleeve_gate.py --engine        # the pre-registered test (minutes)
python -m hydra_strategy_gate                     # what Hydra will trade, per pair
```

Restart the agent to apply; `HYDRA_TREND_SLEEVE=1` / `0` overrides the evidence. Re-run the gate quarterly: results older than 180 days switch `auto` back off. The gate needs 5+ years of daily history per asset. Build the store once from Kraken's trade-history archive (`python -m tools.bootstrap_history --zip <archive.zip>`), or every pair reports `INSUFFICIENT_DATA` and stays on the 1h engine. Every pair card shows its trade plan: the daily close that flips it and where the breaker sits. The rules and the evidence behind each are in [`docs/TRADE_PLAN.md`](docs/TRADE_PLAN.md). It is not a money machine: the sleeve earns when crypto trends up and holds cash otherwise.

### Config

| Source | Purpose |
|--------|---------|
| `.env` / `.env.example` | API keys and `HYDRA_*` kill switches ([`docs/REFERENCE.md`](docs/REFERENCE.md#env-flags-kill-switches-and-opt-ins)) |
| `--pairs` / `--quote` / `HYDRA_QUOTE` | Pairs. `--quote` only changes the `--pairs auto` fallback quote |
| `--demo` / `--paper` / `--resume` / `--mode` | Offline demo, paper, snapshot resume, Kelly mode |
| `python hydra_auth.py create-user` / `reset-password` | Dashboard login (create never overwrites) |

## Architecture

```
Candle/Ticker WS → per pair, one of:
   trend sleeve (gate PASS): completed daily closes → ensemble → long/flat → vol-target size
   1h rails engine:          indicators → regime → strategy signal
        → hold-through + daily trend overlay (default on)
        → friction (entries) → 15% CB (BUY only)
        → (optional) AI brain + R1–R11 / QFE
        → Kelly size → limit post-only (kraken-cli / WSL)
        → ExecutionStream / journal / snapshot → dashboard WS :8765
        ↳ parallel: S3 QI ± shadow | heartbeat P(up) surface  (never orders)
```

Backtests replay `HydraEngine` (and the coordinator); the AI brain is not on the backtest path. File-by-file roles: [`docs/REFERENCE.md`](docs/REFERENCE.md#modules).

## Testing

CI runs on every PR to `main`: Python 3.11 and 3.12, the dashboard build, and the smoke and mock harness.

```bash
python -m pytest tests/ -q
python tests/live_harness/harness.py --mode mock    # mandatory for placement changes
cd dashboard && npm run build
```

Harness modes are `smoke`, `mock`, `validate` and `live` (explicit flag); see [`tests/live_harness/README.md`](tests/live_harness/README.md).

## Security

- Report vulnerabilities privately via [Security Advisories](https://github.com/eternal-roman/hydra/security/advisories/new); see [`SECURITY.md`](SECURITY.md).
- Secrets stay local and gitignored: `.env`, `hydra_*_token.json`, `hydra_auth_state.json`, `*.db`.
- The dashboard WS binds loopback and authenticates **both directions**: a socket gets no account state until it presents the per-process token (or a JWT in production mode).
- Kraken credentials reach WSL through `WSLENV`, never through `wsl` argv.

## Docs

| Doc | Contents |
|-----|----------|
| [`CLAUDE.md`](CLAUDE.md) | Agent rules and cross-cutting invariants |
| [`SKILL.md`](SKILL.md) | Trading formulas and risk rules |
| [`docs/REFERENCE.md`](docs/REFERENCE.md) | Modules, env flags, state-file owners |
| [`docs/TRADE_PLAN.md`](docs/TRADE_PLAN.md) | The plan each pair trades (trend sleeve or hold-through rails), the evidence behind each rule, the per-session routine |
| [`docs/BACKTEST.md`](docs/BACKTEST.md) | Backtest runbook and design archive (code wins on defaults) |
| [`docs/COMPANION_SPEC.md`](docs/COMPANION_SPEC.md) | Companion system |
| [`heartbeat/README.md`](heartbeat/README.md) · [`HONEST_FINDINGS.md`](heartbeat/HONEST_FINDINGS.md) | Order-flow posterior and the evidence ledger |
| [`research/`](research/) | Papers and promoted study data |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| A pair never trades | It is on the 1h rails engine; the `[STRATEGY]` boot line and the pair card say why. Run `run_strategy_gate.bat` |
| No trades on the rails engine | Hold-through blocks non-TREND_UP BUYs; min confidence is 0.65; friction may skip thin entries. `HYDRA_HOLD_THROUGH=0` gives the raw engine |
| `kraken: command not found` | Install kraken-cli in WSL and `source ~/.cargo/env`, or use `--demo` |
| Wrong WSL distro | `wsl -l -v`, then set `HYDRA_WSL_DISTRO` |
| Paper mode idle, 0 ticks | Needs working kraken-cli OHLC; use `--demo` without WSL |
| Port 3000 taken | Vite uses `strictPort`: free the port |
| Port 8765 taken | Launchers refuse to start. Free the port or pass `--ws-port` |
| Dashboard disconnected | The agent hosts `ws://127.0.0.1:8765` (not `localhost`/IPv6) |
| Dashboard connects, then shows nothing | WS auth failed: the UI must be served by the agent process that wrote `hydra_ws_token.json`. Unauthenticated sockets get no state and close after `HYDRA_WS_AUTH_GRACE_S` (default 10s) |
| Forgot dashboard password | `python hydra_auth.py reset-password <user>` |

## License

[MIT](LICENSE) © 2026 eternal-roman
