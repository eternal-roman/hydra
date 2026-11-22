# HYDRA Backtesting & Experimentation

The runbook for the research backtest platform, then its design spec. The backtest
modules cite the spec by section (§N); those numbers are unchanged.

> **Honest scope:** Phase-1 `BacktestRunner` = `HydraEngine` + optional coordinator
> (no full AI brain). Positive metrics are not go-live. Default engine includes
> hold-through rails (`docs/TRADE_PLAN.md`).

> Live research surface: engine replay + experiments + walk-forward in
> **RESEARCH**. The v2.26.0 AI Reviewer / Shadow Validator were never
> production-wired; their design history is in the spec below.

---

## What it is

A backtesting layer that reuses live `HydraEngine` code without placing
exchange orders:

1. Historical replay (`tests/test_backtest_drift.py` guards Phase-1 drift).
2. Preset / sweep compare (Sharpe, drawdown, win rate — not live-edge proof).
3. Stream results in the dashboard **RESEARCH** tab.
4. Optional brain tool-use mid-session (engine-only replay, same limits).

Kill switch: `HYDRA_BACKTEST_DISABLED=1`.

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
      f'sharpe={result.metrics.sharpe:.2f}')
"
```

Live product defaults: `pairs=("BTC/USD",)`, `candle_interval=60`. Schema
history in the spec below may show older SOL/15m design-era defaults —
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
print(result.status, result.metrics.total_trades, result.metrics.sharpe)
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

Manual parameter promotion: `HydraTuner.apply_external_param_update` with
one-step `rollback_to_previous()` (depth=1).

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

Ten of the twelve are enforced in code. Highlights:

- **I1** Live tick cadence unaffected — measured, not assumed.
- **I3** Separate storage; live state files are never touched.
- **I6** Kill switch → v2.9.x behavior.
- **I7** Zero logic drift — drift regression replays live session tick-by-tick.
- **I11** Bounded: 2 default workers (4 hard cap), queue depth 20, 50 experiments/day global.

**I8 and I9 are moot, not enforced** — both governed the AI Reviewer / Shadow
Validator, archived in v2.26.0. Nothing auto-applies code or params today, so
there is no gate to run; re-enabling either subsystem re-opens both invariants.

The full list is [§3](#3-core-invariants-safety-critical); §9 maps each to its code.

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

Full layering, schemas and rationale: the design spec below.

---

## Design spec (v2.10.0, shipped)

**Status:** SHIPPED (live since v2.10.0) — **design archive + operator history**
**Target version:** v2.10.0 (design-era); product pin is CLAUDE.md / `HYDRA_VERSION`
**Author:** eternal-roman
**Date:** 2026-04-16

> **Live defaults (code is authoritative):** `BacktestConfig.pairs = ("BTC/USD",)`,
> `candle_interval = 60`. Schema snippets below that still show SOL triangle /
> 15m are **design-era**, not current product defaults. Agent index: `CLAUDE.md`.

> **v2.26.0 note:** the **AI Reviewer** (Phase 7) and **Shadow Validator**
> (Phase 11) were archived in v2.26.0 — fully built and CI-tested but never
> wired into production (`reviewer=None`; shadow validator never
> instantiated). Sections describing them below are design history, not
> current behavior. See v2.26.0 in `git show v2.30.0:CHANGELOG.md`.

---

### 0. TL;DR

A production-grade backtesting + experimentation subsystem for Hydra that:

- Replays historical candles through the **live** `HydraEngine`, `CrossPairCoordinator`, `OrderBookAnalyzer`, and (stubbed or real) brain — zero logic drift
- Lets **humans** (via a new dashboard tab) and **AI brain agents** (via Anthropic tool-use) configure, run, and compare experiments
- Emits real-time `dashboard_state`-shape events during replay so an **observer modal** can render the backtest using the exact same UI components as live
- Supports a **dual-state view** — live agent and backtest side-by-side in the same visual language
- Includes an **AI Reviewer layer** that post-analyzes every backtest for *materially impactful, repeatable* improvements — with architectural anti-handwaving gates
- Ships institutional-grade rigor: walk-forward, Monte Carlo CI, regime-conditioned stats, parameter sensitivity, reproducibility stamps
- **Never touches live agent state, live journal, live snapshot, or live tick cadence.**

---

### 1. Goals

#### Primary

1. **Zero logic drift** — backtester uses the live engine's code verbatim; only I/O (candles, ticker, book, balances, order placement) is mocked.
2. **Human-triggerable** — a dashboard Backtest tab with preset dropdown, param sliders, data range picker, and a Run button.
3. **Agent-triggerable** — the Brain's Analyst and Risk Manager can invoke backtests mid-deliberation via Anthropic tool-use; results stream back into their reasoning context.
4. **Real-time observable** — backtests emit `dashboard_state` events during replay at configurable pace (0 = fast as possible, 1 = live cadence, 60 = 60× faster); a modal renders them live.
5. **Dual-state** — live dashboard and backtest dashboard co-exist in the same view during brain-triggered experiments. "What is" vs "what if", side by side.
6. **AI Reviewer with rigor** — post-backtest review proposes parameter or code changes *only* when they pass architectural materiality and repeatability gates. Anti-handwaving is in code, not prompt.
7. **Persistent experiment library** — every run stored with reproducibility stamps (git SHA, param hash, seed, data source); browsable, comparable, re-runnable.
8. **Institutional rigor** — walk-forward, Monte Carlo bootstrap CIs, regime-conditioned P&L, parameter sensitivity heatmaps, out-of-sample gap analysis.

#### Secondary (future extensions, out of v2.10.0 scope)

- Multi-strategy portfolio optimization (beyond current 4 regime-mapped strategies)
- Live shadow-mode validation daemon (runs candidate param sets in parallel with live, compares after N trades)
- Public experiment sharing / collaborative hypothesis registry
- Adversarial stress testing (flash crash, halt, gap replays)
- LLM-synthesized candidate parameter sets (beyond sweep/preset)

### 2. Non-Goals

- Replacing `HydraTuner` — the existing per-trade exponential-smoothing tuner stays. Reviewer operates at a different timescale.
- Replacing `live_harness` — that's our order-placement validation tool. Backtester is about strategy evaluation.
- A vectorized/batch backtester (e.g., pandas-based) — we use per-tick iteration to preserve engine statefulness and zero drift.
- External dependencies — stays pure Python stdlib (CLAUDE.md invariant: no numpy/pandas/scipy in engine path).
- A general-purpose trading framework — this is Hydra-specific.

### 3. Core Invariants (safety-critical)

These are non-negotiable; any PR that violates them is rejected.

**I1. Live-unaffected cadence.** Backtest workers must never block the live tick loop. Measurable: live tick duration distribution pre/post-deploy must be statistically identical (bootstrap t-test, p > 0.5).

**I2. Separate state.** Backtest workers construct their own `HydraEngine` / `CrossPairCoordinator` / `HydraBrain` instances. They NEVER hold references to the live agent's instances.

**I3. Separate storage.** Backtest artifacts go in `.hydra-experiments/`. They NEVER write to `hydra_session_snapshot.json`, `hydra_order_journal.json`, `hydra_trades_live.json`, or `hydra_params_*.json`.

**I4. Daemon threads only.** All backtest worker threads are daemon threads. If the live process dies, workers die too — no orphans.

**I5. Exception isolation.** Every backtest worker entry point is wrapped in try/except. Exceptions are logged to `hydra_backtest_errors.log` and the live tick loop is never affected. (Mirror of the existing `agent.tick_exception_safety` invariant.)

**I6. Kill switch.** Environment variable `HYDRA_BACKTEST_DISABLED=1` disables the entire subsystem — no worker pool, no WS message handling, no Brain tool. The agent runs identically to v2.9.2 when this is set.

**I7. Zero drift.** A drift regression test replays a known live session snapshot through the backtester with identical params and asserts tick-by-tick equality of `(regime, signal.action, signal.confidence, position.size)` within floating-point tolerance 1e-9. Fails the build on drift.

**I8. Observer never auto-applies code.** The AI Reviewer may recommend code changes; it may NEVER apply them. Code recommendations generate a PR draft and notify the human via the dashboard.

**I9. Observer parameter auto-apply requires shadow validation.** Even when a param-change recommendation passes all materiality gates, it is NOT applied to live params directly. It is queued for live shadow validation (runs in parallel with live for N ticks); only promoted after statistical significance is confirmed and human approves via dashboard.

**I10. Kraken-safe data fetch.** Historical candle fetches go through `KrakenCLI.ohlc()` and respect the existing 2s rate limit. A local disk cache (`.hydra-experiments/candle_cache/{pair}_{interval}_{start}_{end}.json`) prevents redundant fetches.

**I11. Bounded resources.** Worker pool size default 2, max 4. Queue depth capped at 20. Per-day compute budget (agent-triggered) bounded at 50 experiments. Per-experiment candle budget 200,000 (enough for ~2 years of 15-min SOL).

**I12. Reproducibility stamp.** Every `BacktestResult` carries `git_sha`, `param_hash`, `data_spec_hash`, `random_seed`, `hydra_version`. Re-running with the same stamp on the same data source must produce bit-identical metrics.

---

### 4. System Architecture — 7 Layers

```
┌───────────────────────────────────────────────────────────────────┐
│ Layer 7: Dashboard UI                                              │
│   App.jsx — tabs (Live | Backtest | Compare), BacktestControlPanel │
│   BacktestObserverModal (dual-state), ExperimentLibrary,           │
│   ReviewPanel (verdicts, evidence, human approve/reject)           │
└──────────────────────────────▲─────────────────────────────────────┘
              WebSocket (shared port 8765, message-type discrim.)
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 6: Backend Bridge                                            │
│   hydra_backtest_server.py — BacktestWorkerPool (2 daemon threads),│
│   request queue, progress broadcaster, WS message router mounted   │
│   inside HydraAgent's existing DashboardBroadcaster                │
└──────────────────────────────▲─────────────────────────────────────┘
                               │ Python function calls
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 5: AI Reviewer / Self-Evaluation                             │
│   hydra_reviewer.py — ResultReviewer (Claude Opus), reads result + │
│   runs walk-forward + MC confirmation passes, produces             │
│   ReviewDecision with materiality + repeatability evidence         │
└──────────────────────────────▲─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 4: Agent Tool API                                            │
│   hydra_backtest_tool.py — Anthropic-tool-use-compatible functions │
│   exposed to Analyst, Risk Manager (Claude only; Grok text-only)   │
└──────────────────────────────▲─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 3: Experiments Framework                                     │
│   hydra_experiments.py — Experiment dataclass, ExperimentStore,    │
│   Preset library, sweep, compare, walk_forward, monte_carlo        │
└──────────────────────────────▲─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 2: Advanced Metrics                                          │
│   hydra_backtest_metrics.py — bootstrap CI, regime-conditioned P&L,│
│   walk-forward slicer, MC resampler, sensitivity grid              │
└──────────────────────────────▲─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 1: Backtest Engine                                           │
│   hydra_backtest.py — CandleSource (Kraken/CSV/Snapshot/Synthetic),│
│   SimulatedFiller, BacktestRunner, BacktestResult                  │
└──────────────────────────────▲─────────────────────────────────────┘
                               │ imports, no modifications
