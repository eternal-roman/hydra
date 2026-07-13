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
- **Pairs (default v2.19+):** SOL/USD, SOL/BTC, BTC/USD. The active
  triangle's stable quote is selected by the agent's `--pairs` flag;
  `STABLE_QUOTES = {USD, USDC, USDT}` are first-class. v2.19 flipped
  the default from USDC → USD; opt back into USDC by passing
  `--pairs SOL/USDC,SOL/BTC,BTC/USDC`. **`--pairs auto`** discovers every
  held Kraken asset and adds one satellite pair each (USDC-quoted when
  USDC is funded, else USD; `HYDRA_AUTO_QUOTE` forces) —
  `hydra_agent.discover_portfolio_pairs`.
- **Bridge is signal-only by default (v2.28):** SOL/BTC engines run
  `exit_only` drain mode (SELLs flow until flat, BUYs refused) —
  isolation study on real 1h tape showed zero 1y trades and a Sharpe
  drag when included (`.hydra-flywheel/bridge_isolation.json`).
  `HYDRA_BRIDGE_TRADING=1` opts back in.
- **Candles default 60m (v2.28):** the hold-through rails and friction
  hurdle were calibrated on 1h tape; 15m ran them off-calibration.
  `--candle-interval` still accepts 1/5/15/30/60; snapshot resume drops
  candle history on interval mismatch (positions/journal restore).
- **Version pin:** v2.27.6

## Defaults (inherited)

- Engine: Python stdlib only (no numpy/pandas in engine)
- Orders: limit post-only (`--type limit --oflags post`). Never market.
- Engine isolation: one HydraEngine per pair, no shared state
- Kraken CLI: `wsl -d $HYDRA_WSL_DISTRO -- bash -c "source ~/.cargo/env && kraken ..."`
  (distro from `hydra_kraken_cli.WSL_DISTRO`, default `Ubuntu`; verify via `wsl -l -v`)
- Kraken REST min interval: **2s** between calls
- min_confidence: 0.65 (both modes); warmup_candles: 50
- Circuit breaker: **15% drawdown halts engine for session** (permanent)
- WS dashboard port: 8765; Vite dev: 3000 (`strictPort: true`)
- CI authority: `.github/workflows/ci.yml` (jobs: `engine-tests`,
  `dashboard-build`)

## Cross-cutting invariants (HIGH severity if violated)

