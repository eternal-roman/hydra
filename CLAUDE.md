# CLAUDE.md — Agent Instructions for HYDRA

> **HARD REQUIREMENT.** Update this file in the same change as: module
> add/remove/rename/split, launcher add/remove, version-bump site change,
> new env flag or kill switch, state-file ownership change, safety
> invariant change, CI gate change. If not possible in the same commit,
> leave `TODO(claude-md):` in code AND a matching `<!-- TODO(claude-md): -->`
> here. Stale CLAUDE.md = CI failure waiting to happen.
>
> This file is the hot index — pointers, rules, and cross-cutting
> invariants only. Point, don't duplicate; cold subsystem detail lives in
> the module docstrings, `SKILL.md`, and `CHANGELOG.md`.

## Operating Rules (binding, non-negotiable)

Each was earned through a documented past failure. Violating one is a
regression bug, not a style issue.

1. **Parallel Task agents for any audit > 20 files.** Use N parallel
   agents on `audit.partition` (default 7-way). Each returns HIGH/MED/LOW;
   then synthesize. Scale to 10+ if file count justifies.
2. **Stop processes before editing their state.** A live writer overwrites
   your edit on its next tick. Check ownership in `state_files`; stop
   owner, edit, verify persisted, restart. Snapshot + journal must stay
   in sync — clean both together.
3. **Verify claims with actual commands.** "Verified", "passing", "fixed"
   require running the verification (`pytest`, `git tag -v`, etc.) in the
   same turn and pasting the output. No claims without evidence.
4. **Two-phase self-audit on new code.** After writing, audit for unused
   imports, dead code, unhandled exceptions, null/empty crashes,
   deprecated APIs, misleading errors, false-positive checks. Fix all,
   then a second pass. Only then declare done.
5. **Enumerate all version-bump locations upfront.** Before bumping to
   X.Y.Z, run `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` and confirm every
   site in `version_sites`. Update all in one commit.

## Project

- **HYDRA** — regime-adaptive crypto trading agent for Kraken. Detects
  regime (trending/ranging/volatile), switches between 4 strategies
  (Momentum, MeanReversion, Grid, Defensive), executes limit post-only.
- **Product thesis (evidence-locked):** live engine path is
  **capital preservation** (hold-through + daily trend overlay + friction
  + 15% BUY-only CB) — not a proven growth alpha claim. The only
  after-fee *selection* edge in the ledger is **S3 daily bounce X1 on
  BTC/ETH**, still **shadow-only** (`HYDRA_S3_STRATEGY`, no order path).
  **Heartbeat** is a BTC/ETH order-flow confirmer for display + shadow
  co-log (dashboard P(up); brain advisory); **never** a live BUY/SELL
  gate until a powered bakeoff clears. SOL/ZEC flow FAIL; ZEC S3
  untradable. Ledger: `heartbeat/HONEST_FINDINGS.md` · funnel:
  `heartbeat/evidence/ABI_FUNNEL_2026-07-19.md`.
- **Pairs (default v2.29+):** BTC/USD, ETH/USD, ZEC/USD — three
  independent stable-quoted cores, NO triangle/coordinator (both
  `_derive_triangle`s return None; coordinator is a no-op). The SOL
  triangle was dropped as default after 90d real-tape studies found no
  SOL edge (`heartbeat/evidence/real_tape/calibrate_SOL_USD.txt` AUC
  0.56 FAIL) and the bridge was already proven dead. Explicit SOL pairs
  remain fully supported: `--pairs SOL/USD,SOL/BTC,BTC/USD` re-activates
  the TradingTriangle + CrossPairCoordinator unchanged.
  `STABLE_QUOTES = {USD, USDC, USDT}` are first-class. **`--pairs auto`**
  seeds the three cores in the *funded* stable quote (keep `--quote` /
  `HYDRA_QUOTE` when that pool is above costmin; otherwise switch to the
  largest funded stable — a USDC-only book must not run USD cores at $0
  cash) and adds one satellite pair per additional held asset — held SOL
  becomes a normal tradable satellite (USDC-quoted when USDC is funded,
  else USD; `HYDRA_AUTO_QUOTE` forces) —
  `hydra_agent.discover_portfolio_pairs`.
- **Bridge is signal-only by default (v2.28):** when a SOL triangle is
  explicitly configured, SOL/BTC engines run `exit_only` drain mode
  (SELLs flow until flat, BUYs refused) — isolation study on real 1h
  tape showed zero 1y trades and a Sharpe drag when included
  (`.hydra-flywheel/bridge_isolation.json`). `HYDRA_BRIDGE_TRADING=1`
  opts back in.
- **Candles default 60m (v2.28):** the hold-through rails and friction
  hurdle were calibrated on 1h tape; 15m ran them off-calibration.
  `--candle-interval` still accepts 1/5/15/30/60; snapshot resume drops
  candle history on interval mismatch (positions/journal restore).
  v2.29 three-core `--resume` remaps same-base stable keys
  (BTC/USD snap → BTC/USDC engine) even when `triangle` is None;
  mixed leftover quotes (ZEC/USD) stay exact — never a global
  quote flip that would invent ZEC/USDC.
- **Version pin:** v2.33.2

## Defaults (inherited)

- Engine: Python stdlib only (no numpy/pandas in engine)
- Orders: limit post-only (`--type limit --oflags post`). Never market.
- Engine isolation: one HydraEngine per pair, no shared state
- Kraken CLI: **pinned v0.4.1**; `wsl -d $HYDRA_WSL_DISTRO -- bash -c "source ~/.cargo/env && kraken ..."`
  (distro from `hydra_kraken_cli.WSL_DISTRO`, default `Ubuntu`; verify via `wsl -l -v`).
  Flag-by-flag compatibility notes and the deliberately-unused v0.4.1 surface
  (`order amend`/`batch`, `workspace`/`tape`/`lab`/`mcp`, …) live in the
  `KrakenCLI` docstring. v0.4.1 `ohlc` emits `{candles:[{time,open,...}],last,pair}`
  (object array, not the legacy pair-keyed list-of-lists) — `ohlc_paged`
  accepts both. v0.4.1 WS **does not print** `{"channel":"heartbeat"}` on
  stdout (swallowed at the JSON sink; `--monitor` health is stderr).
  ExecutionStream / BalanceStream therefore treat process+reader+snapshot
  as healthy; a 30s stdout-heartbeat timeout is public-stream only.
