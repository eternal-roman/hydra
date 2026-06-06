# HYDRA Backtesting & Experimentation — Runbook

User-facing guide for the v2.10.0 backtest platform. For the full design spec,
see [`BACKTEST_SPEC.md`](./BACKTEST_SPEC.md).

> **v2.26.0 note:** the **AI Reviewer** and **Shadow Validator** were archived
> in v2.26.0 (built + CI-tested, never wired into production — `reviewer=None`).
> The live platform today is engine replay + experiments + walk-forward, surfaced
> through the dashboard **RESEARCH** tab. Their design is kept as history in
> `BACKTEST_SPEC.md`. See CHANGELOG v2.26.0.

---

## What it is

A strictly-additive backtesting and experimentation layer that sits **on top of**
the live agent without touching its code path. You can:

1. Run historical simulations using the exact same `HydraEngine` code that trades
   live (zero logic drift — guaranteed by `tests/test_backtest_drift.py`).
2. Compare presets (default, ideal, divergent, aggressive, defensive, and three
   regime-focused variants) and custom parameter sweeps side-by-side.
3. Watch a backtest render in real time alongside live in a dockable "observer
   modal" — the same pair-card / equity-chart / regime-ribbon components you see
   on the live tab, so "what is" and "what if" use the same visual language.
4. Optionally let the AI Brain (Analyst + Risk Manager) run backtests
   **mid-deliberation** via Anthropic tool-use, so hypotheses are validated
   against history before they influence a live trade.

Everything is gated behind flags and kill switches. Default behavior with no
opt-in flag is identical to v2.9.x.

> The **AI Reviewer** (rigor gates) and **Shadow Validator** were archived in
> v2.26.0 — built and CI-tested but never wired into production. Their design
> lives in `BACKTEST_SPEC.md` (§Layer 5, §Phase 11) as history.

---

## Quick start

### Run a backtest from the dashboard

1. Start the dashboard: `cd dashboard && npm run dev`.
2. Open the **RESEARCH** tab. It has two panes:
   - **Dataset** — inspect the canonical OHLC store (`hydra_history.sqlite`) coverage.
   - **Lab** — configure and run a hypothesis backtest (pick a preset, pair, and
     window; submit). Results stream in with metrics and the equity/regime views.
3. For side-by-side comparison, the CLI `compare_experiments` path below
   highlights the winner per metric (Sharpe, max drawdown, win rate) and flags
   statistically significant deltas.

### Run a backtest from the CLI

```bash
python -c "
from hydra_backtest import BacktestConfig, BacktestRunner, SyntheticSource
from hydra_engine import HydraEngine
cfg = BacktestConfig(pair='SOL/USD', start_ts=0, end_ts=86400*30, seed=42)
runner = BacktestRunner(cfg, engine_factory=HydraEngine, sources_override={'SOL/USD': SyntheticSource(seed=42)})
result = runner.run()
print(f'trades={result.metrics.total_trades} sharpe={result.metrics.sharpe_ratio:.2f}')
"
```

For real historical data, swap `SyntheticSource` for `KrakenHistoricalSource`
(caches to `.hydra-experiments/candle_cache/`; respects the 2s Kraken rate limit).

### Use a preset programmatically

```python
from hydra_experiments import PRESET_LIBRARY, run_experiment

preset = PRESET_LIBRARY["regime_volatile"]
experiment = run_experiment(preset, pair="SOL/USD", start_ts=0, end_ts=86400*7)
print(experiment.result.metrics.sharpe_ratio)
```

---

## Preset library

Eight presets ship in `.hydra-experiments/presets.json`. Users can add more without
touching code.

| Preset | Intent |
|---|---|
| `default` | Current live tuning — baseline reference. |
| `ideal` | Hand-picked "known good" parameters from historical tuner wins. |
| `divergent` | Deliberately atypical parameters to stress-test the regime detector. |
| `aggressive` | Half-Kelly, lower confidence threshold, tighter regime floors. |
| `defensive` | Eighth-Kelly, higher confidence threshold, wider volatility floors. |
| `regime_trending` | Trend-weighted params for TREND_UP / TREND_DOWN regimes. |
| `regime_ranging` | Mean-reversion-weighted params for RANGING regime. |
| `regime_volatile` | Grid-weighted params for VOLATILE regime. |