┌──────────────────────────────▼─────────────────────────────────────┐
│ Layer 0: LIVE CODE — UNTOUCHED                                     │
│   hydra_engine.py, hydra_agent.py (engine path), hydra_brain.py    │
│   (brain path), hydra_tuner.py                                     │
│                                                                    │
│   Touchpoints for mount-only changes (strictly additive):          │
│     - hydra_agent.py: mount server thread, route WS messages       │
│     - hydra_brain.py: add _call_llm_with_tools() method,           │
│       wire tool schemas to Analyst + Risk Manager                  │
│     - DashboardBroadcaster: message-type discrimination            │
└────────────────────────────────────────────────────────────────────┘
```

---

### 5. Data Schemas

#### 5.1 BacktestConfig

```python
@dataclass(frozen=True)
class BacktestConfig:
    # Identity
    name: str
    description: str = ""
    hypothesis: str = ""                              # freeform human/agent text

    # Universe
    pairs: Tuple[str, ...] = ("SOL/USD", "SOL/BTC", "BTC/USD")
    initial_balance_per_pair: float = 100.0
    candle_interval: int = 15                          # minutes

    # Strategy config
    mode: str = "conservative"                         # or "competition"
    param_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)
                                                       # keyed by pair → {param: value}
    coordinator_enabled: bool = True
    order_book_enabled: bool = False                   # sim book data is fake by default
    forex_session_enabled: bool = True
    # brain_mode — DEFERRED design (the brain-in-backtest integration sketched
    # in §6.1's loop is unbuilt). Not present in the shipped BacktestConfig:
    # the replay loop runs the engine deterministically (generate_only) with no
    # brain constructed. Planned modes: none | confirm_all | mock_analyst | real.

    # Data
    data_source: str = "kraken"                        # kraken | csv | snapshot | synthetic
    data_source_params: Dict[str, Any] = field(default_factory=dict)
    start_time: Optional[str] = None                   # ISO 8601 UTC
    end_time: Optional[str] = None

    # Fill model
    fill_model: str = "realistic"                      # optimistic | realistic | pessimistic
    maker_fee_override: Optional[float] = None         # bps; None = use Kraken tier

    # Execution
    real_time_factor: float = 0.0                      # 0 = as fast as possible; 60 = 60× live
    random_seed: int = 42
    max_ticks: int = 200_000                           # hard cap — I11

    # Stamps (auto-filled at construction)
    git_sha: str = ""
    param_hash: str = ""
    hydra_version: str = ""
    created_at: str = ""
