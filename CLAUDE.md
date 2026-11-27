# CLAUDE.md — Agent Instructions for HYDRA

> **HARD REQUIREMENT.** Update this file (or the doc it points to) in the same change as: module add/remove/rename/split (`docs/REFERENCE.md`), launcher add/remove, version-site change, new env flag or kill switch, state-file ownership change (all three tables: `docs/REFERENCE.md`), safety-invariant change (path-scoped ones live in `.claude/rules/*.md`), CI gate change. If that can't land in the same commit, leave `TODO(claude-md):` in code AND a matching `<!-- TODO(claude-md): -->` here.
>
> Hot index only: rules, cross-cutting invariants, pointers. Detail lives in `.claude/rules/*.md`, module docstrings, `SKILL.md` and `CHANGELOG.md`.

## Operating Rules (binding, non-negotiable)

Each was earned by a documented past failure; breaking one is a regression bug, not a style issue.

1. **Partition big audits instead of skimming them** — for a full-repo review, run `/audit` (it owns the partitioning and its parallel agents). For a narrower review over ~20 files, split it into 3–4 partitions (parallel agents optional), each returning HIGH/MED/LOW, then synthesize; below that, review it directly.
2. **Stop processes before editing their state** — a live writer overwrites your edit on its next tick. Find the owner in `docs/REFERENCE.md` (State files); stop it, edit, verify the edit persisted, restart. Snapshot + journal must stay in sync — clean both together.
3. **Verify claims with actual commands** — "verified", "passing", "fixed" require running the check (`pytest`, `git tag -v`, …) in the same turn and pasting the output.
4. **Self-audit new code — twice where money moves** — after writing, check for unused imports, dead code, unhandled exceptions, null/empty crashes, deprecated APIs, misleading errors and false-positive checks, and fix them. Execution-path and money-handling code (`hydra_engine.py`, `hydra_agent.py`, `hydra_quant_rules.py`, `hydra_streams.py`, `hydra_kraken_cli.py`, sizing/journal/balance logic) gets a second pass before declaring done; tests, tools, research, docs and dashboard code need one.
5. **Enumerate every version site before a bump** — run `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'`, confirm each entry in §Version sites, and update all of them in one commit.

## Project

