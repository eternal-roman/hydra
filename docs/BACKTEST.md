# HYDRA Backtesting & Experimentation — Runbook

User-facing guide for the research backtest platform. For the full design spec,
see [`BACKTEST_SPEC.md`](./BACKTEST_SPEC.md).

> **Honest scope:** Phase-1 `BacktestRunner` = `HydraEngine` + optional coordinator
> (no full AI brain). Positive metrics are not go-live. Default engine includes
> hold-through rails (`docs/HOLD_THROUGH.md`).

> **v2.26.0 note:** the **AI Reviewer** and **Shadow Validator** were archived
> (built + CI-tested, never production-wired). Live research surface is engine
> replay + experiments + walk-forward in the **RESEARCH** tab. Design history
> remains in `BACKTEST_SPEC.md`.

---

## What it is

A backtesting and experimentation layer that reuses live engine code without
placing exchange orders. You can:

1. Run historical simulations with the same `HydraEngine` logic as live
   (`tests/test_backtest_drift.py` guards drift for the Phase-1 path).
2. Compare presets and parameter sweeps (Sharpe, drawdown, win rate — **not**
   automatic proof of live edge).
3. Stream results in the dashboard **RESEARCH** tab (observer modal).
4. Optionally invoke backtests from the AI brain **tool-use** path mid-session
   (when enabled) — still subject to the engine-only replay limitations above.

Kill switch: `HYDRA_BACKTEST_DISABLED=1`.

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
cfg = BacktestConfig(
    name='smoke',
    pairs=('BTC/USD',),
    candle_interval=60,   # live default (v2.28+); rails calibrated on 1h
    random_seed=42,
    max_ticks=200,
)
src = SyntheticSource(seed=42, n_candles=300)
runner = BacktestRunner(cfg, sources_override={'BTC/USD': src})
result = runner.run()
print(f'status={result.status} trades={result.metrics.total_trades} '
      f'sharpe={result.metrics.sharpe_ratio:.2f}')
"
```

Live product defaults: `pairs=("BTC/USD",)`, `candle_interval=60`. Schema
history in `BACKTEST_SPEC.md` may show older SOL/15m design-era defaults —
**code is authoritative.**

For real historical data, use `data_source="sqlite"` + `hydra_history.sqlite`
params, or `KrakenHistoricalSource` (caches under `.hydra-experiments/`; 2s REST floor).

### Use a preset programmatically

```python
from hydra_backtest import BacktestConfig, BacktestRunner, SyntheticSource

cfg = BacktestConfig(name="regime_volatile_smoke", pairs=("BTC/USD",),
                     candle_interval=60, random_seed=7, max_ticks=150)
result = BacktestRunner(
    cfg, sources_override={"BTC/USD": SyntheticSource(seed=7, n_candles=250)}
).run()
print(result.status, result.metrics.total_trades, result.metrics.sharpe_ratio)
```

Presets live in `hydra_experiments.PRESET_LIBRARY` / `.hydra-experiments/`;
prefer the RESEARCH tab Lab pane for full experiment store runs.

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