- **SPOT-ONLY execution** — Hydra places orders ONLY on Kraken spot pairs (the active triangle: stable-quoted SOL, stable-quoted BTC, and SOL/BTC; default v2.19+ is SOL/USD, SOL/BTC, BTC/USD). Derivatives data (Kraken Futures funding/OI via `kraken futures tickers` CLI) is SIGNAL INPUT ONLY. No futures, no options, no margin orders placed. `hydra_derivatives_stream.py` is read-only by construction; its test suite greps for authenticated subcommand names and fails if any appear.
- **Limit post-only, never market** — deliberate design choice
- **No REST for market data** — all Kraken market data flows through the WebSocket streams or the `kraken` CLI (WSL Ubuntu). New data sources must use CLI or WS.
- **2s REST floor** — Kraken throttles or bans below this
- **15% drawdown kills engine for session** — both `tick()` and `_maybe_execute` check
- **RSI/ATR = Wilder exponential smoothing, NOT SMA** (Bollinger = population variance)
- **SKIP ≠ BLOCK** — a soft restriction skips an action for the tick; BLOCK is reserved for hard rules (the 15% drawdown breaker)
- **`HYDRA_COMPANION_LIVE_EXECUTION` default OFF** — proposals are paper until opted in
- **Funding is markPrice-relative, never absolute** — Kraken Futures `PF_*` `fundingRate` is absolute USD-per-contract-per-period. Convert to bps via `(fundingRate / markPrice) * 10000`, never `fundingRate * 10000`. The `_absolute_to_relative_bps` helper in `hydra_derivatives_stream.py` enforces this (±500 bps clamp vs API drift). Pre-v2.15.2 fires used the wrong absolute conversion — not authoritative.
- **Synthetic pairs declare themselves to R10** — `DerivativesSnapshot.synthetic=True` propagates to `quant_indicators["synthetic_pair"]`; R10 then tracks only funding/cvd/regime (the fields the synthetic path actually populates). Adding a new pair without a direct Kraken Futures perp requires this flag, otherwise R10 will structurally force-hold every tick.
- **Uncovered pairs declare themselves to R10** — pairs with no `SPOT_TO_DERIVATIVES` entry at all (portfolio satellites, e.g. NIGHT/USD) get `quant_indicators["derivatives_covered"]=False` from `_build_quant_indicators`; R10 then tracks only CVD. Coverage is structural (pair in the futures map), never "snapshot present" — a covered pair with a warming/stale stream must still hit the R10 blackout.
- **Per-quote balance pools (v2.28)** — live stable-quoted engines are funded from the REAL holding of their own quote currency split across pairs sharing that quote (`_set_engine_balances`); a USDC engine never sizes against USD it cannot spend. Zero pool ⇒ balance 0 (sizer refuses entries) but `tradable` stays True so inventory can exit. Paper keeps the uniform split.
- **`exit_only` drain mode** — engine-level flag: BUY entries refused (SKIP semantics), every SELL path untouched. Set per-session by the agent (never persisted); the bridge default uses it. Composes with hold-through and the CB.
- **`hydra_rm_features.py` is pure** — no I/O, subprocess, network, or file access; every function returns `Optional[float]` (or `Optional[dict]`) from input alone, returning `None` on insufficient data. A future contributor adding side effects breaks the "fails-silent with None" contract that lets R10 and RM reason over missing vs corrupted data and that lets `HYDRA_RM_FEATURES_DISABLED` work as an instant rollback.
- **`PLACEMENT_FAILED` entries are session-only** — pre-exchange diagnostics (`insufficient_USD_balance`, `placement_error:api`) live in the in-memory `HydraAgent.order_journal` for live debugging but MUST NOT persist to `hydra_session_snapshot.json` or the rolling `hydra_order_journal.json`. The `_journal_for_persistence()` helper is the single chokepoint; both write paths (`_save_snapshot` and the per-tick rolling write) go through it. If you add a third write path, route it through the helper too.
- **Pair identity has one source of truth** — `hydra_pair_registry.PairRegistry` owns alias resolution (XBT↔BTC, ZUSD↔USD, USDC.F→USDC, slashed↔slashless, case-insensitive) and per-pair metadata (price decimals, ordermin, costmin, tick size). `hydra_kraken_cli.KrakenCLI` delegates to the class-level `registry`. New pair-handling code must consume the registry — never re-implement an alias dict. v2.19 absorbed 1048 USDC literals into a single registry + role binding.
- **Roles, not literal pair names, in coordinator/agent logic** — CrossPairCoordinator and HydraAgent address pairs by their `TradingTriangle` role (`stable_sol`, `stable_btc`, `bridge`), not by hardcoded `"SOL/USDC"` etc. `STABLE_QUOTES = {USD, USDC, USDT}`; the engine treats every member as $1. Switching the default quote is a config flip, not a refactor — see `hydra_config.HydraConfig.from_quote`.
- **R11/QFE is exit-only, profit-only, squeeze-filtered** — `evaluate_qfe()` in `hydra_quant_rules.py` lets a SELL through force_hold ONLY when: position is in profit (≥`QFE_MIN_PROFIT_PCT` = 1.0% mark, fee-cushioned), the engine already generated SELL, and no **deterministic** squeeze catalyst is present (`short_squeeze` OI regime, or extreme-short-funding + accumulation CVD). LLM `positioning_bias=crowded_short` alone does **not** veto QFE. QFE must never open a position, must never fire on an underwater position, and force_hold remains active for entries after QFE exits. Every QFE event logs a full trigger snapshot via `qfe_trigger_values` in `state["ai_decision"]`.
- **Exit guarantees (PR-A)** — Circuit breaker blocks BUY only; SELL always allowed when `position.size > 0` (halt flatten). SELL ignores `min_confidence` (entries still require it). R2 force_holds extreme-negative-funding **BUY** (bounce-chase), never spot SELL (long close).
- **Hard risk caps (PR-B)** — `max_position_pct` applies **after** brain `size_multiplier` and caps gross inventory (notional/equity). Peak equity never rebases downward on balance seed/resume. Portfolio max DD ≥ 15% sticky-blocks new BUYs (SELL still allowed).
- **Fill true-up (PR-C)** — Every terminal FILLED/PARTIAL restores `pre_trade_snapshot` and replays at exchange `avg_fill_price` (not candle close). Snapshot persisted on journal PLACED. Unsellable dust below ordermin is written off. BUY limit offsets capped (≤20 bps SOL/STABLE) for post-only fill rate.
- **Kelly / friction honesty (PR-D)** — PositionSizer uses excess-over-threshold Kelly (conf=min → edge 0.10, conf=1 → 1.0), not `(conf*2-1)`. Friction hurdle is timeframe-aware (≥2.0% on 1h+ bars). Go-live plumbing gates: `python scripts/go_live_gates.py`.
- **Quant/cross-pair (PR-E)** — `HYDRA_QUANT_INDICATORS_DISABLED=1` skips `apply_rules`/QFE (no R10 blackout). Rules re-applied after brain OVERRIDE. Rule 2 recovery preferred over Rule 3 swap; Rule 3 requires bridge `tradable` (emitted on engine state from `_build_state`). Always `tick(generate_only=True)` then post-coord execute. USDT pairs mapped in `SPOT_TO_DERIVATIVES`. Companion live (opt-in) registers orders on `ExecutionStream` but remains engine-inventory-blind until a full agent place adapter exists.
- **Unified warmup (PR-F)** — `SignalGenerator.WARMUP_CANDLES = 50` (aligned with regime detector).