- **HYDRA** — regime-adaptive crypto trading agent for Kraken: detects trending/ranging/volatile regimes, switches among 4 strategies (Momentum, MeanReversion, Grid, Defensive), executes limit post-only.
- **Product thesis (evidence-locked — don't overclaim):** the live path is **capital preservation** (hold-through + daily trend overlay + friction + 15% BUY-only breaker), not proven growth alpha. The only after-fee selection edge is **S3 daily bounce X1 on BTC/ETH**: shadow-only (`HYDRA_S3_STRATEGY`, no order path) and not significant (BTC t=0.74, ETH t=1.59). **Heartbeat** (BTC/ETH order-flow P(up)) is display, shadow co-log and brain advisory, **never** a live BUY/SELL gate; its 2026-07 PASS had a labeler leak and is withdrawn (the leak-fixed walk-forward misses the 0.70 bar on all four pairs: `research/data/abi/heartbeat_leakfix_2026-10-02.json`). SOL/ZEC flow FAIL; ZEC S3 untradable. The only candidate with a risk rationale is the daily trend sleeve (`HYDRA_TREND_SLEEVE`, default `auto`): a pair trades it only after its pre-registered gate passes significantly on the operator's own data; otherwise it stays on the 1h engine. Plan: `docs/TRADE_PLAN.md`. Ledger: `heartbeat/HONEST_FINDINGS.md`; funnels: `heartbeat/evidence/ABI_FUNNEL_2026-07-19.md`, `research/ABI_FUNNEL_2026-10-01.md`.
- **Pairs (default):** BTC/USD, ETH/USD, ZEC/USD: three independent stable-quoted cores, no triangle or coordinator. An explicit `--pairs SOL/USD,SOL/BTC,BTC/USD` still re-activates TradingTriangle + CrossPairCoordinator. `STABLE_QUOTES = {USD, USDC, USDT}` are first-class.
- **`--pairs auto`** (`discover_portfolio_pairs`) seeds the cores in the *funded* stable quote, plus one satellite per other held asset.
- **Bridge is signal-only by default:** with an explicit SOL triangle, the SOL/BTC engines run `exit_only` drain (zero trades in 1y and a Sharpe drag on real 1h tape — `.hydra-flywheel/bridge_isolation.json`); `HYDRA_BRIDGE_TRADING=1` opts back in.
- **Candles default 60m:** the hold-through rails and friction hurdle are calibrated on 1h tape. Resume drops candle history on an interval mismatch and remaps same-base stable keys, never a global quote flip.
- **Version pin:** v2.34.6

## Defaults

- Engine is Python stdlib only — never add `numpy`/`pandas` to it.
- One HydraEngine per pair with no shared state; never merge instances across pairs.
- `min_confidence` 0.65 (both modes).
- Kraken CLI **pinned v0.4.1** (check `kraken --version` before debugging `--validate` schema errors), invoked as `wsl -d $HYDRA_WSL_DISTRO -- bash -c "source ~/.cargo/env && kraken ..."` (distro from `hydra_kraken_cli.WSL_DISTRO`, default `Ubuntu`). v0.4.1 schema notes and the deliberately unused surface live in the `KrakenCLI` docstring. Gotcha: v0.4.1 WS never prints `{"channel":"heartbeat"}` to stdout (`--monitor` health goes to stderr), so ExecutionStream/BalanceStream treat process + reader + snapshot as healthy; the 30s stdout-heartbeat timeout is public-stream only.
- Dashboard WS port 8765; Vite dev :3000 with `strictPort: true` (it fails rather than moving ports — free :3000 with `npx kill-port 3000`).
- Python 3.11+ (CI `engine-tests` runs 3.11 and 3.12). CI authority: `.github/workflows/ci.yml` (`watermark-gate`, `engine-tests`, `dashboard-build`).

## Cross-cutting invariants (HIGH severity if violated)

Execution-path bullets below give the rule. The full text, with the failure behind each, is in `.claude/rules/execution.md`, which loads with any engine, agent, stream, CLI, brain or test file.

**Execution scope**
- **SPOT-ONLY execution** — orders go only to Kraken spot pairs. Derivatives data (funding/OI via `kraken futures tickers`) is SIGNAL INPUT ONLY: no futures, options or margin orders. `hydra_derivatives_stream.py` is read-only by construction; its tests grep for authenticated subcommands.
- **Limit post-only, never market** (`--type limit --oflags post`; deliberate). **2s REST floor** between calls (Kraken throttles or bans below it). **No REST for market data** — WS streams or the `kraken` CLI only, including for new sources.
- **Research surfaces never place orders** — `hydra_s3`/`s3bounce` and `hydra_heartbeat_surface`/`heartbeat` are signal, display, shadow or brain-advisory only (test grep-guards forbid order verbs). No `HYDRA_S3_LIVE` or heartbeat-driven engine SKIP without a powered, pre-registered bakeoff (engine co-occurrence currently FORBIDs it: 0 BUYs / 365d under rails).
- **`HYDRA_COMPANION_LIVE_EXECUTION` default OFF** — companion proposals stay paper until opted in (`HYDRA_COMPANION_LIVE_EXECUTION=1`); confirm it is unset before live debugging. Opted-in companion orders register on `ExecutionStream` but stay engine-inventory-blind until a full agent place adapter exists.
- **Paper never touches live state** — paper/demo persist snapshot, journal, backfill and tuner params under `.hydra-paper/` (`state_dir_for_mode`); the companion launcher runs `--paper` from the production directory. Live `--resume` refuses a snapshot whose `paper` flag differs; the journal merge drops rows whose `intent.paper` differs (`journal_row_matches_mode`; flagless legacy rows count as live).

**Circuit breaker and exits**
- **15% drawdown sticky-halts new BUYs (engine and portfolio); SELL flatten is always allowed when `position.size > 0`** (PR-A). It arms on CURRENT drawdown, never the monotone `max_drawdown` record, and the portfolio flag arms before Phase 2.5.
- **The halt survives `--resume`** and is cleared only by the operator via `HYDRA_RESET_CIRCUIT_BREAKER=1`, which starts a new drawdown epoch. The env var acts on every start while set: unset it after the start that cleared the halt. Never auto-clear on resume.
- **Breakers measure trading losses on Hydra's own books (`_books_equity_usd`); moving capital is not a loss** (v2.34.5). Every non-fill write of engine cash goes through `_reseed_engine_cash`, so a seed or resume never erases or creates a drawdown.
- **Entry controls never gate exits** (PR-A): `tradable=False` blocks entries only, SELL ignores `min_confidence`, R2 never force-holds a spot SELL, and R1–R11 never rewrite a halt or hold-through flatten to HOLD.
- **`exit_only` drain mode** — engine flag: BUYs refused (SKIP semantics), every SELL path untouched; set per session by the agent, never persisted; the bridge default uses it; composes with hold-through and the breaker.
- **SKIP ≠ BLOCK** — a soft restriction skips an action for the tick; BLOCK is reserved for hard rules (the 15% breaker).

**Guardrails and the LLM**
- **Deterministic guardrails never depend on the LLM layer**: `HydraAgent._apply_quant_guardrails` runs R1–R11 + QFE when `self.brain` is None. Making them brain-conditional again is a HIGH regression. A brain call that outlasts the tick wait is abandoned, and its late return writes nothing.
- **The LLM may veto or exit, never open**: fresh and cached verdicts both go through `merge_llm_verdict(engine_action, final_signal)`, and nothing comes from an engine HOLD. The breaker mandate blocks BUYs, never exits.
- **Nothing reachable from the brain's tool loop runs inline**: it runs on the LIVE TICK THREAD, so a long-running handler routes through `BacktestWorkerPool` (I1).
- **R11/QFE is exit-only, profit-only, squeeze-filtered**: it lets an engine SELL through force_hold only at ≥`QFE_MIN_PROFIT_PCT` profit with no deterministic squeeze catalyst. It never opens and never fires underwater.
- **Quant/cross-pair (PR-E):** always `tick(generate_only=True)`, then execute post-coordination. Rules re-apply after a brain OVERRIDE; `HYDRA_QUANT_INDICATORS_DISABLED=1` skips `apply_rules`/QFE.
- **Derivatives coverage and funding math** — pairs declare derivatives coverage to R10 structurally (from `SPOT_TO_DERIVATIVES`, never from data presence), or R10 force-holds them every tick; a new pair without a direct perp needs the matching flag. Funding bps are markPrice-relative and hourly-scaled, never `fundingRate * 10000`. Full invariants: `.claude/rules/derivatives.md`.

**Sizing and balances**
- **Gross vs free balance are different numbers**: equity, peak and the breakers read GROSS; every spendability decision reads FREE through the `_get_real_quote_balance` chokepoint. Sizing on gross re-commits held money.
- **Per-quote balance pools**: a live engine sizes only from the real holding of its own quote. A zero pool means no entries, but `tradable` stays True so inventory can exit. On resume, restored books keep their own cash (`_seed_cash`).
- **De-risking multipliers survive every later sizing step**: any floor or override after `size = size * effective_mult` in `_maybe_execute` must scale by `effective_mult` too. `max_position_pct` applies after `size_multiplier` (PR-B).
- **Kelly / friction honesty (PR-D)** — PositionSizer uses excess-over-threshold Kelly (conf=min → edge 0.10, conf=1 → 1.0), not `(conf*2-1)`. The friction hurdle is timeframe-aware (≥2.0% on 1h+ bars). Go-live plumbing gates: `python scripts/go_live_gates.py`.

**Orders, fills and the journal**
- **One resting order per pair**: a `PLACED` row blocks another `execute_signal` on that pair. An opposite signal cancels the resting order and places nothing that tick. Snapshot whenever the book changes, even if the journal didn't grow.
- **An ambiguous placement is never `PLACEMENT_FAILED`**: when Kraken may already hold the order, the row stays `PLACED` with `lifecycle.unconfirmed` until the stream adopts its txid (rolling back let the next tick place a duplicate live order).
- **A working order the market left behind is re-priced** (`_reprice_stale_resting`), still limit post-only. It is the only case where a resting SELL is cancelled without an opposite signal.
- **Fill true-up (PR-C)**: every terminal FILLED/PARTIAL restores `pre_trade_snapshot` and replays at exchange `avg_fill_price`, live and for a previous session's fills.
- **`PLACEMENT_FAILED` entries are session-only**: they MUST NOT persist. `_journal_for_persistence()` is the single chokepoint for both write paths; route any new one through it.
- **The journal cap never drops a working order** (`_trim_journal` never removes a `PLACED` row).
- **On shutdown the agent cancels all resting limit orders and flushes the snapshot** — do not bypass it.

**Ticks, streams and signals**
- **Every pair with a state lands in `all_states`**: it is Phase 2.5's sole input, so the assignment sits OUTSIDE the brain / brain-free / cached-replay branches. Nesting it drops the other arms' exits while everything looks healthy.
- **Stream recovery runs in `finally`**, never the try body. A skipped pair's `engine_states[pair]` is `None`: use `.get(pair) or {}`.
- **Finished bars reach the engine in final form** (`latest_closed_candle` before the forming bar). With no pushed candle a pair ticks from `kraken ohlc`, so exits keep flowing during an outage.
- **Trend overlay is evidence-gated and fails open**: every `daily_trend_long()` consumer treats `None` as pre-overlay behavior. Entering on the daily ensemble alone was tested and REJECTED; don't re-add it without a passing gate.
- **The trend sleeve is daily-bandwidth only and off unless evidence enables it** — full invariant: `.claude/rules/trend-sleeve.md`. Cross-cutting parts: sleeve pairs skip Phase 2 entirely (no LLM, no R1–R11/QFE — the brain-free-guardrail invariant governs 1h-engine pairs only); `TREND_SLEEVE:exit`/`:trim` are protected; **never commit a gate report** built on data other than the operator's store — it would switch production.
- **No blanket bar-count hold**: each indicator uses the history it has.
- **RSI/ATR = Wilder exponential smoothing, NOT SMA**; Bollinger uses population variance.
- **`hydra_rm_features.py` is pure**: no I/O; every function returns `Optional` from its inputs alone, `None` on insufficient data.
- **Backtest/evidence invariants (path-scoped)** — timestamp-aligned backtest candles, null-calibrated evidence tooling and trade-level Sharpe annualization live in `.claude/rules/backtest-evidence.md`, loaded when backtest, metrics, walk-forward, flywheel, S3, `tools/`, `research/`, `heartbeat/` or `s3bounce/` files are touched. Read it before changing any evidence gate.

**Pairs, config and auth**
- **Pair identity has one source of truth**: `hydra_pair_registry.PairRegistry`. Never re-implement an alias dict. CLI schema drift skips a pair loudly, never falls back to a literal.
- **Roles, not literal pair names, in coordinator/agent logic** (`stable_sol`, `stable_btc`, `bridge`). Every `STABLE_QUOTES` member is $1; switching the default quote is a config flip.
- **The dashboard WS authenticates BOTH directions** — an unauthenticated socket receives nothing (no `latest_state`, no broadcast) and closes after `HYDRA_WS_AUTH_GRACE_S`; auth, not loopback binding, protects the account, and nothing is restored on connect behind `_origin_allowed`. Full invariant: `.claude/rules/dashboard-ws.md`.

## Pointers and tooling

- **Reference tables:** `docs/REFERENCE.md`: modules (file → role), env flags, state files (owner per file — Rule 2). `hydra_users.db`, `hydra_auth_state.json`, `hydra_ws_token.json` and `.env` (Kraken API keys) hold **secrets** — never commit them.
- **Env flags:** add every new flag to `docs/REFERENCE.md`. Money/security-critical defaults: `HYDRA_COMPANION_LIVE_EXECUTION` **OFF**, `HYDRA_TREND_SLEEVE` **`auto`** (per-pair evidence gate; `1`/`0` override), `HYDRA_S3_STRATEGY` **OFF** (no order path), `HYDRA_CLI_LEGACY_SECRET_EXPORT` off, `HYDRA_WS_HOST` `127.0.0.1` (`0.0.0.0` only behind an auth-terminating proxy). Set `HYDRA_RESET_CIRCUIT_BREAKER=1` only after reviewing the drawdown, and unset it after the start that cleared the halt. Secret env vars (`HYDRA_JWT_SECRET`, `HYDRA_ENCRYPTION_KEY`, admin/new-user passwords) are never committed.
- **Deep specs:** `SKILL.md` (trading formulas + risk rules) · `CHANGELOG.md` · `SECURITY.md` · `docs/BACKTEST.md` (runbook + design archive; defaults live in code) · `docs/COMPANION_SPEC.md` · `docs/TRADE_PLAN.md` (the plan each pair trades, sleeve and hold-through rails, the evidence behind each rule, the per-session cycle) · `heartbeat/HONEST_FINDINGS.md` (S3 + heartbeat verdict ledger) · `research/RETAIL_CRYPTO_EDGE_2026.md` · `research/S3_BOUNCE_EDGE_2026.md` + `research/data/`. Root `AUDIT_*.md` files are gitignored local snapshots, not product truth.
- **Subsystem detail** (indicators, regime, Kelly sizing, price precision, execution-stream lifecycle, resume reconciliation, forex modifier, shutdown): the `hydra_engine.py` / `hydra_agent.py` docstrings and `SKILL.md`.
- **Post-edit hook:** `.claude/hooks/post-edit.py` runs path-scoped checks, advisory and **async**, so re-run the test yourself before calling an edit verified (Rule 3). Silence it with `HYDRA_POSTEDIT_HOOK_DISABLED=1`.
- **gitattributes pin:** `*.sh text eol=lf` keeps Windows core.autocrlf from CRLF-ing hook shebangs; `*.bat`/`*.cmd text eol=crlf`, because cmd.exe can miss `goto` labels in an LF-only batch file.

## Launchers

- `start_hydra.bat` — production watchdog: `--pairs auto --mode competition --resume` (**do not remove these flags**); starts `start_heartbeat.bat` once before the restart loop only when `HYDRA_START_HEARTBEAT=1` (default off; idempotent if heartbeat.exe is already up).
- `start_all.bat` — dashboard + agent watchdog (heartbeat only via `HYDRA_START_HEARTBEAT=1`). `start_dashboard.bat` — dashboard only.
- `run_strategy_gate.bat` — refreshes `hydra_history.sqlite`, runs `tools/trend_sleeve_gate.py --engine`, prints the per-pair strategy `auto` will pick. No orders.
- `start_heartbeat.bat` — `heartbeat run` for BTC/USD + ETH/USD (research P(up) status files, **no order path**; USDC-quoted cores read the USD tape via `status_path_candidates`; ZEC is flow-FAIL and not started).
- `start_hydra_companion.bat` — paper-mode companion testing (no real money), same `--pairs auto` as production.

**A launcher's explicit `--pairs` overrides the code default and does not drift with it.** When the default pair set changes, re-check `start_hydra.bat` and `start_hydra_companion.bat` by hand — nothing else does, and no test covers `.bat` content (both once kept trading the rejected SOL triangle, with no ETH or ZEC, for several releases).

## Version sites (Rule 5: update ALL in one commit)

1. `CHANGELOG.md` — new `## [X.Y.Z]` section header
2. `dashboard/package.json` — `"version"` field
3. `dashboard/package-lock.json` — **both** `"version"` fields (root + `""` package)
4. `dashboard/src/App.jsx` — footer string `HYDRA vX.Y.Z`
5. `hydra_agent.py` — `_export_competition_results()` → `"version"` field
6. `hydra_backtest.py` — `HYDRA_VERSION = "X.Y.Z"` (stamps every `BacktestResult`)
7. `CLAUDE.md` — `**Version pin:** vX.Y.Z` (Project section)
8. Git tag — `git tag -s vX.Y.Z -m "vX.Y.Z"` after merge; verify with `git tag -v vX.Y.Z` (Rule 3)
9. GitHub Release — `gh release create vX.Y.Z --verify-tag --notes-from-tag`; a pushed tag alone does NOT publish a Release and leaves GitHub's "Latest" badge stale

**Alignment gate:** `python scripts/check_release_alignment.py --check-tag --check-gh-release` must exit 0 at the end of every release cycle (all 7 code/doc sites + tag + published Release). **Policy:** MINOR only for material upgrades; bug fixes and doc tweaks are PATCH.

## Release, CI and audit

- **Cycle:** branch → tests pass → PR → CI green → merge → signed tag; the `/release` skill codifies it. **Never merge with red or pending CI.**
- **CI gates:** `watermark-gate` + `engine-tests` + `dashboard-build`. `watermark-gate` is a merge blocker: agent trailers (e.g. AI `Co-Authored-By:` lines), "Generated with" banners, or actionable invisible Unicode in files changed vs `main` (not Layer B).
- **Mock harness** (`tests/live_harness/harness.py --mode mock`) is **MANDATORY** for any PR touching the execution path (modes: smoke/mock/validate/live).
- **Tests:** `python -m pytest tests/` or individual `python tests/test_*.py` (the CI pattern).
- **Audit:** `/audit` drives the full cycle; its SKILL.md owns the 7-way partition (Rule 1) and triage. HIGH severity = violations of backtest I1–I12 (`docs/BACKTEST.md` §3), limit post-only, the 2s REST floor, the 15% breaker, Wilder RSI/ATR, or `HYDRA_COMPANION_LIVE_EXECUTION` default-off. Two-phase audit: after fixing HIGH/MED, re-run the partition sweep against your diff, then full tests + `harness.py --mode mock`; done only when phase 2 is clean.

## Windows / WSL gotchas

- **Use Bash (Git Bash) for all shell commands, including in subagents and parallel workers** — PowerShell's cp1252 encoding, quoting differences and inconsistent Python-tooling behaviour break things here. Use PowerShell only when a command explicitly needs it (e.g. registry access).
- Use UTF-8 explicitly — cp1252 crashes on Unicode (the dashboard regime emoji and the console portfolio block both do).
- Time intervals with `time.monotonic()` (what `BaseStream` uses for heartbeats and `RESTART_COOLDOWN_S`) or `time.perf_counter()` — never `time.time()`, whose clock jumps and ~15ms Windows resolution miscount them.
- Escape parentheses inside `.bat` if-blocks — the cmd parser silently drops branches otherwise.
- If the WSL distro is `Ubuntu-22.04` rather than `Ubuntu`, `kraken` calls silently route nowhere — check `wsl -l -v`; fix with `HYDRA_WSL_DISTRO=Ubuntu-22.04`.

## Known gap

- `CrossPairCoordinator` Rule 2 (BTC recovery BUY boost) and Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favours the safer SELL). Future: explicit priority or merge logic.