- Kraken REST min interval: **2s** between calls
- min_confidence: 0.65 (both modes); warmup_candles: 50
- Circuit breaker: **15% drawdown sticky-halts new BUYs for session; SELL flatten still allowed (PR-A)**
- WS dashboard port: 8765; Vite dev: 3000 (`strictPort: true`)
- CI authority: `.github/workflows/ci.yml` (jobs: `engine-tests`,
  `dashboard-build`)

## Cross-cutting invariants (HIGH severity if violated)

- **SPOT-ONLY execution** — Hydra places orders ONLY on Kraken spot pairs (default v2.29+: BTC/USD, ETH/USD, ZEC/USD; a SOL triangle when explicitly configured). Derivatives data (Kraken Futures funding/OI via `kraken futures tickers` CLI) is SIGNAL INPUT ONLY. No futures, no options, no margin orders placed. `hydra_derivatives_stream.py` is read-only by construction; its test suite greps for authenticated subcommand names and fails if any appear.
- **Limit post-only, never market** — deliberate design choice
- **No REST for market data** — all Kraken market data flows through the WebSocket streams or the `kraken` CLI (WSL Ubuntu). New data sources must use CLI or WS.
- **2s REST floor** — Kraken throttles or bans below this
- **15% drawdown sticky-halts new BUYs for session** — SELL flatten still allowed when `position.size > 0` (PR-A); both `tick()` and `_maybe_execute` check
- **The circuit-breaker halt is cleared only by an explicit operator act, and it arms on the CURRENT drawdown (v2.32)** — `halted` restores from the snapshot, so "for session" above really meant *forever* under `--resume` (which is what production runs). It now prints a loud RESUMED-STILL-HALTED banner at boot and is cleared only by `HYDRA_RESET_CIRCUIT_BREAKER=1`. The reset clears the FLAG only — `peak_equity` / `max_drawdown` are preserved as the record. `tick()` therefore arms off `drawdown` (equity vs peak, this tick), **never** `self.max_drawdown`: that field is a monotone high-water mark nothing lowers, so arming on it re-halted the engine on the very next tick after a reset — even from a fully recovered account — making the documented escape hatch a no-op. Still-underwater re-arms immediately, which is the intent. Never auto-clear on resume: that silently re-enables risk after a breach.
  Both halves behave identically: `hydra_engine.tick` arms on the tick's `drawdown` and `hydra_agent._build_dashboard_state` arms on `cur_dd`. Neither reads its monotone running max, which stays purely as the record.