```

#### 5.2 BacktestResult

```python
@dataclass
class BacktestResult:
    config: BacktestConfig
    status: str                                        # running | complete | cancelled | failed
    started_at: str
    completed_at: Optional[str]
    wall_clock_seconds: float

    # Tick-level data (compact — sampled for large runs)
    equity_curve: Dict[str, List[float]]               # pair → tick-aligned equity (incl. aggregate)
    regime_ribbon: Dict[str, List[str]]                # pair → tick-aligned regime
    signal_log: Dict[str, List[Dict]]                  # pair → list of (tick, action, confidence)
    trade_log: List[Dict]                              # same shape as live order_journal entries

    # Summary stats
    metrics: BacktestMetrics
    per_pair_metrics: Dict[str, BacktestMetrics]

    # Diagnostics
    candles_processed: int
    fills: int
    rejects: int                                        # e.g., ordermin/costmin fails
    brain_calls: int
    brain_overrides: int

    # Errors
    errors: List[Dict]                                  # [{tick, pair, type, message, traceback}]
```

#### 5.3 BacktestMetrics

```python
@dataclass
class BacktestMetrics:
    # Return & risk
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_pct: float
    avg_drawdown_pct: float
    p95_drawdown_pct: float
    profit_factor: float

    # Trade stats
    total_trades: int
    win_count: int
    loss_count: int
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_holding_ticks: float
    longest_win_streak: int
    longest_loss_streak: int

    # Regime-conditioned
    pnl_by_regime: Dict[str, float]                    # TREND_UP/DOWN/RANGING/VOLATILE
    trades_by_regime: Dict[str, int]
    win_rate_by_regime: Dict[str, float]

    # Strategy-conditioned
    pnl_by_strategy: Dict[str, float]
    trades_by_strategy: Dict[str, int]

    # Brain diagnostics
    brain_override_rate: float
    brain_avg_size_multiplier: float

    # Robustness (from Monte Carlo resample)
    sharpe_ci_95: Tuple[float, float]                  # (lower, upper)
    return_ci_95: Tuple[float, float]
    max_dd_ci_95: Tuple[float, float]

    # Walk-forward stability
    wf_sharpe_slices: List[float]                      # one Sharpe per window
    wf_sharpe_stability: float                          # 1 / (1 + std(slices))

    # Sensitivity (only when sweep was run)
    sensitivity: Dict[str, float]                      # param → |d(sharpe)/d(param)| normalized

    # Fill realism
    fill_rate: float                                   # fills / (fills + rejects)
    fill_optimism_label: str                           # "high" | "medium" | "low"
```

#### 5.4 Experiment

```python
@dataclass
class Experiment:
    id: str                                            # uuid4
    created_at: str
    name: str
    hypothesis: str
    triggered_by: str                                  # "human" | "brain:analyst" | "brain:risk" |
                                                       # "brain:strategist" | "cli" | "reviewer"
    parent_id: Optional[str] = None                    # if derived from another experiment
    base_preset: Optional[str] = None
    overrides: Dict[str, Any] = field(default_factory=dict)

    config: BacktestConfig = None
    result: Optional[BacktestResult] = None
    review: Optional["ReviewDecision"] = None

    status: str = "pending"                            # pending | running | complete | failed | cancelled
    tags: List[str] = field(default_factory=list)
```

#### 5.5 ReviewDecision (AI Observer output)

```python
@dataclass
class ReviewDecision:
    experiment_id: str
    reviewed_at: str
    reviewer_model: str                                # e.g. "claude-opus-4-6"

    verdict: str
    # NO_CHANGE          — result is as expected, nothing actionable
    # PARAM_TWEAK        — specific param change proposed (auto-apply eligible)
    # CODE_REVIEW        — code/rule change recommended (human review required)
    # RESULT_ANOMALOUS   — result doesn't fit any known pattern; flag for human
    # HYPOTHESIS_REFUTED — experimenter's stated hypothesis is contradicted by data

    observations: List[str]                            # specific factual patterns, each < 200 chars
    root_cause_hypothesis: str                         # what the reviewer thinks is driving the result
    reasoning: str                                     # full reasoning chain

    # Proposed changes (if any)
    proposed_changes: List[ProposedChange]

    # Rigor gates — all must be True for auto-apply eligibility
    materiality_score: float                           # 0-1; expected Sharpe delta normalized
    repeatability: RepeatabilityEvidence
    gates_passed: Dict[str, bool]                      # gate_name → pass/fail
    all_gates_passed: bool                             # computed: all(gates_passed.values())

    # Meta
    confidence: str                                    # LOW | MEDIUM | HIGH
    risk_flags: List[str]
    source_files_read: List[str]                       # audit trail for code review
    tokens_used: int
    cost_usd: float
```

```python
@dataclass
class ProposedChange:
    change_type: str                                   # "param" | "code"
    scope: str                                         # "global" | "pair:SOL/USD" | "regime:VOLATILE"
    target: str                                        # param name or file:line
    current_value: Any
    proposed_value: Any
    expected_impact: Dict[str, float]                  # {sharpe: +0.3, max_dd: -2.1, ...}
    evidence_refs: List[str]                           # experiment IDs supporting this
    rationale: str
    risk_notes: str