To add a preset, edit `.hydra-experiments/presets.json`. Schema: each entry must
carry a `name`, `description`, and `overrides` dict keyed by engine parameter.

---

## AI Reviewer & Shadow Validator (archived v2.26.0)

The post-backtest **AI Reviewer** (seven code-enforced rigor gates →
`ReviewDecision`) and the single-slot FIFO **Shadow Validator** were built and
CI-tested but never wired into production, and were archived in v2.26.0. Their
full design — the gate definitions, verdict types, PR-draft flow, and phantom-
trade validation loop — is retained as history in `BACKTEST_SPEC.md` (§Layer 5,
§Phase 11). No reviewer runs after a backtest today.

Manual parameter promotion still flows through the tuner directly:
`HydraTuner.apply_external_param_update(params)`, with one-step rollback via
`HydraTuner.rollback_to_previous()` (bounded depth=1 — reverts exactly one apply,
never cascades).

---

## Brain tool-use (opt-in)

By default, the AI Brain uses plain text prompts + JSON parsing as it did in
v2.9.x. To let the Analyst and Risk Manager run backtests mid-deliberation,
export:

```bash
export HYDRA_BRAIN_TOOLS_ENABLED=1
```

When enabled, both agents receive the `BACKTEST_TOOLS` Anthropic tool schemas
(`run_backtest`, `get_experiment`, `compare_experiments`, `list_presets`, etc.)
and can invoke them during deliberation. The Strategist (Grok) stays text-only —
xAI doesn't support the same tool-use protocol.

Per-caller quotas apply: 10 backtests per agent per day, 3 concurrent, 50
global/day. When the daily budget exceeds 80%, tool-use is disabled for the
rest of the day and the brain falls back to text-only.

---

## Kill switch

Export `HYDRA_BACKTEST_DISABLED=1` to disable the entire backtest subsystem.
The worker pool will not be constructed, the WebSocket handlers will reject
backtest messages with a `subsystem_disabled` error, and the agent boots
identically to v2.9.x. Verified by
`tests/live_harness/harness.py --mode smoke`.

---

## Storage

All backtest state lives under `.hydra-experiments/` (gitignored):

```
.hydra-experiments/
  experiments/        # one JSON per run (config + metrics + result summary)
  candle_cache/       # cached Kraken OHLC by (pair, interval, start, end)
```

Zero writes to live state files (invariant I3). Errors route to
`hydra_backtest_errors.log`.

---

## Safety invariants (I1–I12)

All twelve invariants from the spec are enforced. The highlights:

- **I1** Live tick cadence unaffected — measured, not assumed.
- **I3** Separate storage; live state files are never touched.
- **I6** Kill switch → v2.9.x behavior.
- **I7** Zero logic drift — drift regression replays live session tick-by-tick.
- **I11** Bounded: 2 default workers (4 max), queue depth 20, 50 experiments/day.

(I8/I9 governed the archived Reviewer/Shadow Validator — see the note above.)

See `BACKTEST_SPEC.md` §Safety Invariants for the full list.

---

## Tests

```bash
python -m pytest tests/test_backtest_engine.py      # runner, sources, fill model
python -m pytest tests/test_backtest_drift.py       # I7 — engine parity with live
python -m pytest tests/test_backtest_metrics.py     # bootstrap, walk-forward, MC
python -m pytest tests/test_experiments.py          # presets, store, sweep, compare
python -m pytest tests/test_backtest_tool.py        # tool dispatcher + quotas
python -m pytest tests/test_brain_tool_use.py       # Anthropic tool-use loop
python -m pytest tests/test_backtest_server.py      # worker pool + WS routing
python tests/live_harness/harness.py --mode smoke   # kill-switch verified
```

The full `python -m pytest tests/` run must pass before merging.

---

## Where to look when things go wrong

| Symptom | First place to check |
|---|---|
| Backtest never completes | `hydra_backtest_errors.log` |
| Dashboard doesn't show observer modal | browser devtools — WS frame with `type: "backtest.*"` should be visible |
| Brain never calls tools | `HYDRA_BRAIN_TOOLS_ENABLED=1`? Daily quota consumed? |
| Live tick slows down | `HYDRA_BACKTEST_DISABLED=1` should restore v2.9.x perf — if it doesn't, file a bug |

Full layering, schemas, and rationale: [`BACKTEST_SPEC.md`](./BACKTEST_SPEC.md).