- **Deterministic guardrails must not depend on the LLM layer (v2.32)** — `apply_rules` / `evaluate_qfe` used to be reachable only from inside `_apply_brain`, so with no LLM key configured the entire R1-R11 + QFE stack silently did not run while the derivatives feed kept streaming to the dashboard. `HydraAgent._apply_quant_guardrails` is the brain-free path (positioning_bias `""`, so R8 cannot fire); the tick loop calls it for any actionable signal when `self.brain` is None, and boot prints which guardrail layer is live. Anything that makes rules conditional on the brain again is a HIGH regression.
- **Every pair with a state must land in `all_states`** — that dict is the sole input to Phase 2.5, so the `all_states[pair] = state` assignment belongs OUTSIDE the brain / brain-free / cached-replay branches. Nesting it inside one arm silently drops the other arms' pairs from execution entirely — no entries and **no exits** — while ticks, logs and the dashboard all look healthy.
- **Backtest candles align on TIMESTAMP, never on index (v2.32.1)** — `BacktestRunner._loop` advances only the pairs whose next bar sits at the earliest timestamp across pairs; a pair with no bar there is absent for that tick. Index-zipping silently accumulated cross-pair skew on any gapped series (which the real sqlite store has — `HistoryStore.coverage` tracks `gap_count`), corrupting coordinator decisions and the summed equity curves that `tools/flywheel_validation.py` writes as the evidence gate. Never reintroduce a bare `next(it)` per pair per tick.
- **Trade-level Sharpe annualizes by sqrt(TRADES/yr), not sqrt(bars/yr) (v2.32.1)** — `monte_carlo_resample` operates on one return per TRADE. Scaling those by `annualization_factor(candle_interval_min)` inflated Sharpe ~93x on 60m bars and made the `mc_ci_lower_positive` rigor gate unable to bind. Pass `trades_per_year`, or accept the honest unannualized per-trade figure.
- **Nothing reachable from the brain's tool loop may run inline (v2.32.1)** — the tool dispatcher executes on the LIVE TICK THREAD. `run_backtest` and `sweep_param` both route through `BacktestWorkerPool` when mounted (I1). A new long-running tool handler must do the same or it stalls ticks, fill reconciliation and shutdown while positions are open.
- **Stream recovery runs in `finally` (v2.32)** — `ensure_healthy()` for the candle/ticker/balance/book streams sits in the tick body's `finally`, never the try body. A dead stream is the most common cause of a tick crash, so gating recovery on tick success is self-sustaining: crash → no restart → identical crash next tick, permanently, including no exits. Related: `engine_states[pair]` is explicitly `None` for a skipped pair, so consumers must use `.get(pair) or {}` — `.get(pair, {})` does NOT apply its default when the key exists.
- **The dashboard WS authenticates BOTH directions (v2.32.1)** — an accepted socket lands in `DashboardBroadcaster.pending` and receives **nothing**: no `latest_state`, no broadcast. It graduates into `clients` only via `_promote_client`, reached by a `{"type":"auth"}` handshake (or any command carrying a valid credential) that passes `_client_authenticated` — per-process token, or JWT in production mode. Unauthenticated sockets are closed after `HYDRA_WS_AUTH_GRACE_S`. Loopback binding narrows *who can reach the port*; it is no longer the thing protecting the account. `_origin_allowed` deliberately fails open for a missing `Origin` (tests, CLI, Electron) and is browser-CSRF defence only — never restore state-on-connect behind it. Client side: `connected` flips on `auth_ack`, not on socket open.
- **RSI/ATR = Wilder exponential smoothing, NOT SMA** (Bollinger = population variance)
- **SKIP ≠ BLOCK** — a soft restriction skips an action for the tick; BLOCK is reserved for hard rules (the 15% drawdown breaker)
- **`HYDRA_COMPANION_LIVE_EXECUTION` default OFF** — proposals are paper until opted in
- **Funding is markPrice-relative, never absolute** — Kraken Futures `PF_*` `fundingRate` is absolute USD-per-contract-per-period. Convert to bps via `(fundingRate / markPrice) * 10000`, never `fundingRate * 10000`. The `_absolute_to_relative_bps` helper in `hydra_derivatives_stream.py` enforces this (±500 bps clamp vs API drift). Pre-v2.15.2 fires used the wrong absolute conversion — not authoritative.
- **Synthetic pairs declare themselves to R10** — `DerivativesSnapshot.synthetic=True` propagates to `quant_indicators["synthetic_pair"]`; R10 then tracks only funding/cvd/regime (the fields the synthetic path actually populates). Adding a new pair without a direct Kraken Futures perp requires this flag, otherwise R10 will structurally force-hold every tick.
- **Perp-only pairs declare themselves to R10 (v2.29)** — pairs whose Kraken Futures listing has a perp but NO quarterly contracts (ZEC: `PF_ZECUSD`) get `DerivativesSnapshot.basis_available=False`, derived from `SPOT_TO_DERIVATIVES.quarterly_prefix is None` at construction — map-driven, never from data presence. It propagates to `quant_indicators["basis_available"]`; R10 then tracks 4 fields (drops `basis_apr_pct`). Without it a perp-only pair sits permanently at 1 stale field and any transient miss trips a structural force-hold.
- **Uncovered pairs declare themselves to R10** — pairs with no `SPOT_TO_DERIVATIVES` entry at all (portfolio satellites, e.g. NIGHT/USD) get `quant_indicators["derivatives_covered"]=False` from `_build_quant_indicators`; R10 then tracks only CVD. Coverage is structural (pair in the futures map), never "snapshot present" — a covered pair with a warming/stale stream must still hit the R10 blackout.
- **Per-quote balance pools (v2.28)** — live stable-quoted engines are funded from the REAL holding of their own quote currency split across pairs sharing that quote (`_set_engine_balances`); a USDC engine never sizes against USD it cannot spend. Zero pool ⇒ balance 0 (sizer refuses entries) but `tradable` stays True so inventory can exit. Paper keeps the uniform split.
- **Gross vs free balance are different numbers (v2.32)** — `BalanceStream.latest_balances()` / `_cached_balance` are GROSS (include funds locked behind our own resting post-only orders) and are what equity, peak equity and the portfolio drawdown breaker must read; held funds are still ours. `BalanceStream.latest_free_balances()` / `KrakenCLI.free_balance()` (`kraken extended-balance`) are NET of holds and are what every *spendability* decision must read — `_get_real_quote_balance` is that chokepoint. Sizing against gross re-commits money an unfilled order already owns, which is the `PLACEMENT_FAILED: insufficient_<quote>_balance` loop. Both paths fail OPEN to gross when the hold field is absent.
- **Deterministic size multipliers must survive every later sizing step** — a de-risking `size_multiplier` (brain quant × RM, or the R3/R5/R7 penalty stack) is only real if it reaches the placed order. Any floor/override added after `size = size * effective_mult` in `_maybe_execute` must scale by `effective_mult` too, or it silently discards the whole risk stack (the v2.32 conviction-sizing fix). PR-B's "`max_position_pct` applies **after** brain `size_multiplier`" is the invariant.
- **Trend overlay is evidence-gated and fails open** — every consumer of `daily_trend_long()` must treat `None` (warmup / disabled) as "behave exactly as pre-overlay". The daily-entry path (enter on ensemble alone) was tested and REJECTED (whipsawed against 1h flattens, −5.4% vs +0.1% 2y — `.hydra-flywheel/trend_entry_gate.json`); do not re-add without a passing gate. Daily closes are seeded at boot (agent: Kraken 1440m OHLC; backtest: pre-window sqlite) and persist in the snapshot.
- **`exit_only` drain mode** — engine-level flag: BUY entries refused (SKIP semantics), every SELL path untouched. Set per-session by the agent (never persisted); the bridge default uses it. Composes with hold-through and the CB.
- **`hydra_rm_features.py` is pure** — no I/O, subprocess, network, or file access; every function returns `Optional[float]` (or `Optional[dict]`) from input alone, returning `None` on insufficient data. A future contributor adding side effects breaks the "fails-silent with None" contract that lets R10 and RM reason over missing vs corrupted data and that lets `HYDRA_RM_FEATURES_DISABLED` work as an instant rollback.
- **Research surfaces never place orders** — `hydra_s3` / `s3bounce` and
  `hydra_heartbeat_surface` / `heartbeat` are signal, display, shadow, or
  brain-advisory only. Grep-guards in tests forbid order verbs. No
  `HYDRA_S3_LIVE` or engine SKIP from heartbeat without a powered
  pre-registered bakeoff (engine co-occurrence currently FORBID claims:
  0 BUYs / 365d under rails).