```

```python
@dataclass
class RepeatabilityEvidence:
    # Walk-forward
    wf_slices_tested: int
    wf_improved_slices: int                            # out of total
    wf_improvement_pct_per_slice: List[float]

    # Monte Carlo on the improvement delta itself
    mc_iterations: int
    mc_mean_improvement: float
    mc_ci_95: Tuple[float, float]                      # if lower > 0 → statistically positive
    mc_p_value: float                                  # probability improvement is ≤ 0

    # Out-of-sample
    oos_held_out_pct: float                            # e.g. 0.20
    in_sample_sharpe: float
    oos_sharpe: float
    oos_gap_pct: float                                 # (in_sample - oos) / in_sample * 100

    # Cross-pair
    pairs_improved: int                                # out of len(pairs)
    improvement_by_pair: Dict[str, float]

    # Regime
    regimes_improved: int                              # out of 4
    improvement_by_regime: Dict[str, float]

    # Trade count sanity
    total_trades_in_sample: int                        # must be >= 50 for any recommendation
```

#### 5.6 WebSocket Message Types (new)

All messages are JSON. Existing tick broadcast wrapped as `{"type": "state", "data": ...}` to preserve backward compat (dashboards missing the tab can keep reading `data`).

| Direction | Type | Payload |
|-----------|------|---------|
| server → client | `state` | existing live-tick state dict |
| server → client | `backtest_progress` | `{experiment_id, tick, total, eta_sec, dashboard_state}` |
| server → client | `backtest_result` | `{experiment_id, result: BacktestResult}` |
| server → client | `backtest_review` | `{experiment_id, review: ReviewDecision}` |
| server → client | `experiment_list` | `{experiments: [Experiment summaries]}` |
| server → client | `preset_list` | `{presets: [PresetInfo]}` |
| server → client | `error` | `{channel, message, experiment_id?}` |
| client → server | `backtest_start` | `{config: BacktestConfig, triggered_by: "human"}` |
| client → server | `backtest_cancel` | `{experiment_id}` |
| client → server | `experiment_delete` | `{experiment_id}` |
| client → server | `review_request` | `{experiment_id}` — force re-review |
| client → server | `review_decision_response` | `{experiment_id, action: "accept"/"reject"/"park", notes}` |
| client → server | `shadow_promote` | `{experiment_id}` — queue param change for live shadow validation |

---

### 6. Layer-by-layer Detailed Design

#### 6.1 Layer 1: Backtest Engine (`hydra_backtest.py`, ~900 LOC)

**Classes:**

**`CandleSource` (ABC)**

```python
class CandleSource(ABC):
    @abstractmethod
    def iter_candles(self, pair: str) -> Iterator[Candle]: ...
    @abstractmethod
    def describe(self) -> Dict[str, Any]: ...          # for result stamp
```

Implementations:

- **`KrakenHistoricalSource`** — calls `KrakenCLI.ohlc(pair, interval)`. Caches to `.hydra-experiments/candle_cache/{pair_sanitized}_{interval}_{start}_{end}.json`. Cache invalidation: never (historical candles are immutable by definition). Respects 2s rate limit.
- **`CsvSource`** — loads from CSV path. Columns: `timestamp, open, high, low, close, volume`.
- **`SnapshotReplaySource`** — replays candles already stored in a `hydra_session_snapshot.json` engine snapshot. Read-only on the snapshot file.
- **`SyntheticSource`** — generates synthetic series. Strategies: `gbm` (geometric Brownian motion), `mean_reverting` (Ornstein-Uhlenbeck), `regime_switching` (HMM-like). Seeded.

**`SimulatedFiller`**

```python
class SimulatedFiller:
    def __init__(self, fill_model: str, maker_fee_bps: float): ...
    def try_fill(self, order: PendingOrder, next_candle: Candle) -> Optional[Fill]: ...
```

Fill logic:
- `optimistic`: fill if `order.limit_price` is between `next_candle.low` and `next_candle.high` (wick touch). Fills at limit price. Maker fee applied.
- `realistic` (default): fill if wick touches AND `next_candle`'s body passes through at least 30% of the distance from its own edge to the limit — i.e., the price actually spent time near the limit, not just a momentary spike. More conservative than wick-touch.
- `pessimistic`: fill only if `next_candle.close` crosses the limit. Models worst-case post-only behavior in fast markets.

All models: post-only, maker fees, no slippage beyond the limit price itself.

**`BacktestRunner`**

```python
class BacktestRunner:
    def __init__(self, config: BacktestConfig): ...
    def run(self,
            on_tick: Callable[[Dict], None] = None,
            cancel_token: threading.Event = None) -> BacktestResult: ...
```

Execution loop (pseudocode):
```
result = BacktestResult(config, status="running")
engines = {pair: HydraEngine(asset=pair, **config) for pair in pairs}
coord = CrossPairCoordinator(pairs) if config.coordinator_enabled else None
brain = construct_brain(config.brain_mode)
pending_orders = {}     # open limit orders per pair
candles_by_pair = {pair: list(source.iter_candles(pair)) for pair in pairs}

for tick in range(min_tick_count):
    if cancel_token and cancel_token.is_set():
        result.status = "cancelled"; break

    current_candles = {pair: candles_by_pair[pair][tick] for pair in pairs}

    # Try to fill any pending orders against this tick's candle
    for pair, order in pending_orders.items():
        fill = filler.try_fill(order, current_candles[pair])
        if fill: engines[pair].apply_synthetic_fill(fill); ...

    # Ingest and tick each engine (mirrors live _fetch_and_tick)
    engine_states = {}
    for pair in pairs:
        engines[pair].ingest_candle(current_candles[pair])
        engine_states[pair] = engines[pair].tick(generate_only=bool(brain))

    # Cross-pair coordination
    if coord:
        for pair, state in engine_states.items():
            coord.update(pair, state["regime"])
        overrides = coord.get_overrides(engine_states)
        apply_overrides(engine_states, overrides)

    # Order book + FOREX session modifiers (same logic as live)
    apply_modifiers(engine_states, config)

    # Brain
    if brain:
        for pair, state in engine_states.items():
            if state["signal"]["action"] != "HOLD":
                decision = brain.deliberate(state)
                apply_brain_decision(engines[pair], state, decision)

    # Execute signals via engine.execute_signal() with size_multiplier
    for pair, state in engine_states.items():
        trade = engines[pair].execute_signal(...)
        if trade and trade["needs_placement"]:
            pending_orders[pair] = make_pending_order(trade, current_candles[pair])

    # Progress callback
    if on_tick and tick % progress_stride == 0:
        on_tick(build_dashboard_state_shape(tick, engine_states, result))

    # Pace if requested
    if config.real_time_factor > 0:
        time.sleep(candle_interval_seconds / config.real_time_factor)

