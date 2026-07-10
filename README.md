# HYDRA

[![CI](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml/badge.svg)](https://github.com/eternal-roman/hydra/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

**Regime-adaptive Kraken spot trading agent** — detects trending / ranging / volatile markets, switches among Momentum, Mean Reversion, Grid, and Defensive strategies, and places **limit post-only** orders only. Live React dashboard included.

> **Not financial advice.** Experimental software. Crypto trading can lose money.

## Highlights

- **Regime switching** on pure-Python indicators (Wilder RSI/ATR, Bollinger, MACD, EMAs)
- **Spot-only** execution on the SOL/BTC/USD triangle (default: `SOL/USD`, `SOL/BTC`, `BTC/USD`)
- **Limit post-only** — never market; 2s REST floor; 15% session circuit breaker
- **AI quant pipeline** (optional): Market Quant + Risk Manager + Grok + R1–R11 rules
- **Research stack**: backtests, walk-forward metrics, paper **flywheel** allocator (v2.27)
- **Companions** (optional chat/proposals; live execution **opt-in**, default off)

## Safety (non-negotiable)

| Rule | Detail |
|------|--------|
| Spot only | No futures/margin/options orders placed |
| Limit post-only | `--type limit --oflags post` |
| Rate limit | ≥ 2s between Kraken REST calls |
| Drawdown | 15% max → engine halted for the session |
| Companion live | `HYDRA_COMPANION_LIVE_EXECUTION` default **off** |

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
python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD --balance 100

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
| `--pairs` / `--quote` / `HYDRA_QUOTE` | Triangle + stable quote (default USD) |
| `--demo` / `--paper` / `--resume` / `--mode` | Offline demo, paper, snapshot resume, Kelly mode |

Full flag and env tables: [`CLAUDE.md`](CLAUDE.md) · trading spec: [`SKILL.md`](SKILL.md)

## Architecture (short)

```
Candle/Ticker WS → indicators → regime → strategy signal
        → (optional) AI brain + R1–R11 rules
        → Kelly size → limit post-only via kraken-cli (WSL)
        → ExecutionStream / journal / snapshot
        → dashboard WS :8765
```

**v2.27 additions:** friction expectancy gate on BUY entries; fee-true live accounting; paper flywheel (`python hydra_flywheel.py --report`) — **no live order path** in the flywheel.

## Testing

CI runs on every PR to `main` (Python 3.10–3.12 + dashboard build + mock harness).

```bash
# Full suite (preferred)
python -m pytest tests/ -q

# Flywheel + fee/friction (v2.27)
python -m pytest tests/test_flywheel.py tests/test_friction_fee.py -v

# Execution path (mandatory for placement changes)
python tests/live_harness/harness.py --mode smoke
python tests/live_harness/harness.py --mode mock

# Dashboard
cd dashboard && npm run build
```

Harness modes: `smoke` · `mock` · `validate` (read-only Kraken) · `live` (real orders, explicit flag). See [`tests/live_harness/README.md`](tests/live_harness/README.md).

## Project layout

| Path | Role |
|------|------|
| `hydra_engine.py` | Indicators, regime, signals, sizing |
| `hydra_agent.py` | Live loop, orders, journal, resume |
| `hydra_brain.py` / `hydra_quant_rules.py` | AI + deterministic rules |
| `hydra_flywheel.py` | Paper multi-sleeve allocator |
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
| [`CLAUDE.md`](CLAUDE.md) | Agent/dev invariants & module index |
| [`SKILL.md`](SKILL.md) | Full trading specification |
| [`docs/BACKTEST.md`](docs/BACKTEST.md) | Backtest runbook |
| [`docs/COMPANION_SPEC.md`](docs/COMPANION_SPEC.md) | Companion system |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Want a no-keys smoke test | `python hydra_agent.py --demo --duration 30` |
| `kraken: command not found` | Install kraken-cli in WSL; `source ~/.cargo/env` — or use `--demo` |
| Wrong WSL distro | `wsl -l -v` → set `HYDRA_WSL_DISTRO` |
| Port 3000 taken | Vite uses `strictPort` — free the port |
| Port 8765 taken | Pass `--ws-port 8766` (or free the port) |
| Dashboard disconnected | Start agent first (hosts WS on 8765); `--demo` works offline |
| No trades | Confidence gate 0.65; ranging markets often HOLD |
| Paper mode idle / 0 ticks | Needs working kraken-cli OHLC; use `--demo` without WSL |

## Disclaimer

This is experimental research software, **not** financial advice. Past performance does not predict future results. Use least-privilege API keys. Safety nets (dead-man switch, circuit breaker) are not guarantees.

## License

[MIT](LICENSE) © 2026 eternal-roman