- **`PLACEMENT_FAILED` entries are session-only** — pre-exchange diagnostics (`insufficient_USD_balance`, `placement_error:api`) live in the in-memory `HydraAgent.order_journal` for live debugging but MUST NOT persist to `hydra_session_snapshot.json` or the rolling `hydra_order_journal.json`. The `_journal_for_persistence()` helper is the single chokepoint; both write paths (`_save_snapshot` and the per-tick rolling write) go through it. If you add a third write path, route it through the helper too.
- **Pair identity has one source of truth** — `hydra_pair_registry.PairRegistry` owns alias resolution (XBT↔BTC, XZEC↔ZEC, ZUSD↔USD, USDC.F→USDC, slashed↔slashless, case-insensitive) and per-pair metadata (price decimals, ordermin, costmin, tick size). `hydra_kraken_cli.KrakenCLI` delegates to the class-level `registry`. New pair-handling code must consume the registry — never re-implement an alias dict. v2.19 absorbed 1048 USDC literals into a single registry + role binding. **CLI schema drift degrades to the registry, never to a literal:** `load_pair_constants` skips (loudly) any pair missing/unparseable `pair_decimals`/`ordermin`/`costmin` rather than overlaying a generic default — a renamed key once gave every pair `ordermin=0.02`, which makes `write_off_dust` erase sub-0.02 BTC positions. `KrakenCLI.balance()` likewise skips unparseable entries instead of raising into three callers with no try/except.
- **Roles, not literal pair names, in coordinator/agent logic** — CrossPairCoordinator and HydraAgent address pairs by their `TradingTriangle` role (`stable_sol`, `stable_btc`, `bridge`), not by hardcoded `"SOL/USDC"` etc. `STABLE_QUOTES = {USD, USDC, USDT}`; the engine treats every member as $1. Switching the default quote is a config flip, not a refactor — see `hydra_config.HydraConfig.from_quote`.
- **R11/QFE is exit-only, profit-only, squeeze-filtered** — `evaluate_qfe()` in `hydra_quant_rules.py` lets a SELL through force_hold ONLY when: position is in profit (≥`QFE_MIN_PROFIT_PCT` = 1.0% mark, fee-cushioned), the engine already generated SELL, and no **deterministic** squeeze catalyst is present (`short_squeeze` OI regime, or extreme-short-funding + accumulation CVD). LLM `positioning_bias=crowded_short` alone does **not** veto QFE. QFE must never open a position, must never fire on an underwater position, and force_hold remains active for entries after QFE exits. Every QFE event logs a full trigger snapshot via `qfe_trigger_values` in `state["ai_decision"]`.
- **Exit guarantees (PR-A)** — Circuit breaker blocks BUY only; SELL always allowed when `position.size > 0` (halt flatten). **`tradable=False` likewise blocks entries only** — it is derived from the QUOTE balance, which a SELL does not spend, so gating exits on it stranded inventory forever (bridge `exit_only` drain, or a satellite whose quote pool emptied while holding the base) with the engine breaker suppressed and nothing else to force the flatten. SELL ignores `min_confidence` (entries still require it). R2 force_holds extreme-negative-funding **BUY** (bounce-chase), never spot SELL (long close).
- **Hard risk caps (PR-B)** — `max_position_pct` applies **after** brain `size_multiplier` and caps gross inventory (notional/equity). Peak equity never rebases downward on balance seed/resume, except a live first-seed of an unfunded quote must not treat constructor `--balance`/N as peak (that printed DD 100% and armed the BUY halt). Snapshot peaks stay. Portfolio max DD ≥ 15% sticky-blocks new BUYs (SELL still allowed).
- **Fill true-up (PR-C)** — Every terminal FILLED/PARTIAL restores `pre_trade_snapshot` and replays at exchange `avg_fill_price` (not candle close). `pre_trade_snapshot` **is** persisted (only `PLACEMENT_FAILED` entries are stripped by `_journal_for_persistence`), so `_reconcile_stale_placed` trues up a previous-session fill and rolls back a phantom position on CANCELLED/REJECTED exactly like `_apply_execution_event` does live — treating it as in-memory-only is what left resume passing `None` and skipping both repairs. Unsellable dust below ordermin is written off. BUY limit offsets capped (≤20 bps SOL/STABLE) for post-only fill rate.
- **Kelly / friction honesty (PR-D)** — PositionSizer uses excess-over-threshold Kelly (conf=min → edge 0.10, conf=1 → 1.0), not `(conf*2-1)`. Friction hurdle is timeframe-aware (≥2.0% on 1h+ bars). Go-live plumbing gates: `python scripts/go_live_gates.py`.
- **Quant/cross-pair (PR-E)** — `HYDRA_QUANT_INDICATORS_DISABLED=1` skips `apply_rules`/QFE (no R10 blackout). Rules re-applied after brain OVERRIDE. Rule 2 recovery preferred over Rule 3 swap; Rule 3 requires bridge `tradable` (emitted on engine state from `_build_state`). Always `tick(generate_only=True)` then post-coord execute. USDT pairs mapped in `SPOT_TO_DERIVATIVES`. Companion live (opt-in) registers orders on `ExecutionStream` but remains engine-inventory-blind until a full agent place adapter exists.
- **Unified warmup (PR-F)** — `SignalGenerator.WARMUP_CANDLES = 50` (aligned with regime detector).

Subsystem detail (indicators, regime, Kelly sizing, price precision,
execution stream lifecycle, resume reconciliation, forex modifier,
shutdown) lives in the `hydra_engine.py` / `hydra_agent.py` docstrings and `SKILL.md`.

## Modules (thin index — details in deep specs)