Subsystem detail (indicators, regime, Kelly sizing, price precision,
execution stream lifecycle, resume reconciliation, forex modifier,
shutdown) lives in the `hydra_engine.py` / `hydra_agent.py` docstrings and `SKILL.md`.

## Modules (thin index — details in deep specs)

| id | file | role |
|---|---|---|
| engine | `hydra_engine.py` | indicators, regime, signals, sizing, hold-through rails |
| agent | `hydra_agent.py` | live agent: Kraken CLI via WSL, WS broadcast, execution, reconciler, snapshot + `--resume` |
| brain | `hydra_brain.py` | 3-agent AI: Claude Market Quant + Risk Manager + Grok Strategist |
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
| journal_maintenance | `journal_maintenance.py` | order journal compaction/rotation |
| journal_migrator | `hydra_journal_migrator.py` | one-shot legacy journal migration (auto on first start) |
| dashboard | `dashboard/src/App.jsx` | single-file React, inline styles; tabs LIVE/RESEARCH/SETTINGS |
| pair_registry | `hydra_pair_registry.py` | single source of truth for pair metadata; `Pair` value object + `PairRegistry` (alias resolution, kraken-pairs bootstrap); `STABLE_QUOTES`, `normalize_asset` |
| config | `hydra_config.py` | `TradingTriangle` role-binding + `HydraConfig` boot-time facade; `add_config_args()` registers `--quote` (env `HYDRA_QUOTE`); `DEFAULT_QUOTE = "USD"` |
| state_migrator | `hydra_state_migrator.py` | one-shot quote-currency migration of `hydra_session_snapshot.json` (engines, regime history, derivatives); preserves `order_journal` audit trail |
| flywheel | `hydra_flywheel.py` | paper capital allocator (CLI-only, NO live order path, not wired into agent capital): signal-driven daily trend ensemble + carry monitor + cash; **only** the legacy engine sleeve is evidence-gated (0% until `validation_results.json` clears). Research tools: `tools/flywheel_validation.py`, `tools/carry_backtest.py`, `tools/trend_backtest.py` (trend/carry JSONs are research-only) |

## Deep specs

- `SKILL.md` — full trading specification (agent-readable)
- `CHANGELOG.md` — version history
- `SECURITY.md` — security policy
- `docs/BACKTEST.md` / `docs/BACKTEST_SPEC.md` — runbook + authoritative design
- `docs/COMPANION_SPEC.md` — companion spec (authoritative)
- Latest post-release audit report lives in `AUDIT_YYYY-MM-DD.md` at root (keep only the most recent)

## Claude Code tooling

- **Skills:** `/release` (release SOP), `/audit` (zero-skip review), `/review`, `/security-review`
- **Post-edit hook:** `.claude/hooks/post-edit.py` — path-scoped verification; advisory; silence with `HYDRA_POSTEDIT_HOOK_DISABLED=1` (wired in `.claude/settings.json`)
- **Settings split:** per-user `.claude/settings.local.json` + runtime `.claude/scheduled_tasks.lock` gitignored; everything else under `.claude/` committed
- **gitattributes pin:** `*.sh text eol=lf` — prevents Windows core.autocrlf CRLF-ing hook shebang

## State files