result.status = "complete"
result.metrics = compute_metrics(engines, result)
return result
```

**Integration with live code**: this loop mirrors `HydraAgent._tick()` in `hydra_agent.py` but uses local instances. The order of modifier application, coordinator calls, and brain calls matches live exactly to satisfy I7 (zero drift).

#### 6.2 Layer 2: Advanced Metrics (`hydra_backtest_metrics.py`, ~500 LOC)

**Functions:**

- `compute_basic_metrics(equity_curve, trades) → BacktestMetrics (core fields)` — stdlib only.
- `bootstrap_ci(values, n_iter=1000, ci=0.95) → (lower, upper)` — vanilla Python bootstrap; no scipy.
- `walk_forward(config, train_pct, test_pct, n_windows) → List[BacktestResult]` — slides train/test windows across data; returns a result per test window. Used for `wf_sharpe_stability`.
- `monte_carlo_resample(trades, n_iter=500) → Dict[metric, CI]` — resamples trade sequences with replacement, preserves temporal structure via block bootstrap (block length = 20 trades).
- `annualization_factor(candle_interval_min) → float` — same formula as live engine: `sqrt(365*24*60/candle_interval_min)`.

#### 6.3 Layer 3: Experiments Framework (`hydra_experiments.py`, ~700 LOC)

**`ExperimentStore`** — persistent backing store.

```python
class ExperimentStore:
    def __init__(self, root: Path = Path(".hydra-experiments"))
    def save(self, exp: Experiment) -> None
    def load(self, id: str) -> Experiment
    def list(self, filter: Dict = None, limit: int = 100) -> List[Experiment]
    def find_best(self, metric: str = "sharpe",
                  filter: Dict = None, min_trades: int = 50) -> Optional[Experiment]
    def delete(self, id: str) -> None
    def prune(self, older_than_days: int = 30, keep_tags: List[str] = None) -> int
    def audit_log(self, event: Dict) -> None         # appends to audit.log
```

Files: `.hydra-experiments/{id}.json` per experiment, `.hydra-experiments/audit.log` append-only, `.hydra-experiments/presets.json` editable by humans, `.hydra-experiments/review_history.jsonl` per-review record.

**`ExperimentPresets`** — blessed starting points:

```json
{
  "default":                    { "description": "Current live params", "overrides": {} },
  "ideal":                      { "description": "Best params from tuner on disk" },
  "divergent":                  { "description": "Deliberately contrary to current",
                                  "overrides": { "min_confidence_threshold": 0.55,
                                                 "momentum_rsi_lower": 25,
                                                 "momentum_rsi_upper": 75,
                                                 "kelly_multiplier": 0.5 } },
  "aggressive":                 { "description": "Competition mode + lowered gates",
                                  "overrides": { "min_confidence_threshold": 0.60,
                                                 "kelly_multiplier": 0.75,
                                                 "max_position_pct": 0.50 } },
  "defensive":                  { "description": "Higher conf, wider RSI, quarter-Kelly",
                                  "overrides": { "min_confidence_threshold": 0.75,
                                                 "momentum_rsi_lower": 35,
                                                 "momentum_rsi_upper": 65 } },
  "regime_trending":            { "description": "Tuned for TREND regimes",
                                  "overrides": { "trend_ema_ratio": 1.003,
                                                 "momentum_rsi_lower": 28 } },
  "regime_ranging":             { "description": "Tuned for RANGING",
                                  "overrides": { "mean_reversion_rsi_buy": 30,
                                                 "mean_reversion_rsi_sell": 70 } },
  "regime_volatile":            { "description": "Tuned for VOLATILE",
                                  "overrides": { "volatile_atr_mult": 1.5,
                                                 "kelly_multiplier": 0.15 } }
}
```

User-editable; loaded at startup. New presets can be added either via dashboard (future) or direct JSON edit.

**`compare(exp_ids) → ComparisonReport`** — returns ranked table with winner per metric and diff-of-means bootstrap p-values on terminal equity, Sharpe, max DD, profit factor.

**`sweep(param, values, base_config) → List[Experiment]`** — parallelizes across worker pool; streams progress.

**`walk_forward(config, train_days, test_days, step_days)` and `monte_carlo(config, n_paths, perturbation)`** — thin wrappers over Layer 2 functions, producing Experiment records.

#### 6.4 Layer 4: Agent Tool API (`hydra_backtest_tool.py`, ~500 LOC)

**Tool schemas** (Anthropic-compatible):

```python
BACKTEST_TOOLS = [
    {
        "name": "run_backtest",
        "description": "Run a backtest experiment. Returns an experiment_id immediately; result is retrievable via get_experiment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "enum": [...presets]},
                "overrides": {"type": "object",
                             "description": "Param name → value overrides"},
                "data_range_days": {"type": "integer", "minimum": 7, "maximum": 365},
                "pairs": {"type": "array", "items": {"type": "string"}},
                "hypothesis": {"type": "string",
                              "description": "Why you're running this; recorded in experiment"},
                "mode": {"type": "string", "enum": ["conservative", "competition"]}
            },
            "required": ["preset", "hypothesis"]
        }
    },
    # ... list_presets, list_experiments, get_experiment, compare_experiments,
    # ... find_best, sweep_param, get_equity_curve, cancel_experiment
]
```

**Safety guardrails (I11):**
- Per-brain-agent quota: max 3 concurrent, max 10/day per agent persona
- Global quota: 50/day across all agents
- Audit log entry for every call with agent persona + hypothesis
- Brain cannot delete or mutate experiments — read + create only

**Dispatcher:**

```python
class BacktestToolDispatcher:
    def execute(self, tool_name: str, tool_input: Dict,
                caller: str) -> Dict: ...
```

Returns a dict shaped for Anthropic `tool_result` blocks.

#### 6.5 Layer 5: AI Reviewer (`hydra_reviewer.py`, ~700 LOC)

**`ResultReviewer`** — the observer persona.

```python
class ResultReviewer:
    def __init__(self, anthropic_client, reviewer_model="claude-opus-4-6",
                 max_daily_cost: float = 5.0): ...
    def review(self, experiment: Experiment,
               live_context: LiveContext) -> ReviewDecision: ...
    def batch_review(self, experiment_ids: List[str]) -> List[ReviewDecision]: ...
    def self_retrospective(self, lookback_days: int = 30) -> SelfRetrospective: ...