| id | file | role |
|---|---|---|
| engine | `hydra_engine.py` | indicators, regime, signals, sizing, hold-through rails, daily trend overlay |
| agent | `hydra_agent.py` | live agent: Kraken CLI via WSL, WS broadcast, execution, reconciler, snapshot + `--resume` |
| brain | `hydra_brain.py` | 3-agent AI: Market Quant + Risk Manager + Grok Strategist |
| derivatives_stream | `hydra_derivatives_stream.py` | Kraken Futures public data via kraken CLI (funding, OI, basis) — read-only, SIGNAL INPUT ONLY |
| quant_rules | `hydra_quant_rules.py` | R1-R11 deterministic guardrails (funding extreme, OI regime, basis euphoric, CVD divergence, contrarian edge, staleness, QFE profit exit) |
| rm_features | `hydra_rm_features.py` | pure engine-internal RM signals (realized vol, DD velocity, fill rate, slippage, cross-pair corr, idle minutes) — stdlib only, no I/O, no mutation |
| tuner | `hydra_tuner.py` | self-tuning params; `apply_external_param_update` + `rollback_to_previous` (depth=1 deque) |
| companions | `hydra_companions/` | chat/proposals/nudges/ladder/live executor/souls; per-companion memory is local JSONL (`.hydra-companions/memory/`) |
| backtest | `hydra_backtest.py` | replay engine; reuses HydraEngine verbatim; `HYDRA_VERSION` lives here |
| backtest_metrics | `hydra_backtest_metrics.py` | bootstrap CI, walk-forward, Monte Carlo, regime P&L, sensitivity |
| backtest_server | `hydra_backtest_server.py` | `BacktestWorkerPool` (max=2 daemon, queue=20) + WS via `mount_backtest_routes` |
| backtest_tool | `hydra_backtest_tool.py` | 8 Anthropic tool schemas + dispatcher + `QuotaTracker` (10/d caller, 3 concurrent, 50/d global) |
| experiments | `hydra_experiments.py` | `Experiment` + `ExperimentStore` (RLock); 8 presets; sweep/compare |
| journal_maintenance | `journal_maintenance.py` | journal audit + lockstep purge (agent must be stopped) |
| journal_migrator | `hydra_journal_migrator.py` | one-shot legacy journal migration (auto on first start) |
| dashboard | `dashboard/src/App.jsx` | React LIVE/RESEARCH/SETTINGS; RESEARCH split under `components/` |
| pair_registry | `hydra_pair_registry.py` | single source of truth for pair metadata; `Pair` value object + `PairRegistry` (alias resolution, kraken-pairs bootstrap); `STABLE_QUOTES`, `normalize_asset` |
| config | `hydra_config.py` | `TradingTriangle` role-binding + `HydraConfig.from_quote`; `--quote` / `HYDRA_QUOTE` select the `--pairs auto` fallback quote (`DEFAULT_QUOTE = USD`) |
| state_migrator | `hydra_state_migrator.py` | one-shot quote-currency migration of `hydra_session_snapshot.json` (engines, regime history, derivatives); preserves `order_journal` audit trail |
| heartbeat | `heartbeat/` + `hydra_heartbeat_surface.py` | Order-flow P(up) confirmer (BTC/ETH PASS). **No order path.** Separate `heartbeat run` (`start_heartbeat.bat`); status `heartbeat_status_<PAIR>.json`; USDC/USDT engines fall back to the USD tape (same-base order flow). Agent → `quant_indicators["heartbeat"]` + dashboard; kill `HYDRA_HEARTBEAT_SURFACE=0`. Ledger: `heartbeat/HONEST_FINDINGS.md` |
| s3 | `hydra_s3.py` + `s3bounce/` | Daily bounce X1 signal (BTC/ETH; ZEC breadth-only). Read-only QI + **shadow** (`HYDRA_S3_STRATEGY=1`, default off, `.hydra-s3/`). **No order path.** Evidence: `heartbeat/evidence/bakeoffs/s3_*`, `research/S3_*` |
| flywheel | `hydra_flywheel.py` | paper capital allocator (CLI-only, NO live order path, not wired into agent capital): signal-driven daily trend ensemble + carry monitor + cash; **only** the legacy engine sleeve is evidence-gated (0% until `validation_results.json` clears). Research tools: `tools/flywheel_validation.py`, `tools/carry_backtest.py`, `tools/trend_backtest.py` (trend/carry JSONs are research-only) |

## Deep specs

- `SKILL.md` — trading formulas + risk rules · `CHANGELOG.md` · `SECURITY.md`
- `docs/BACKTEST.md` runbook · `docs/BACKTEST_SPEC.md` design archive (defaults: code)
- `docs/COMPANION_SPEC.md` · `docs/HOLD_THROUGH.md`
- `heartbeat/HONEST_FINDINGS.md` — research verdict ledger (S3 + heartbeat)
- `research/RETAIL_CRYPTO_EDGE_2026.md` · `research/S3_BOUNCE_EDGE_2026.md` + `research/data/`
- Root `AUDIT_*.md` gitignored local snapshot only (not product truth)

## Agent tooling

- **Skills:** `/release` (release SOP), `/audit` (zero-skip review), `/bakeoff` (candidate signal vs current system on real data), `/review`, `/security-review`
- **Post-edit hook:** `.claude/hooks/post-edit.py` — path-scoped verification; advisory; silence with `HYDRA_POSTEDIT_HOOK_DISABLED=1` (wired in `.claude/settings.json`)
- **Settings split:** per-user `.claude/settings.local.json` + runtime `.claude/scheduled_tasks.lock` gitignored; everything else under `.claude/` committed
- **gitattributes pin:** `*.sh text eol=lf` — prevents Windows core.autocrlf CRLF-ing hook shebang

## State files

