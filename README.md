# HYDRA

[![CI](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml/badge.svg)](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

**Regime-adaptive Kraken spot trading agent** — detects trending / ranging / volatile markets, switches among Momentum, Mean Reversion, Grid, and Defensive strategies, and places **limit post-only** orders only. Live React dashboard included.

> **Not financial advice.** Experimental research software. **Strategy expectancy is not proven** on the shipped engine path; safety rails reduce some failure modes but do not guarantee profit. Crypto trading can lose money.

## Highlights

- **Regime switching** on pure-Python indicators (Wilder RSI/ATR, Bollinger, MACD, EMAs)
- **Spot-only** execution — default `BTC/USD` + `ETH/USD` + `ZEC/USD` (v2.29, independent pairs;
  explicit SOL pairs restore the legacy triangle, bridge signal-only). `--pairs auto` seeds the
  cores and adds a satellite per held Kraken asset (USDC-preferred quote when funded, USD fallback)
- **Limit post-only** — never market; 2s REST floor; 15% drawdown **blocks new BUYs** (SELL flatten still allowed)
- **AI quant pipeline** (optional): Market Quant + Risk Manager + Grok + R1–R11 rules
- **Hold-through rails** (default on; `HYDRA_HOLD_THROUGH=0` off): TREND_UP entries ≥0.65 conf, flatten downs, ride mid-trends — defense + capture discipline, **not** a profit claim
- **Daily trend-ensemble overlay** (default on; `HYDRA_TREND_OVERLAY=0` off): sma200/ema20x100/
  don55 on daily closes gate entries, flatten on regime flip, vol-target conviction sizing —
  evidence-gated on real tape (`.hydra-flywheel/trend_overlay_gate.json`); 1h candles default
- **Research stack**: backtests, walk-forward, paper flywheel (no live flywheel orders), tools under `tools/`
- **Companions** (chat/proposals; live execution opt-in, default off)

## Safety (non-negotiable)

| Rule | Detail |
|------|--------|
| Spot only | No futures/margin/options orders placed |
| Limit post-only | `--type limit --oflags post` |
| Rate limit | ≥ 2s between Kraken REST calls |
| Drawdown | 15% max → **no new BUYs**; inventory SELL still allowed |
| Companion live | `HYDRA_COMPANION_LIVE_EXECUTION` default **off** |
| Hold-through | `HYDRA_HOLD_THROUGH` default **on** (`=0` off) |

## Quick start

### Zero-deps offline demo (no API keys, no WSL)

Clone and verify the stack without Kraken, keys, or Node:

```bash
git clone https://github.com/eternal-roman/hydra.git
cd hydra

pip install -r requirements.txt

# Engine synthetic walk (stdlib only)
python hydra_engine.py

# Backtest on synthetic series
python hydra_backtest.py

# Full agent loop offline — synthetic candles, paper fills, WS :8765
python hydra_agent.py --demo --duration 30 --balance 1000

# Paper flywheel report (local ledger; empty history → cash-only targets)
python hydra_flywheel.py --report
```

`--demo` never calls kraken-cli or loads API keys. Use it to confirm the
download works before installing WSL or provisioning keys.

### Requirements (live / paper)

- Python **3.10+**
- Node.js **18+** (dashboard only)
- WSL Ubuntu with [kraken-cli](https://github.com/krakenfx/kraken-cli) (`kraken --version` → 0.3.2+)
- Kraken API keys for **live** trading (spot trade; **no withdraw**)
- **Paper** still needs WSL + kraken-cli (public OHLC + `kraken paper`); no trade keys required for market data

### Install (dashboard + live)

```bash
git clone https://github.com/eternal-roman/hydra.git
cd hydra

pip install -r requirements.txt
cp .env.example .env   # fill keys for live only; never commit .env

cd dashboard && npm install && cd ..
```

### Run

```bash
# Offline first-run (recommended after clone)
python hydra_agent.py --demo --duration 30

# Paper via kraken-cli (no real money; needs WSL)
python hydra_agent.py --mode competition --paper

# Live (requires keys + kraken-cli)
python hydra_agent.py --balance 100                         # BTC/USD, ETH/USD, ZEC/USD
python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD       # legacy SOL triangle

# Dashboard (http://localhost:3000 → agent WS :8765)
cd dashboard && npm run dev

# Windows launchers
start_all.bat              # agent + dashboard
start_hydra.bat            # production: --mode competition --resume
```

### Config

| Source | Purpose |
|--------|---------|
| `.env` / `.env.example` | API keys, kill switches (`HYDRA_*`) |
| `--pairs` / `--quote` / `HYDRA_QUOTE` | Pairs + stable quote (default USD; triangle only if SOL triple configured) |
| `--demo` / `--paper` / `--resume` / `--mode` | Offline demo, paper, snapshot resume, Kelly mode |

Full flag and env tables: [`CLAUDE.md`](CLAUDE.md) · trading spec: [`SKILL.md`](SKILL.md)

## Architecture (short)

```
Candle/Ticker WS → indicators → regime → strategy signal
        → hold-through rails (default on)
        → (optional) AI brain + R1–R11
        → Kelly size → limit post-only (kraken-cli / WSL)
        → ExecutionStream / journal / snapshot → dashboard WS :8765
```

**Backtests** replay `HydraEngine` (+ coordinator). Full AI brain is not on the Phase-1 backtest path.

## Testing

CI runs on every PR to `main` (Python 3.10–3.12 + dashboard build + mock harness).

```bash
# Full suite (preferred)
python -m pytest tests/ -q

# Safety / money-path packs
python -m pytest tests/test_flywheel.py tests/test_friction_fee.py tests/test_hold_through.py -v

# Execution path (mandatory for placement changes)
python tests/live_harness/harness.py --mode smoke
python tests/live_harness/harness.py --mode mock

# Dashboard
cd dashboard && npm run build
```

Harness: `smoke` · `mock` (**35** scenarios in CI) · `validate` · `live` (explicit flag). See [`tests/live_harness/README.md`](tests/live_harness/README.md).

## Project layout

| Path | Role |
|------|------|
| `hydra_engine.py` | Indicators, regime, signals, sizing, hold-through rails |
| `hydra_agent.py` | Live loop, orders, journal, resume |
| `hydra_brain.py` / `hydra_quant_rules.py` | AI + deterministic rules |
| `hydra_flywheel.py` | Paper multi-sleeve allocator (CLI only) |
| `tools/` | History refresh, flywheel research, causal counterfactuals |
| `hydra_companions/` | Chat / proposals / optional live executor |
| `dashboard/` | React + Vite UI |
| `tests/` | Unit + harness |
| `docs/` | Backtest + companion specs |
| `.github/` | CI, Dependabot, CODEOWNERS |

## Dependabot & security

- **Dependabot** weekly updates: pip (`requirements.txt`), npm (`dashboard/`), GitHub Actions
- **Secret scanning** + **push protection** enabled on the repo
- Report vulnerabilities **privately** via [Security Advisories](https://github.com/eternal-roman/hydra/security/advisories/new) — see [`SECURITY.md`](SECURITY.md)
- Secrets stay local: `.env`, `hydra_*_token.json`, `hydra_auth_state.json`, `*.db` (all gitignored)

## Docs

| Doc | Contents |
|-----|----------|
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |
| [`CLAUDE.md`](CLAUDE.md) | Agent/dev invariants, env flags, module index |
| [`SKILL.md`](SKILL.md) | Trading formulas + risk rules |
| [`SECURITY.md`](SECURITY.md) | Vulnerability reporting |
| [`docs/BACKTEST.md`](docs/BACKTEST.md) | Backtest runbook |
| [`docs/BACKTEST_SPEC.md`](docs/BACKTEST_SPEC.md) | Backtest design archive (defaults: code wins) |
| [`docs/COMPANION_SPEC.md`](docs/COMPANION_SPEC.md) | Companion system |
| [`docs/HOLD_THROUGH.md`](docs/HOLD_THROUGH.md) | Hold-through rails (default on) |
| [`heartbeat/README.md`](heartbeat/README.md) · [`HONEST_FINDINGS.md`](heartbeat/HONEST_FINDINGS.md) | Order-flow posterior + evidence ledger |
| [`research/`](research/) | Formal papers + promoted study data |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Want a no-keys smoke test | `python hydra_agent.py --demo --duration 30` |
| `kraken: command not found` | Install kraken-cli in WSL; `source ~/.cargo/env` — or use `--demo` |
| Wrong WSL distro | `wsl -l -v` → set `HYDRA_WSL_DISTRO` |
| Port 3000 taken | Vite uses `strictPort` — free the port |
| Port 8765 taken | Pass `--ws-port 8766` (or free the port) |
| Dashboard disconnected | Start agent first (hosts WS on 8765); `--demo` works offline |
| No trades | Hold-through blocks non-TREND_UP BUYs; min_conf 0.65; friction may skip thin entries |
| Paper mode idle / 0 ticks | Needs working kraken-cli OHLC; use `--demo` without WSL |
| Want raw engine (no rails) | `HYDRA_HOLD_THROUGH=0` |

## Disclaimer

Experimental research software, **not** financial advice. Backtests are not promises of profit. Use least-privilege API keys. Safety rails are not guarantees.

## License

[MIT](LICENSE) © 2026 eternal-roman