| id | path | ownership / notes |
|---|---|---|
| snapshot | `hydra_session_snapshot.json` | atomic `.tmp → os.replace`; `--resume` target; embeds v2.18.0 `derivatives_history` (OI + mark-price deques, rehydrated with 30 min staleness gate) |
| order_journal | `hydra_order_journal.json` | snapshots immediately on any tick that appends (crash cannot lose since last successful tick); gitignored |
| params | `hydra_params_<pair>.json` | per-pair learned tuning params; gitignored |
| errors_log | `hydra_errors.log` | tick try/except writes here with full traceback; loop continues |
| companion_memory | `.hydra-companions/memory/{user}_{companion}.jsonl` | per-companion distilled facts; local JSONL, authoritative, 4KB LRU budget; gitignored |
| experiments_store | `.hydra-experiments/` | owner `experiments`; `presets.json` bootstraps from code on first init (delete to regenerate) |
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
| `HYDRA_QUOTE` | config | Default stable quote when `--quote` is not passed and no `--pairs` override. Choices: `USD` (v2.19+ default), `USDC`, `USDT`. Resolution order: explicit `--quote` > `HYDRA_QUOTE` env > `DEFAULT_QUOTE` (USD). |
| `HYDRA_BRIDGE_TRADING` | agent | `=1` re-enables SOL/BTC bridge trading. Default OFF (v2.28): the bridge runs exit_only drain mode — evidence in `.hydra-flywheel/bridge_isolation.json` (0 trades/1y; Sharpe drag 2y). Candles/synthetic funding still stream as signal input. |
| `HYDRA_AUTO_QUOTE` | agent | Forces the satellite quote for `--pairs auto` (`USD`/`USDC`/`USDT`). Default unset: prefer USDC when the account holds USDC above costmin (yield), else the triangle quote. USD remains essential for USD-only listings (e.g. NIGHT/USD) and fill-rate-sensitive flow. |
| `HYDRA_TAPE_CAPTURE` | history | `=1` (default) wires CandleStream candle-close pushes into a bounded-queue writer that upserts to `hydra_history.sqlite` (`source='tape'`). Set `=0` to disable (e.g. paper-mode tests on a shared DB). |
| `HYDRA_HISTORY_DB` | history | Path override for the canonical OHLC store. Defaults to `hydra_history.sqlite` in the working directory. Used by the agent (tape capture), `tools/refresh_history.py`, and the SqliteSource backtest path. |
| `HYDRA_WSL_DISTRO` | cli | WSL distribution name for all `kraken` CLI invocations. Defaults to `Ubuntu`. Override if your distro is named differently (e.g. `Ubuntu-24.04`). Single source of truth: `hydra_kraken_cli.WSL_DISTRO`; isolated modules read the env var directly. |
| `HYDRA_FRICTION_GATE_DISABLED` | engine | `=1` disables the friction expectancy gate (v2.27): BUY entries whose strategy-implied expected move (BB-mid reversion distance or 2×ATR%) is under `FRICTION_HURDLE_MULT × ROUND_TRIP_FRICTION_PCT` (0.84%) are skipped (SKIP semantics). Entries only — exits never gated; fails open on insufficient history. Active on BOTH `tick()` and `execute_signal()` paths. |
| `HYDRA_HOLD_THROUGH` | engine | **Default ON** (all pairs). TREND_UP BUY ≥0.65, flatten `TREND_DOWN`, ride mid-UP except extreme overbought. `=0` = raw engine (research/tests). Does not disable friction or 15% CB. Spec: `docs/HOLD_THROUGH.md`. Replaces removed `HYDRA_REGIME_SELECTIVE`. |
| `HYDRA_FEE_DEDUCTION_DISABLED` | agent | `=1` reverts fee-true accounting (v2.27): confirmed fills debit `lifecycle.fee_quote` from the engine's quote balance exactly once (idempotent via `lifecycle.fee_applied`). Default off (fees deducted) — pre-v2.27 live P&L was overstated ~16 bps/fill vs the backtest, which always deducted fees. |

## Build / run

- Dashboard dev: `cd dashboard && npm install && npm run dev`
- Agent default: `python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD --balance 100`
- Agent USDC opt-in: `python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC` (registry handles both transparently; engine/coordinator/agent quote-agnostic)
- Agent competition: `python hydra_agent.py --mode competition`
- Agent paper: `python hydra_agent.py --mode competition --paper`
- Agent resume: `python hydra_agent.py --mode competition --resume`
- Engine demo (no keys): `python hydra_engine.py`

**Launchers:**
- `start_hydra.bat` — production watchdog (`--mode competition --resume` — **do not remove these flags**)
- `start_all.bat` — full stack: agent + dashboard
- `start_dashboard.bat` — dashboard only
- `start_hydra_companion.bat` — paper-mode companion testing (no real money)

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
- `kraken-cli` is an external WSL Ubuntu dep (`source ~/.cargo/env && kraken`); check dashboard footer pinned version before debugging `--validate` schema errors