| id | path | ownership / notes |
|---|---|---|
| snapshot | `hydra_session_snapshot.json` | atomic `.tmp → os.replace`; `--resume` target; embeds v2.18.0 `derivatives_history` (OI + mark-price deques, rehydrated with 30 min staleness gate). Same-base stable remap on resume when triangle is None (BTC/USD → BTC/USDC); journal pair fields stay on the market they traded |
| order_journal | `hydra_order_journal.json` | snapshots immediately on any tick that appends (crash cannot lose since last successful tick); gitignored |
| params | `hydra_params_<pair>.json` | per-pair learned tuning params; gitignored |
| errors_log | `hydra_errors.log` | tick try/except writes here with full traceback; loop continues |
| companion_memory | `.hydra-companions/memory/{user}_{companion}.jsonl` | per-companion distilled facts; local JSONL, authoritative, 4KB LRU budget; gitignored |
| experiments_store | `.hydra-experiments/` | owner `experiments`; `presets.json` bootstraps from code on first init (delete to regenerate) |
| s3_shadow | `.hydra-s3/` | owner `s3` (`s3bounce.ShadowLedger` via `hydra_s3`); `events.jsonl` append-only audit + `state.json` open shadow positions/proposal dedupe (atomic `.tmp → os.replace`; garbage state treated as empty, events remain the audit trail); survives `--resume` independently of the snapshot; gitignored |
| flywheel_store | `.hydra-flywheel/` | owner `flywheel`; `state.json` paper ledger (atomic `.tmp → os.replace`), validation/carry/trend evidence JSONs, downloaded funding history; gitignored |

## Env flags (kill switches + opt-ins)