```

**Review flow:**

1. **Gather evidence** — reads `BacktestResult`, relevant live journal context (last N fills for comparable regimes), related prior experiments, current live params.
2. **Run confirmation passes** — before LLM, the reviewer automatically runs:
   - Walk-forward re-test on the experiment's config
   - Monte Carlo resample on the trade sequence
   - Out-of-sample test if the experiment used in-sample only
   - Cross-pair breakdown (does the improvement hold on each pair?)
   - Regime breakdown (does it hold in each regime?)
3. **LLM deliberation** — Claude Opus gets the full evidence pack and produces a structured `ReviewDecision`. Tool-use enabled for:
   - `read_source_file(path)` — read any .py file in the repo (read-only)
   - `read_param_bounds()` — get PARAM_BOUNDS from tuner
   - `read_live_trades(pair, n)` — read last N journal entries (read-only)
   - `read_prior_reviews(limit)` — read reviewer's own history
4. **Apply rigor gates** — the code computes `gates_passed` dict:
   - `min_trades_50`: `result.trades >= 50`
   - `mc_ci_lower_positive`: `review.repeatability.mc_ci_95[0] > 0`
   - `wf_majority_improved`: `review.repeatability.wf_improved_slices / wf_slices_tested >= 0.6`
   - `oos_gap_acceptable`: `review.repeatability.oos_gap_pct < 30`
   - `improvement_above_2se`: `mc_mean_improvement > 2 * se(mc_iterations)`
   - `cross_pair_majority`: `pairs_improved / len(pairs) >= 0.5`
   - `regime_not_concentrated`: improvement not concentrated in exactly one regime (if it is, verdict must be regime-scoped recommendation)
5. **Verdict assignment** — the reviewer's `verdict` field must be consistent with gates. If `verdict` in `{PARAM_TWEAK}` but `all_gates_passed == False`, the dispatcher downgrades to `RESULT_ANOMALOUS` with a "reviewer self-contradicted" risk flag.

**Self-retrospective (`self_retrospective`):**

Periodically (once per week, human-triggered or scheduled), the reviewer audits its own prior recommendations:
- For each past `PARAM_TWEAK` that was promoted to live shadow and then to live: did Sharpe actually improve?
- For each past `CODE_REVIEW` that was merged: did the claimed impact materialize?
- Compute `reviewer_accuracy_score` — a running metric of reviewer quality
- If accuracy drops below threshold (e.g., 40% of recommendations materialize as claimed), reviewer auto-downgrades its confidence outputs and flags for human calibration

**Anti-handwaving architecture:**

- Reviewer cannot recommend based on < 50 trades (enforced in dispatcher)
- Reviewer cannot claim improvement exceeding the Monte Carlo upper CI
- Reviewer's claimed `expected_impact` is cross-checked against `mc_mean_improvement`; deviation > 50% → risk flag
- Reviewer must cite specific trade IDs or regime windows in `evidence_refs`
- Every recommendation records `source_files_read` for audit
- Recommendations without quantitative evidence (i.e., only prose rationale) are auto-downgraded to `CODE_REVIEW` (never `PARAM_TWEAK`)

#### 6.6 Layer 6: Backend Bridge (`hydra_backtest_server.py`, ~400 LOC)

**`BacktestWorkerPool`**

```python
class BacktestWorkerPool:
    def __init__(self, max_workers: int = 2, store: ExperimentStore = None,
                 broadcaster: DashboardBroadcaster = None): ...
    def submit(self, config: BacktestConfig, triggered_by: str) -> str: ...
    def cancel(self, experiment_id: str) -> bool: ...
    def status(self, experiment_id: str) -> Dict: ...
    def shutdown(self) -> None: ...
```

Uses `concurrent.futures.ThreadPoolExecutor` with daemon=True via custom thread factory. Each worker thread:

```python
def _worker(self, experiment_id: str):
    try:
        exp = self.store.load(experiment_id)
        exp.status = "running"
        self.store.save(exp)

        runner = BacktestRunner(exp.config)
        result = runner.run(
            on_tick=lambda state: self._broadcast_progress(experiment_id, state),
            cancel_token=self._cancel_tokens[experiment_id]
        )
        exp.result = result
        exp.status = result.status
        self.store.save(exp)
        self._broadcast_result(experiment_id, result)

        if self.reviewer_enabled:
            review = self.reviewer.review(exp, self._live_context())
            exp.review = review
            self.store.save(exp)
            self._broadcast_review(experiment_id, review)

    except Exception as e:
        exp.status = "failed"
        exp.result = BacktestResult(..., errors=[{..., traceback: traceback.format_exc()}])
        self.store.save(exp)
        self._broadcast_error(experiment_id, str(e))
        log_to_file("hydra_backtest_errors.log", e)
```

**Mount in `HydraAgent`:**

Strictly additive modifications to `hydra_agent.py`:

```python
# In HydraAgent.__init__ (after line ~1501)
if not os.environ.get("HYDRA_BACKTEST_DISABLED"):
    from hydra_backtest_server import BacktestWorkerPool, mount_backtest_routes
    self.backtest_pool = BacktestWorkerPool(
        max_workers=2,
        store=ExperimentStore(),
        broadcaster=self.broadcaster,
        live_agent=self    # read-only reference for live_context
    )
    mount_backtest_routes(self.broadcaster, self.backtest_pool)
else:
    self.backtest_pool = None

# In HydraAgent shutdown
if self.backtest_pool:
    self.backtest_pool.shutdown()
```

**Refactor `DashboardBroadcaster`** — minimal, backward-compatible:

```python
# Current: broadcast(state) sends raw state dict.
# New: broadcast() wraps in {"type": "state", "data": state}.
#      broadcast_message(type, payload) sends {"type": type, ...payload}.
#      handler now dispatches inbound messages by "type" field.

def broadcast(self, state: dict):
    self.broadcast_message("state", {"data": state})

def broadcast_message(self, type: str, payload: dict):
    msg = {"type": type, **payload}
    # ... existing asyncio send ...

async def _handler(self, websocket):
    self.clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state", "data": self.latest_state}))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                await self._dispatch(msg, websocket)
            except Exception as e:
                await websocket.send(json.dumps({"type": "error", "message": str(e)}))
    finally:
        self.clients.discard(websocket)