| flag | scope | effect |
|---|---|---|
| `HYDRA_BACKTEST_DISABLED` | backtest | kill when `=1` only; worker pool off, WS rejects backtest msgs |
| `HYDRA_BRAIN_TOOLS_ENABLED` | brain | enables Anthropic tool-use for Analyst+RM (Grok stays text-only) |
| `HYDRA_QUANT_INDICATORS_DISABLED` | brain/quant | `=1` skips DerivativesStream + R1-R11 quant rules; Quant sees no funding/OI/CVD block and no force_hold from rules |
| `HYDRA_TAX_FRICTION_FLOOR_USD` | brain | Tax/fee friction floor in USD (default `50.0`; `hydra_brain.TAX_FRICTION_FLOOR_USD`). On a SELL that would realize a gain below the floor, the analyst prompt gets a soft advisory line — **advisory only, never a gate**. `=0` suppresses it; cutting a loss or banking a gain ≥ floor never triggers it. |
| `HYDRA_COMPANION_DISABLED` | companion | kill (no orb) |
| `HYDRA_COMPANION_PROPOSALS_ENABLED` | companion | default on; `=0` for no trade cards |
| `HYDRA_COMPANION_NUDGES` | companion | default on; `=0` for no proactive messages |
| `HYDRA_COMPANION_LIVE_EXECUTION` | companion | **opt-in** real-order execution; **default OFF for money safety** |
| `HYDRA_POSTEDIT_HOOK_DISABLED` | tooling | silence hook during heavy refactors |
| `HYDRA_RM_FEATURES_DISABLED` | rm_features | `=1` skips engine-internal feature computation in `_build_quant_indicators`; instant rollback without redeploy. Default off (features enabled). |
| `HYDRA_BUY_OFFSET_DISABLED` | execution | `=1` reverts BUYs to raw bid (default off). Offset table: `hydra_agent.py:_BUY_LIMIT_OFFSET_BPS` keyed by `(base, quote_class, regime)`; only SOL bases in `VOLATILE`/`TREND_DOWN` carry offsets — BTC bases and RANGING/TREND_UP stay at raw bid (avoid missed fills). Empirical derivation in the code comment. |
| `HYDRA_QUOTE` | config | Fallback quote for `--pairs auto` (`USD`/`USDC`/`USDT`). `--quote` > env > `DEFAULT_QUOTE` (USD). If that pool is unfunded, cores switch to the largest funded stable. Explicit `--pairs` is unchanged. |
| `HYDRA_BRIDGE_TRADING` | agent | `=1` re-enables SOL/BTC bridge trading. Default OFF (v2.28): the bridge runs exit_only drain mode — evidence in `.hydra-flywheel/bridge_isolation.json` (0 trades/1y; Sharpe drag 2y). Candles/synthetic funding still stream as signal input. |
| `HYDRA_TREND_OVERLAY` | engine | **Default ON** (v2.28). Daily trend-ensemble gate: BUY additionally requires daily ensemble long (0.4·sma200 + 0.4·ema20x100 + 0.2·don55 on daily closes, long ≥ 0.6); open positions flatten on ensemble flip. Fails OPEN (None) below 210 daily closes or when `=0`. Won 6/6 real-tape windows (`.hydra-flywheel/trend_overlay_gate.json`). |
| `HYDRA_TREND_CONVICTION_SIZING` | engine | **Default ON** (v2.28). Overlay-long entries allocate vol-target × max_position_pct of balance (Kelly is the floor). `=0` reverts to pure Kelly sizing. Won 3/3 windows (`.hydra-flywheel/conviction_sizing_gate.json`). |
| `HYDRA_TREND_TARGET_VOL` | engine | Annualized vol target (percent) for overlay sizing. Default `30.0`. |
| `HYDRA_AUTO_QUOTE` | agent | Forces the satellite quote for `--pairs auto` (`USD`/`USDC`/`USDT`). Default unset: prefer USDC when funded, else `--quote`/`HYDRA_QUOTE`. USD stays required for USD-only listings (e.g. NIGHT/USD). |
| `HYDRA_TAPE_CAPTURE` | history | `=1` (default) wires CandleStream candle-close pushes into a bounded-queue writer that upserts to `hydra_history.sqlite` (`source='tape'`). Set `=0` to disable (e.g. paper-mode tests on a shared DB). |
| `HYDRA_HISTORY_DB` | history | Path override for the canonical OHLC store. Defaults to `hydra_history.sqlite` in the working directory. Used by the agent (tape capture), `tools/refresh_history.py`, and the SqliteSource backtest path. |
| `HYDRA_WSL_DISTRO` | cli | WSL distribution name for all `kraken` CLI invocations. Defaults to `Ubuntu`. Override if your distro is named differently (e.g. `Ubuntu-24.04`). Single source of truth: `hydra_kraken_cli.WSL_DISTRO`; isolated modules read the env var directly. |
| `HYDRA_FRICTION_GATE_DISABLED` | engine | `=1` disables the friction expectancy gate (v2.27): BUY entries whose strategy-implied expected move (BB-mid reversion distance or 2×ATR%) is under `FRICTION_HURDLE_MULT × ROUND_TRIP_FRICTION_PCT` (0.84%) are skipped (SKIP semantics). Entries only — exits never gated; fails open on insufficient history. Active on BOTH `tick()` and `execute_signal()` paths. |
| `HYDRA_HOLD_THROUGH` | engine | **Default ON** (all pairs). TREND_UP BUY ≥0.65, flatten `TREND_DOWN`, ride mid-UP except extreme overbought. `=0` = raw engine (research/tests). Does not disable friction or 15% CB. Spec: `docs/HOLD_THROUGH.md`. Replaces removed `HYDRA_REGIME_SELECTIVE`. |
| `HYDRA_S3_DISABLED` | s3 | `=1` removes the S3 signal surface entirely (no daily tracking, no `quant_indicators["s3"]`, no shadow). Read per call — live-flippable. Default unset (signal ON). |
| `HYDRA_S3_STRATEGY` | s3 | **Default OFF.** `=1` enables the S3 shadow strategy: gated entryable-b1 signals are logged as proposals with per-exit-arm paper positions in `.hydra-s3/`. Structurally shadow-only — this flag has NO code path to an order; live enablement is a future, gate-pending PR (needs the shadow window + `/bakeoff` to clear). |
| `HYDRA_S3_HEARTBEAT_STATUS_DIR` | s3 + heartbeat surface | Directory of heartbeat status files (`heartbeat_status_<PAIR>.json`). Default `heartbeat/data`. Missing/stale(>300s)/tainted ⇒ `no_opinion` (never fabricate 0.5). Used by S3 shadow confirmer and dashboard surface. |
| `HYDRA_HEARTBEAT_SURFACE` | agent/dashboard | **Default ON.** `=0` removes `quant_indicators["heartbeat"]` (P(up) display). Read-only — no order path. Requires separate `heartbeat run` process for live values. |
| `HYDRA_FEE_DEDUCTION_DISABLED` | agent | `=1` reverts fee-true accounting (v2.27): confirmed fills debit `lifecycle.fee_quote` from the engine's quote balance exactly once (idempotent via `lifecycle.fee_applied`). Default off (fees deducted) — pre-v2.27 live P&L was overstated ~16 bps/fill vs the backtest, which always deducted fees. |
| `HYDRA_WS_HOST` | dashboard | Bind address for the dashboard WebSocket. Default **`127.0.0.1`** (v2.32.1). The auth handshake, not the bind address, is what keeps account state private — see the WS auth invariant above. Set `=0.0.0.0` only behind a proxy that terminates auth. |
| `HYDRA_WS_AUTH_GRACE_S` | dashboard | Seconds an accepted-but-unauthenticated dashboard socket may stay open before the reaper closes it (`1008 auth required`). Default **`10`**. Lower it to shrink the window for connection-slot exhaustion; raise it only if a slow client legitimately needs longer to fetch `hydra_ws_token.json`. Does not affect what an unauthenticated socket can read — that is already nothing. |
| `HYDRA_RESET_CIRCUIT_BREAKER` | engine/agent | `=1` clears a persisted 15% circuit-breaker halt once, at resume — both the per-engine `halted` flag and the portfolio-wide BUY halt. Default unset (a breach persists across `--resume`, which is the safe direction). Clears the FLAG only: `peak_equity` / `max_drawdown` / `_portfolio_max_drawdown_pct` are preserved as the record. Both halts then re-arm only if the CURRENT drawdown is still ≥15%. Use after reviewing the drawdown, not as a routine flag. |
| `HYDRA_CLI_LEGACY_SECRET_EXPORT` | cli | `=1` restores the pre-v2.32 behavior of interpolating `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` into the `bash -c` string. Default off: secrets are passed through the child process ENVIRONMENT and forwarded via `WSLENV`, keeping them out of the `wsl` process argv (readable via `ps`/procfs by any local user). Single chokepoint `KrakenCLI.forward_credentials`, shared by the REST wrapper **and** the long-lived `hydra_streams.BaseStream` subprocesses (which hold their argv for the whole session — the longer disclosure window of the two). Only affects the multi-tenant path where the keys are already in Hydra's own environment. |

## Build / run

- Dashboard dev: `cd dashboard && npm install && npm run dev`
- Agent default: `python hydra_agent.py --balance 100` (BTC/USD, ETH/USD, ZEC/USD)
- Agent SOL triangle (legacy): `python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD` (re-activates TradingTriangle + coordinator; registry quote-agnostic, USDC/USDT variants work)
- Agent competition: `python hydra_agent.py --mode competition`
- Agent paper: `python hydra_agent.py --mode competition --paper`
- Agent resume: `python hydra_agent.py --mode competition --resume`
- Engine demo (no keys): `python hydra_engine.py`

**Launchers:**
- `start_hydra.bat` — production watchdog (`--pairs auto --mode competition --resume` — **do not remove these flags**). Starts `start_heartbeat.bat` once before the restart loop (idempotent if heartbeat.exe is already up).
- `start_all.bat` — full stack: dashboard + agent watchdog (heartbeat starts from `start_hydra.bat`)
- `start_dashboard.bat` — dashboard only
- `start_heartbeat.bat` — `heartbeat run` for BTC/USD + ETH/USD (research P(up) status files; **no order path**). USDC-quoted cores read the USD tape via `status_path_candidates`. ZEC is flow-FAIL and is not started.
- `start_hydra_companion.bat` — paper-mode companion testing (no real money); same `--pairs auto` as production

**A launcher's explicit `--pairs` overrides the code default and does not
drift with it.** Both agent launchers hardcoded the legacy
`SOL/USD,SOL/BTC,BTC/USD` triangle from before v2.29 and kept running it in
production for every release after the default moved to the three cores —
trading a pair set the evidence ledger had rejected (SOL AUC 0.56 FAIL, the
SOL/BTC bridge drain-only) with **no ETH and no ZEC**. They now pass
`--pairs auto`, which seeds BTC/USD + ETH/USD + ZEC/USD and adds one
satellite per additional held asset, so held SOL is worked as an ordinary
satellite. Any change to the default pair set must be re-checked against
these two files — nothing else does it, and no test covers `.bat` content.

## Version sites (Rule 5: update ALL in one commit)

1. `CHANGELOG.md` — new `## [X.Y.Z]` section header
2. `dashboard/package.json` — `"version"` field
3. `dashboard/package-lock.json` — **both** `"version"` fields (root + `""` package)
4. `dashboard/src/App.jsx` — footer string `HYDRA vX.Y.Z`
5. `hydra_agent.py` — `_export_competition_results()` → `"version"` field
6. `hydra_backtest.py` — `HYDRA_VERSION = "X.Y.Z"` (stamps every `BacktestResult`)
7. `CLAUDE.md` — `**Version pin:** vX.Y.Z` (Project section)
8. Git tag — `git tag -s vX.Y.Z -m "vX.Y.Z"` after merge; verify `git tag -v vX.Y.Z` (Rule 3)
9. GitHub Release — `gh release create vX.Y.Z --verify-tag --notes-from-tag`; a pushed tag alone does NOT publish a Release and leaves GitHub's "Latest" badge stale

**Alignment gate:** `python scripts/check_release_alignment.py --check-tag --check-gh-release` must exit 0 at the end of every release cycle — it enumerates all 7 code/doc sites + tag + published GH Release.

**Policy:** MINOR only for material upgrades; bug fixes / doc tweaks = PATCH.

## Release PR workflow

- **Cycle:** branch → tests pass → PR → CI green → merge → signed tag
- **Tests pass:** both CI jobs green (`engine-tests` + `dashboard-build`). Mock harness (`tests/live_harness/harness.py --mode mock`) **MANDATORY** for any PR touching execution path.
- **Enumerate first:** `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` before bumping (Rule 5)
- **Tag:** signed; verify (Rule 3)
- **Automation:** `/release` skill codifies the cycle. Never merge with red or pending CI.

Tests: `python -m pytest tests/` or individual `python tests/test_*.py`
(CI pattern). Live harness detail in `tests/live_harness/` (`harness.py`
modes: smoke/mock/validate/live).

## Audit

**7-way partition** for Rule 1:

| id | scope |
|---|---|
| p1_engine_tuner | engine, tuner |
| p2_agent_streams | agent, streams |
| p3_ai_layer | brain |
| p4_backtest | backtest, backtest_metrics, backtest_server, backtest_tool, experiments |
| p5_companion | companions |
| p6_dashboard | dashboard |
| p7_tests | `tests/`, `tests/live_harness/` |

**HIGH severity:** violations of backtest I1–I12, limit-post-only, 2s
rate-limit floor, 15% circuit breaker, Wilder-EMA RSI/ATR spec, or
`HYDRA_COMPANION_LIVE_EXECUTION` default-off.

**Two-phase protocol (Rule 4):** after fixing HIGH/MED, re-run partition
sweep against your diff, then full tests + `harness.py --mode mock`;
declare done only when phase 2 is clean. Drive full cycle via `/audit`.

## Windows / WSL gotchas

- **Use Bash for all shell commands, never PowerShell** — Git Bash is available and reliable; PowerShell has encoding issues (cp1252), quoting differences, and inconsistent behavior with Python tooling on this project. Subagents and parallel workers must also use Bash. Only use PowerShell if a command explicitly requires it (e.g., Windows-specific registry access).
- Use UTF-8 explicitly; cp1252 crashes on Unicode (dashboard regime emoji + console portfolio block share the theme — both crash on cp1252)
- `time.time()` has ~15ms Windows resolution; in BaseStream heartbeat or `RESTART_COOLDOWN_S=30s` it silently miscounts — use `time.perf_counter()`
- Escape parentheses in `.bat` files inside if-blocks — cmd parser drops branches silently
- WSL: if distro is `Ubuntu-22.04` instead of `Ubuntu`, `kraken` invocation silently routes nowhere — verify `wsl -l -v`; fix with `HYDRA_WSL_DISTRO=Ubuntu-22.04`
- Vite dev server is pinned to :3000 with `strictPort: true` — it FAILS (does not fall off to another port) if :3000 is taken; free the port (`npx kill-port 3000`) rather than expecting a fallback

## Common pitfalls

- Don't add `import numpy` or `import pandas` to the engine — intentionally pure Python
- Don't change orders to market type — limit post-only is deliberate
- Don't reduce rate limiting below 2s — Kraken throttles/bans
- Don't merge engine instances across pairs — they must remain independent
- `.env` contains Kraken API keys — never commit
- On shutdown agent cancels all resting limit orders and flushes snapshot — do not bypass
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove
- **FEATURE GAP:** `CrossPairCoordinator` Rule 2 (BTC recovery BUY boost) + Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favors safer SELL); future: explicit priority or merge logic
- Companion live execution opt-in: `HYDRA_COMPANION_LIVE_EXECUTION=1`; confirm unset before live debugging
- `kraken-cli` is an external WSL Ubuntu dep (`source ~/.cargo/env && kraken`); pin is **v0.4.1** (dashboard footer + `KrakenCLI` docstring) — confirm `kraken --version` matches before debugging `--validate` schema errors