def register_handler(self, msg_type: str, fn: Callable): ...
```

Backward-compat: the dashboard's existing code does `setState(data)` on each message. With the new wrapper, dashboard checks `if data.type === "state"` and unwraps. For safety during transition: a `compat_mode` flag lets the broadcaster ALSO emit the raw state for one release, so old dashboards still work during rollout.

#### 6.7 Layer 7: Dashboard (`dashboard/src/App.jsx`, additive inline)

**Tab switcher** at top, above the main grid:

```
┌──────────────────────────────────────────────┐
│ HYDRA                        [LIVE][BACKTEST][COMPARE]  v2.10.0 │
└──────────────────────────────────────────────┘
```

Tabs: `LIVE` (existing dashboard), `BACKTEST` (control panel + experiment library), `COMPARE` (2-experiment diff).

**Backtest tab layout:**

```
┌───────────────────┬──────────────────────────────────┐
│ New Experiment    │  Experiment Library              │
│ ─────────────     │  ─────────────                   │
│ Preset: [default] │  [filter: all/done/running/fail] │
│ Hypothesis:       │  ┌─────────────────────────────┐ │
│ [________________]│  │ 2026-04-15  brain:analyst  │ │
│                   │  │ "Tight RSI in VOL"         │ │
│ Pairs:  [✓][✓][✓] │  │ Sharpe 1.84  MaxDD 8.2%    │ │
│ Mode: (●)C ( )C2  │  │ [REVIEW: PARAM_TWEAK]      │ │
│ Data: 30 days     │  └─────────────────────────────┘ │
│ Fill: realistic   │  ┌─────────────────────────────┐ │
│                   │  │ 2026-04-14  human          │ │
│ PARAMS (sliders): │  │ "Divergent preset test"    │ │
│ [see below]       │  │ Sharpe 0.92  MaxDD 14.1%   │ │
│                   │  │ [REVIEW: NO_CHANGE]        │ │
│ [ RUN BACKTEST ]  │  └─────────────────────────────┘ │
│                   │                                   │
└───────────────────┴──────────────────────────────────┘
```

**Param sliders grid** (8 tunables + Kelly/max_position):

Each param gets a `ParamSlider` component — uses existing neon aesthetic:
- Label + current value on top row
- Range slider below using native `<input type="range">` styled inline
- Numeric input for precise entry
- Reset button per param
- Bounds from `hydra_tuner.PARAM_BOUNDS`

**Observer modal** (the dual-state view):

Summoned automatically when a backtest starts. Slides in from right, takes right third of screen. Resizable via drag on left edge. Collapsible to a pinned progress bar at bottom when minimized.

```
┌──────────────────────┐ ┌────────────────────────────┐
│  LIVE AGENT          │ │ BACKTEST OBSERVER          │
│  (pinned top)        │ │ Experiment: brain-analyst  │
│                      │ │ "Tight RSI in VOL"         │
│  SOL/USD  TREND_UP  │ │ Hypothesis: ...            │
│  BUY 0.72            │ │ Progress: 67% (t=4821/7200)│
│  +$4.20/+1.2%        │ │ Speed: 60x  [▶ PAUSE]      │
│                      │ │                            │
│  BTC/USD  RANGING   │ │ SOL/USD  TREND_UP (sim)   │
│  HOLD                │ │ BUY 0.81 (sim)             │
│                      │ │ +$12.40/+4.7% (sim)        │
│                      │ │                            │
│  [equity chart]      │ │ [equity chart — overlaid   │
│                      │ │  vs live as dotted line]   │
│                      │ │                            │
│                      │ │ Δ Live vs Backtest:        │
│                      │ │   Sharpe: +0.40            │
│                      │ │   P&L: +3.5%               │
│                      │ │   Max DD: -2.1%            │
│                      │ │                            │
│                      │ │ [REVIEW PANEL appears here │
│                      │ │  after backtest completes] │
└──────────────────────┘ └────────────────────────────┘
```

**Review panel** (appears after result + review land):

```
╔══════════════════════════════════════════════╗
║ AI REVIEWER VERDICT: PARAM_TWEAK             ║
║ Confidence: HIGH                             ║
║ ────────────────────────────────             ║
║ Observations:                                ║
║  • Win rate +8pp vs default                  ║
║  • Improvement consistent across 4/4 WF slices║
║  • MC 95% CI on Sharpe delta: [+0.21, +0.52] ║
║                                              ║
║ Proposed change:                             ║
║   momentum_rsi_upper: 70 → 75                ║
║   Scope: regime:VOLATILE                     ║
║   Expected Sharpe impact: +0.3               ║
║                                              ║
║ Rigor gates: [✓ all 7 passed]                ║
║                                              ║
║ [ACCEPT → SHADOW] [REJECT] [PARK] [VIEW CODE]║
╚══════════════════════════════════════════════╝
```

Accept → queues for live shadow validation (I9). Reject → dismisses. Park → saves for later. View code → opens file:line references in reviewer's `source_files_read`.

**New styled components** (all inline, matching neon aesthetic — see research in prior agent outputs):

- `TabBar`, `StyledButton` (primary/danger/secondary), `StyledInput`, `StyledSelect`, `StyledSlider`, `StyledTextarea`, `ParamSlider`, `ProgressBar`, `Modal`, `BacktestPairCard`, `EquityOverlayChart`, `RegimeRibbon`, `MetricsCompareTable`, `ReviewVerdictCard`, `ProposedChangeCard`.

**Compare tab** — side-by-side 2-experiment view with winner-per-metric highlighting.

---

### 7. User Experience Flows

#### 7.1 Human-triggered backtest

1. User clicks Backtest tab
2. Picks preset, sets hypothesis, tweaks sliders, picks data range
3. Clicks RUN
4. WS `backtest_start` sent; server returns `experiment_id`
5. Observer modal appears, pinned to right
6. Progress bar + live dashboard-state updates stream in as `backtest_progress`
7. On completion: `backtest_result` + `backtest_review` arrive; review panel shows verdict
8. User accepts → `shadow_promote` / rejects / parks
9. Experiment appears in library

#### 7.2 Brain-triggered backtest (the key innovation)

Scenario: Risk Manager is considering whether to OVERRIDE a BUY signal in a VOLATILE regime.

1. Risk Manager's LLM response contains a `tool_use` block: `run_backtest(preset="regime_volatile", overrides={"momentum_rsi_upper": 75}, data_range_days=30, hypothesis="Testing if tight RSI upper bound improves VOLATILE performance before overriding current signal")`
2. `hydra_brain._call_llm_with_tools` catches the tool call, routes to `BacktestToolDispatcher`
3. Dispatcher enforces quotas; if OK, creates `Experiment`, submits to `BacktestWorkerPool`, returns `experiment_id`
4. Tool result back to Risk Manager: `{experiment_id, status: "queued", estimated_seconds: 30}`
5. Risk Manager continues deliberating (it has the experiment_id; it can poll or wait)
6. Meanwhile: dashboard observer modal appears, labeled "Triggered by: Risk Manager"; user sees the simulation in real time alongside live
7. Backtest completes, reviewer runs; Risk Manager's next tool call `get_experiment(experiment_id)` returns full result
8. Risk Manager's final decision incorporates the backtest evidence: "I ran a backtest of this scenario with momentum_rsi_upper=75; Sharpe improved +0.3, gates passed. Returning ADJUST with size_multiplier=1.3."
9. Reviewer's verdict + proposed changes appear in the review panel for human to accept/reject

#### 7.3 Reviewer-driven self-improvement cycle

1. After every completed backtest, reviewer runs automatically (if enabled)
2. If verdict is `PARAM_TWEAK` and all gates pass → change appears in "Pending Shadow Validation" section
3. Human clicks Accept → shadow daemon starts running the proposed params alongside live for N ticks
4. After N trades, shadow-vs-live diff is presented; human approves/rejects final promotion
5. If promoted, live tuner's `current_params` is updated and persisted
6. Reviewer self-retrospective logs outcome: "did this tweak materialize the claimed Sharpe delta?"
7. Reviewer's running accuracy score updates

#### 7.4 Failure modes

- Backtest worker crashes → isolated by I5; live agent continues; error logged and shown in dashboard
- Reviewer API unavailable → review step skipped; result still saved; user sees "Review unavailable" banner
- Brain tool quota exceeded → tool returns `{error: "quota_exceeded", retry_after_seconds: ...}`; Brain degrades gracefully (doesn't crash)
- Candle cache corrupt → cache invalidated, re-fetched (respecting rate limit); if fetch fails, experiment fails cleanly
- WS disconnect during backtest → reconnection replays backlog; result still saved server-side

---

### 8. Materiality & Repeatability — the Anti-Handwaving Framework

This section formalizes the user's explicit requirement: "no all-costs, no fake optimals, must be materially impactful and repeatable."

#### 8.1 Seven rigor gates (all must pass for auto-apply eligibility)

| Gate | Check | Default threshold |
|------|-------|-------------------|
| `min_trades_50` | `result.trades >= 50` | 50 |
| `mc_ci_lower_positive` | `mc_ci_95[0] > 0` | 0 |
| `wf_majority_improved` | `wf_improved_slices / wf_slices_tested >= X` | 0.6 |
| `oos_gap_acceptable` | `oos_gap_pct < X` | 30% |
| `improvement_above_2se` | `mc_mean_improvement > 2 * se(mc_iterations)` | 2σ |
| `cross_pair_majority` | `pairs_improved / total_pairs >= 0.5` | 0.5 |
| `regime_not_concentrated` | `max(impr_by_regime) / sum(abs(impr)) < 0.7` | 0.7 |

Thresholds are configurable per deployment via `hydra_reviewer_config.json`; defaults are conservative.

#### 8.2 Scope downgrade rule

If improvement is concentrated in one regime (gate 7 fails), the reviewer may NOT recommend a global change — it must re-scope to `regime:VOLATILE` (or whichever regime dominates). Such scoped changes currently have no live implementation path and are logged as `CODE_REVIEW` (human must manually decide whether to add regime-scoped params).

#### 8.3 Scope downgrade: single-pair concentration

Similar: if `pairs_improved == 1` out of 3, reviewer recommends `pair:SOL/USD` (or whichever) scope, logged as `CODE_REVIEW`.

#### 8.4 Self-consistency

- `expected_impact.sharpe` must equal (within 10%) `mc_mean_improvement`
- `confidence = HIGH` requires `all_gates_passed and wf_improved_slices / wf_slices_tested >= 0.8`
- `confidence = MEDIUM` requires gates passed but WF stability between 0.6 and 0.8
- `confidence = LOW` if any soft inconsistency; auto-downgrades verdict to `CODE_REVIEW`

#### 8.5 Reviewer accuracy tracking

Every reviewer recommendation that progresses to live gets a follow-up "materialization score" 30 days after live promotion. The score asks: did the claimed Sharpe improvement materialize? The reviewer's running accuracy is exposed to itself in subsequent reviews, discouraging confident-but-wrong patterns.

---

### 9. Live-Safety Guarantees (implementation of I1–I12)

| Invariant | Implementation |
|-----------|----------------|
| I1 live cadence | Backtest workers run in daemon threads. Live tick loop is single-threaded, independent. Drift regression test measures tick-duration distribution. |
| I2 separate state | `BacktestRunner.__init__` constructs new `HydraEngine` instances from `config`, never accepts engines. `BacktestToolDispatcher` never receives a reference to the live agent. |
| I3 separate storage | Unit test: filesystem-observing test confirms no writes to live paths during backtest. |
| I4 daemon threads | `ThreadPoolExecutor(thread_name_prefix="backtest-", initializer=lambda: threading.current_thread().daemon=True)` — but ThreadPoolExecutor threads default to daemon in our pool; we set explicitly. |
| I5 exception isolation | `_worker()` has outer try/except that NEVER re-raises. All exceptions → log + failed status + broadcast. |
| I6 kill switch | `os.environ.get("HYDRA_BACKTEST_DISABLED")` gates the entire mount. Agent runs identically to pre-backtest when set. |
| I7 zero drift | `tests/test_backtest_drift.py` replays a known live session snapshot through backtester; asserts tick-by-tick equality of critical engine state. |
| I8 no code auto-apply | `ReviewDecision.verdict == "CODE_REVIEW"` never triggers auto-apply — only generates PR draft file under `.hydra-experiments/pr_drafts/{review_id}.md`. |
| I9 shadow validation | `shadow_promote` WS message queues the change for the shadow daemon, which runs it alongside live; human explicit approval required for final promotion. |
| I10 Kraken-safe data | `KrakenHistoricalSource` uses `KrakenCLI.ohlc()` which respects 2s rate limit. Cache layer prevents redundant fetches. |
| I11 bounded resources | `BacktestWorkerPool(max_workers=2)`; queue depth bounded at 20; per-day experiment count tracked via audit log; per-experiment `max_ticks` enforced in `BacktestRunner.run()`. |
| I12 reproducibility | `BacktestConfig.__post_init__` computes `param_hash = sha256(sorted_param_json)`; `git_sha` from `subprocess.check_output(["git", "rev-parse", "HEAD"])`; stored on result. |

---

### 10–17. Build plan (shipped)

The v2.10.0 build plan (test plan, phases and commits, risks, version management, acceptance criteria, rollback, future extensions, open questions, file manifest, sign-off decisions) shipped and is history: `git show v2.30.0:docs/BACKTEST_SPEC.md`.
