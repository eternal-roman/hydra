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
   agents on the `/audit` partitions (default 7-way). Each returns HIGH/MED/LOW;
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
  BTC/ETH**, still **shadow-only** (`HYDRA_S3_STRATEGY`, no order path)
  and not statistically established (BTC t=0.74, ETH t=1.59).
  **Heartbeat** is a BTC/ETH order-flow confirmer for display + shadow
  co-log (dashboard P(up); brain advisory); **never** a live BUY/SELL
  gate until a powered bakeoff clears, and its committed AUCs predate
  the 2026-10 labeler leak fix (re-run before use). SOL/ZEC flow FAIL;
  ZEC S3 untradable. The one candidate with a risk rationale is the
  daily trend sleeve (`HYDRA_TREND_SLEEVE`, default `auto`): a pair trades
  it only after its pre-registered gate passes, significantly, on the
  operator's own data (`hydra_strategy_gate`, `run_strategy_gate.bat`);
  otherwise the pair stays on the 1h engine. Plan and session routine:
  `docs/TRADE_PLAN.md`. Ledger: `heartbeat/HONEST_FINDINGS.md`
  (2026-10 corrections) · funnels:
  `heartbeat/evidence/ABI_FUNNEL_2026-07-19.md`,
  `research/ABI_FUNNEL_2026-10-01.md`.
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
- **Version pin:** v2.34.4

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
  accepts both. v0.4.1 `ticker` emits named fields per pair (`last_price`,
  `bid_price`, `ask_price`, ...), not `c`/`b`/`a` arrays — `ticker()` flattens
  both. v0.4.1 WS **does not print** `{"channel":"heartbeat"}` on
  stdout (swallowed at the JSON sink; `--monitor` health is stderr).
  ExecutionStream / BalanceStream therefore treat process+reader+snapshot
  as healthy; a 30s stdout-heartbeat timeout is public-stream only.
- Kraken REST min interval: **2s** between calls (Kraken throttles/bans below this)
- min_confidence: 0.65 (both modes); warmup_candles: 50
- Circuit breaker: **15% drawdown sticky-halts new BUYs for session; SELL flatten still allowed (PR-A)**
- WS dashboard port: 8765; Vite dev: 3000 (`strictPort: true`)
- Python **3.11+**. CI `engine-tests` runs 3.11 and 3.12.
- CI authority: `.github/workflows/ci.yml` (jobs: `watermark-gate`,
  `engine-tests`, `dashboard-build`)

## Cross-cutting invariants (HIGH severity if violated)

- **SPOT-ONLY execution** — Hydra places orders ONLY on Kraken spot pairs (default v2.29+: BTC/USD, ETH/USD, ZEC/USD; a SOL triangle when explicitly configured). Derivatives data (Kraken Futures funding/OI via `kraken futures tickers` CLI) is SIGNAL INPUT ONLY. No futures, no options, no margin orders placed. `hydra_derivatives_stream.py` is read-only by construction; its test suite greps for authenticated subcommand names and fails if any appear.
- **Limit post-only, never market** — deliberate design choice
- **No REST for market data** — all Kraken market data flows through the WebSocket streams or the `kraken` CLI (WSL Ubuntu). New data sources must use CLI or WS.
- **2s REST floor** — Kraken throttles or bans below this
- **15% drawdown sticky-halts new BUYs for session** — SELL flatten still allowed when `position.size > 0` (PR-A); both `tick()` and `_maybe_execute` check
- **The circuit-breaker halt is cleared only by an explicit operator act, and it arms on the CURRENT drawdown (v2.32)** — `halted` restores from the snapshot, so "for session" above really meant *forever* under `--resume` (which is what production runs). It now prints a loud RESUMED-STILL-HALTED banner at boot and is cleared only by `HYDRA_RESET_CIRCUIT_BREAKER=1`. The reset clears the FLAG only — `peak_equity` / `max_drawdown` are preserved as the record. `tick()` therefore arms off `drawdown` (equity vs peak, this tick), **never** `self.max_drawdown`: that field is a monotone high-water mark nothing lowers, so arming on it re-halted the engine on the very next tick after a reset — even from a fully recovered account — making the documented escape hatch a no-op. Still-underwater re-arms immediately, which is the intent. Never auto-clear on resume: that silently re-enables risk after a breach.
  Both halves behave identically: `hydra_engine.tick` arms on the tick's `drawdown` and `hydra_agent._build_dashboard_state` arms on `cur_dd`. Neither reads its monotone running max, which stays purely as the record.
- **Deterministic guardrails must not depend on the LLM layer (v2.32)** — `apply_rules` / `evaluate_qfe` used to be reachable only from inside `_apply_brain`, so with no LLM key configured the entire R1-R11 + QFE stack silently did not run while the derivatives feed kept streaming to the dashboard. `HydraAgent._apply_quant_guardrails` is the brain-free path (positioning_bias `""`, so R8 cannot fire); the tick loop calls it for any actionable signal when `self.brain` is None, and boot prints which guardrail layer is live. Anything that makes rules conditional on the brain again is a HIGH regression. A brain call that exceeds the tick wait is abandoned: the tick trades the pre-brain snapshot plus these rules, and the late return must not write the replay cache or mark the candle evaluated.
- **Every pair with a state must land in `all_states`** — that dict is the sole input to Phase 2.5, so the `all_states[pair] = state` assignment belongs OUTSIDE the brain / brain-free / cached-replay branches. Nesting it inside one arm silently drops the other arms' pairs from execution entirely — no entries and **no exits** — while ticks, logs and the dashboard all look healthy.
- **Backtest candles align on TIMESTAMP, never on index (v2.32.1)** — `BacktestRunner._loop` advances only the pairs whose next bar sits at the earliest timestamp across pairs; a pair with no bar there is absent for that tick. Index-zipping silently accumulated cross-pair skew on any gapped series (which the real sqlite store has — `HistoryStore.coverage` tracks `gap_count`), corrupting coordinator decisions and the summed equity curves that `tools/flywheel_validation.py` writes as the evidence gate. Never reintroduce a bare `next(it)` per pair per tick. The replay surface is engine rails, the portfolio buy halt, the UTC session weight, and the coordinator (including Rule 4 prices and swap legs). It does not invent derivatives indicators, an order book, or a brain. `BacktestResult.decision_surface` says so.
- **Evidence tooling is calibrated against a null (audit 2026-10)** — the Monte Carlo trade bootstrap is circular with a block of at most round(n^(1/3)) trades; the old non-circular L=20 draw put the 95% CI above zero for 26% of zero-edge series at n=25 (nominal 2.5%), so `mc_ci_lower_positive` passed coin flips ~10x too often (`tools/mc_bootstrap_calibration.py`, `research/data/abi/mc_bootstrap_calibration.json`). `walk_forward` slices every pair on one union-timestamp clock and seeds each slice's daily overlay from the slice start. Synthetic bars start at `SYNTHETIC_EPOCH_S`, never `time.time()` (I12: the UTC-hour session weight made results depend on run time). The realistic fill model does not fill a doji that only wicked the limit, and no model fills a flat zero-volume (no-trade) bar. Heartbeat evaluation scores a checkpoint only while the label is unresolved at its close (`idx < resolve_idx`); scoring resolved events leaked the outcome (null tapes reached AUC ~0.60). Committed heartbeat AUCs predate this and must be re-run.
- **Trade-level Sharpe annualizes by sqrt(TRADES/yr), not sqrt(bars/yr) (v2.32.1)** — `monte_carlo_resample` operates on one return per TRADE. Scaling those by `annualization_factor(candle_interval_min)` inflated Sharpe ~93x on 60m bars and made the `mc_ci_lower_positive` rigor gate unable to bind. Pass `trades_per_year`, or accept the honest unannualized per-trade figure.
- **Nothing reachable from the brain's tool loop may run inline (v2.32.1)** — the tool dispatcher executes on the LIVE TICK THREAD. `run_backtest` and `sweep_param` both route through `BacktestWorkerPool` when mounted (I1). A new long-running tool handler must do the same or it stalls ticks, fill reconciliation and shutdown while positions are open.
- **Stream recovery runs in `finally` (v2.32)** — `ensure_healthy()` for the candle/ticker/balance/book streams sits in the tick body's `finally`, never the try body. A dead stream is the most common cause of a tick crash, so gating recovery on tick success is self-sustaining: crash → no restart → identical crash next tick, permanently, including no exits. Related: `engine_states[pair]` is explicitly `None` for a skipped pair, so consumers must use `.get(pair) or {}` — `.get(pair, {})` does NOT apply its default when the key exists.
- **The dashboard WS authenticates BOTH directions (v2.32.1)** — an accepted socket lands in `DashboardBroadcaster.pending` and receives **nothing**: no `latest_state`, no broadcast. It graduates into `clients` only via `_promote_client`, reached by a `{"type":"auth"}` handshake (or any command carrying a valid credential) that passes `_client_authenticated` — per-process token, or JWT in production mode. Unauthenticated sockets are closed after `HYDRA_WS_AUTH_GRACE_S`. Loopback binding narrows *who can reach the port*; it is no longer the thing protecting the account. `_origin_allowed` deliberately fails open for a missing `Origin` (tests, CLI, Electron) and is browser-CSRF defence only — never restore state-on-connect behind it. Client side: `connected` flips on `auth_ack`, not on socket open.
- **RSI/ATR = Wilder exponential smoothing, NOT SMA** (Bollinger = population variance)
- **SKIP ≠ BLOCK** — a soft restriction skips an action for the tick; BLOCK is reserved for hard rules (the 15% drawdown breaker)
- **`HYDRA_COMPANION_LIVE_EXECUTION` default OFF** — proposals are paper until opted in
- **Funding is markPrice-relative, never absolute** — Kraken Futures `PF_*` `fundingRate` is absolute USD-per-contract-per-period. Convert to bps via `(fundingRate / markPrice) * 10000`, never `fundingRate * 10000`. The `_absolute_to_relative_bps` helper in `hydra_derivatives_stream.py` enforces this (±500 bps clamp vs API drift). Pre-v2.15.2 fires used the wrong absolute conversion — not authoritative. **And it is per HOUR (audit 2026-10):** PF funding settles hourly (the repo's real-data carry tooling already read the feed that way), so `_funding_bps_8h` scales the per-period bps by `FUNDING_PERIODS_PER_8H` (8) and applies the ±500 bound to the 8h value. Unscaled, R1/R2's 80 bps/8h extreme needed 80 bps per hour (~7000% APR) and never fired. Not verified against the live API from the audit container — confirm on first live run.
- **Synthetic pairs declare themselves to R10** — `DerivativesSnapshot.synthetic=True` propagates to `quant_indicators["synthetic_pair"]`; R10 then tracks only funding/cvd/regime (the fields the synthetic path actually populates). Adding a new pair without a direct Kraken Futures perp requires this flag, otherwise R10 will structurally force-hold every tick.
- **Perp-only pairs declare themselves to R10 (v2.29)** — pairs whose Kraken Futures listing has a perp but NO quarterly contracts (ZEC: `PF_ZECUSD`) get `DerivativesSnapshot.basis_available=False`, derived from `SPOT_TO_DERIVATIVES.quarterly_prefix is None` at construction — map-driven, never from data presence. It propagates to `quant_indicators["basis_available"]`; R10 then tracks 4 fields (drops `basis_apr_pct`). Without it a perp-only pair sits permanently at 1 stale field and any transient miss trips a structural force-hold.
- **Uncovered pairs declare themselves to R10** — pairs with no `SPOT_TO_DERIVATIVES` entry at all (portfolio satellites, e.g. NIGHT/USD) get `quant_indicators["derivatives_covered"]=False` from `_build_quant_indicators`; R10 then tracks only CVD. Coverage is structural (pair in the futures map), never "snapshot present" — a covered pair with a warming/stale stream must still hit the R10 blackout.
- **Per-quote balance pools (v2.28)** — live stable-quoted engines are funded from the REAL holding of their own quote currency split across pairs sharing that quote (`_set_engine_balances`); a USDC engine never sizes against USD it cannot spend. Zero pool ⇒ balance 0 (sizer refuses entries) but `tradable` stays True so inventory can exit. Paper keeps the uniform split. A resumed book with `position.size > 0` or a `PLACED` journal row keeps its restored cash; flat engines with no working order are still seeded from the free pool minus cash already on those books.
- **Gross vs free balance are different numbers (v2.32)** — `BalanceStream.latest_balances()` / `_cached_balance` are GROSS (include funds locked behind our own resting post-only orders) and are what equity, peak equity and the portfolio drawdown breaker must read; held funds are still ours. `BalanceStream.latest_free_balances()` / `KrakenCLI.free_balance()` (`kraken extended-balance`) are NET of holds and are what every *spendability* decision must read — `_get_real_quote_balance` is that chokepoint. Sizing against gross re-commits money an unfilled order already owns, which is the `PLACEMENT_FAILED: insufficient_<quote>_balance` loop. Fail OPEN to gross when the hold field is absent or the free read failed (error envelope, exception, missing payload). A successful read with free 0 — the asset present at 0, or an empty free map because every unit is on hold — is not spendable. Equity still reads gross.
- **One resting order per pair** — a journal row in `PLACED` blocks another `execute_signal` on that pair. Same-side and an ordinary HOLD wait. An opposite signal cancels the resting order (paper/demo: restore `pre_trade_snapshot` and mark `CANCELLED_UNFILLED`; live: `cancel_order`, then the execution stream rolls the book back) and does not place the new order on that tick. A resting BUY is also cancelled by an entry veto (rules HOLD, brain HOLD, API-down block, confidence floor) or by the portfolio buy halt. A resting SELL is not cancelled by a HOLD. Stacking a second order makes the first order's true-up restore an older snapshot and wipe or double the position. A fill or cancel rewrites the row in place, so the tick must snapshot when the book changes even if the journal did not grow — otherwise `--resume` reloads the pre-fill position. A breaker halt is cleared on that cancel only when the halt was armed by the unfilled buy itself: the pre-buy book at the current price was under 15%. The mark is stored on the engine snapshot (`halt_from_unfilled_buy`). After `--resume`, a book that is still past 15% is marked again from the resting buy. A real breach stays sticky until `HYDRA_RESET_CIRCUIT_BREAKER=1`, including after the book recovers. A restored book that is still at or past 15% stays halted.
- **Deterministic size multipliers must survive every later sizing step** — a de-risking `size_multiplier` (brain quant × RM, or the R3/R5/R7 penalty stack) is only real if it reaches the placed order. The engine applies it as a pass-through capped at 1.5. Non-finite or negative sends nothing. It does not expand values above 1. A sell multiplier of 0 does not send, except a protected SELL (`is_protected_flatten_reason`: halt flatten, hold-through flatten, trend sleeve exit or trim). A partial sell penalty does not change the full close; the decision records `size_multiplier_applied: false` on a SELL. Any floor/override added after `size = size * effective_mult` in `_maybe_execute` must scale by `effective_mult` too, or it silently discards the whole risk stack (the v2.32 conviction-sizing fix). PR-B's "`max_position_pct` applies **after** brain `size_multiplier`" is the invariant. The API-cost haircut scores the notional about to be sent, after the vol haircut and the gross cap.
- **Trend overlay is evidence-gated and fails open** — every consumer of `daily_trend_long()` must treat `None` (warmup / disabled) as "behave exactly as pre-overlay". The daily-entry path (enter on ensemble alone) was tested and REJECTED (whipsawed against 1h flattens, −5.4% vs +0.1% 2y — `.hydra-flywheel/trend_entry_gate.json`); do not re-add without a passing gate. Daily closes are seeded at boot (agent: Kraken 1440m OHLC; backtest: pre-window sqlite) and persist in the snapshot.
- **The trend sleeve is daily-bandwidth only and default OFF (audit 2026-10)** — `HYDRA_TREND_SLEEVE=1` replaces the 1h signal generator and the hold-through rails with `Strategy.TREND`. It is long while the ensemble on COMPLETED daily closes (`sleeve_trend_score`, never the forming day) is ≥ 0.6, flat otherwise. A held sleeve is re-set every `SLEEVE_RESIZE_DAYS` (30) to `equity × max_position_pct × _sleeve_vol_multiplier()` unless already within `SLEEVE_RESIZE_TOL` (10%). Sized once and never trimmed, a rally drifted it to ~0.9 of equity: on independent monthly BTC that drew down 58% and tripped the sticky 15% breaker in year one, vs 14.3% re-sized (K3b post-hoc, `research/data/abi/`). The re-size trim is the engine's ONE partial SELL; every other SELL is still a full close. `TREND_SLEEVE:exit` and `TREND_SLEEVE:trim` are protected (rules, the LLM, the coordinator and a zero multiplier keep them); entries and top-ups stay vetoable. `execute_signal` admits a BUY only when the sleeve wants long and is flat or owes a due top-up. It refuses a non-protected SELL while the sleeve wants long, since a 1h exit re-couples it to the exits `trend_entry_gate.json` rejected. The halt flatten always passes. The re-size clock (`_sleeve_sized_day`) rides in `snapshot_position`, so a cancelled trim is owed again, and the fill appliers re-stamp it on a real fill. Top-ups and trims skip the entry friction gate and the API-cost haircut. **Who runs it is decided by evidence, per pair (`hydra_strategy_gate.resolve_trend_sleeve`)**: unset/`auto` enables a pair only when `research/data/trend_sleeve_gate.json` (or `HYDRA_TREND_SLEEVE_GATE`) is a `tools/trend_sleeve_gate.py --engine` report with verdict PASS **and** `significant`, built for the engine's current re-size rules and long threshold, younger than `HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS` (180), with that base asset's own PASS and engine check. Anything else, including a missing or malformed file, leaves the pair on the 1h engine. `1`/`0` override. Boot prints one `[STRATEGY]` line per pair; the dashboard gets `strategy_gate`. Never commit a gate report built on data other than the operator's store: it would switch production. The engine publishes `trend_sleeve.plan`: the daily close that flips the decision (`enter_above`/`exit_below`), each term's level, and the breaker price. These are closed-form and tested against the engine's own next-close decision (`docs/TRADE_PLAN.md`; registration `research/data/trend_sleeve_REGISTRATION.md`, amendment A1).
- **`exit_only` drain mode** — engine-level flag: BUY entries refused (SKIP semantics), every SELL path untouched. Set per-session by the agent (never persisted); the bridge default uses it. Composes with hold-through and the CB.
- **`hydra_rm_features.py` is pure** — no I/O, subprocess, network, or file access; every function returns `Optional[float]` (or `Optional[dict]`) from input alone, returning `None` on insufficient data. A future contributor adding side effects breaks the "fails-silent with None" contract that lets R10 and RM reason over missing vs corrupted data and that lets `HYDRA_RM_FEATURES_DISABLED` work as an instant rollback.
- **Research surfaces never place orders** — `hydra_s3` / `s3bounce` and
  `hydra_heartbeat_surface` / `heartbeat` are signal, display, shadow, or
  brain-advisory only. Grep-guards in tests forbid order verbs. No
  `HYDRA_S3_LIVE` or engine SKIP from heartbeat without a powered
  pre-registered bakeoff (engine co-occurrence currently FORBID claims:
  0 BUYs / 365d under rails).
- **An ambiguous placement is never `PLACEMENT_FAILED` (audit 2026-10)** — a placement error after which Kraken may already hold the order (`transport_timeout`/`_empty`/`_exit`/`_parse`, upstream `network`/`parse`; `placement_outcome_unknown`) keeps the row `PLACED` with `lifecycle.unconfirmed`, no txid, and the engine's optimistic book; the stream tracks it under `userref:<n>` and adopts the txid from the first execution entry. Rolling it back let the next tick place a duplicate live order. It is declared not accepted only after `UNCONFIRMED_PLACEMENT_GRACE_S` with the execution stream connected, without restart, since the send; a txid-less *success* (`accepted_without_txid`) or a row from an earlier process is never auto-rejected — it holds and warns. A `PLACED` row the stream cannot finalize (boot query error, sequence gap, cancel answered "Unknown order") is re-queried in session (`_requery_stale_placed`), and `_apply_execution_event` ignores a second terminal event for a row that is no longer `PLACED` (it would restore an old snapshot over later trades).
- **A working order the market left behind is re-priced (audit 2026-10)** — after `HYDRA_ORDER_REPRICE_S` (default 900) with the touch at least `HYDRA_ORDER_REPRICE_BPS` (default 15) beyond the limit, the order is cancelled (`_reprice_stale_resting`) and the engine re-decides at the current touch next tick, still limit post-only. The engine books fills optimistically, so a parked exit read flat while the coins fell (its drawdown and breaker blind). This is the one case where a resting SELL is cancelled without an opposite signal.
- **Paper never touches live state (audit 2026-10)** — paper/demo persist snapshot, rolling journal, backfill and tuner params under `.hydra-paper/` (`state_dir_for_mode`). Live `--resume` refuses a snapshot whose `paper` flag differs, and the journal merge drops rows whose `intent.paper` differs (`journal_row_matches_mode`; flagless legacy rows are live). The companion launcher runs `--paper` from the production directory; before this, its snapshot replaced the live books on the next `--resume`.
- **Finished bars reach the engine in final form (audit 2026-10)** — `CandleStream` keeps the previous interval's last push (`latest_closed_candle`) and `_fetch_and_tick` ingests it before the forming bar; a 300s tick otherwise froze every closed bar (and every daily close) up to 5 minutes early. With no pushed candle — paper, or a dead live stream — the pair ticks from `kraken ohlc` (CLI) instead of being skipped, so exits and halt flattens keep flowing during an outage. Live still decides on the forming bar ~12×/bar while backtests decide once per closed bar; that drift is declared, not fixed.
- **The LLM may veto or exit, never open (audit 2026-10)** — fresh deliberations and the same-candle cached replay apply one rule, `merge_llm_verdict(engine_action, final_signal)`: HOLD vetoes, BUY→SELL turns an entry into an exit, SELL→BUY becomes HOLD (an OVERRIDE used to open a long sized on the bearish signal's confidence), and nothing ever comes from an engine HOLD. The cache is keyed by (candle, engine action), so a BUY after a SELL deliberation is deliberated, not replayed. A non-protected SELL sized 0 by the LLM is an explicit HOLD, so QFE can still rescue a profitable exit. A SELL with no inventory skips the LLM entirely (cannot execute; 182/187 actionable rows of the Sept 2026 paper ledger). `HYDRA_QUANT_INDICATORS_DISABLED` removes the indicator rules, never the cached LLM veto. ADJUST appends to the engine reason (the ride-trend rail re-reads it). The RM prompt's drawdown mandates read `current_drawdown_pct`; `max_drawdown_pct` is shown as a record only, and the breaker mandate blocks BUYs, never exits.
- **The journal cap never drops a working order (audit 2026-10)** — `_trim_journal` removes session-only `PLACEMENT_FAILED` rows first, then the oldest terminal rows, never a `PLACED` row; a new working order sets `_books_dirty` so the snapshot is written even when the capped journal stops growing. A BalanceStream snapshot replaces both balance maps atomically (a merge kept assets sold to zero during an outage).
- **`PLACEMENT_FAILED` entries are session-only** — pre-exchange diagnostics (`insufficient_USD_balance`, `placement_error:api`) live in the in-memory `HydraAgent.order_journal` for live debugging but MUST NOT persist to `hydra_session_snapshot.json` or the rolling `hydra_order_journal.json`. The `_journal_for_persistence()` helper is the single chokepoint; both write paths (`_save_snapshot` and the per-tick rolling write) go through it. If you add a third write path, route it through the helper too.
- **Pair identity has one source of truth** — `hydra_pair_registry.PairRegistry` owns alias resolution (XBT↔BTC, XZEC↔ZEC, ZUSD↔USD, USDC.F→USDC, slashed↔slashless, case-insensitive) and per-pair metadata (price decimals, ordermin, costmin, tick size). `hydra_kraken_cli.KrakenCLI` delegates to the class-level `registry`. New pair-handling code must consume the registry — never re-implement an alias dict. v2.19 absorbed 1048 USDC literals into a single registry + role binding. **CLI schema drift degrades to the registry, never to a literal:** `load_pair_constants` skips (loudly) any pair missing/unparseable `pair_decimals`/`ordermin`/`costmin` rather than overlaying a generic default — a renamed key once gave every pair `ordermin=0.02`, which makes `write_off_dust` erase sub-0.02 BTC positions. `KrakenCLI.balance()` likewise skips unparseable entries instead of raising into three callers with no try/except.
- **Roles, not literal pair names, in coordinator/agent logic** — CrossPairCoordinator and HydraAgent address pairs by their `TradingTriangle` role (`stable_sol`, `stable_btc`, `bridge`), not by hardcoded `"SOL/USDC"` etc. `STABLE_QUOTES = {USD, USDC, USDT}`; the engine treats every member as $1. Switching the default quote is a config flip, not a refactor — see `hydra_config.HydraConfig.from_quote`.
- **R11/QFE is exit-only, profit-only, squeeze-filtered** — `evaluate_qfe()` in `hydra_quant_rules.py` lets a SELL through force_hold ONLY when: position is in profit (≥`QFE_MIN_PROFIT_PCT` = 1.0% mark, fee-cushioned), the engine already generated SELL, and no **deterministic** squeeze catalyst is present (`short_squeeze` OI regime, or extreme-short-funding + accumulation CVD). LLM `positioning_bias=crowded_short` alone does **not** veto QFE. QFE must never open a position, must never fire on an underwater position, and force_hold remains active for entries after QFE exits. Every QFE event logs a full trigger snapshot via `qfe_trigger_values` in `state["ai_decision"]`.
- **Exit guarantees (PR-A)** — Circuit breaker blocks BUY only; SELL always allowed when `position.size > 0` (halt flatten). **`tradable=False` likewise blocks entries only** — it is derived from the QUOTE balance, which a SELL does not spend, so gating exits on it stranded inventory forever (bridge `exit_only` drain, or a satellite whose quote pool emptied while holding the base) with the engine breaker suppressed and nothing else to force the flatten. SELL ignores `min_confidence` (entries still require it). R2 force_holds extreme-negative-funding **BUY** (bounce-chase), never spot SELL (long close).
- **Hard risk caps (PR-B)** — `max_position_pct` applies **after** brain `size_multiplier` and caps gross inventory (notional/equity). Peak equity never rebases downward on balance seed/resume, except a live first-seed of an unfunded quote must not treat constructor `--balance`/N as peak (that printed DD 100% and armed the BUY halt). Snapshot peaks stay. Portfolio max DD ≥ 15% sticky-blocks new BUYs (SELL still allowed). The portfolio flag is armed on current drawdown before Phase 2.5, not after the order is sent. R1–R11 do not rewrite a halt flatten or a hold-through flatten to HOLD, and `execute_signal` does not turn a `[QUANT RULES FORCE_HOLD]` back into a sell. Unknown bases are not sized or written off at a made-up 0.02 ordermin.
- **Fill true-up (PR-C)** — Every terminal FILLED/PARTIAL restores `pre_trade_snapshot` and replays at exchange `avg_fill_price` (not candle close). `pre_trade_snapshot` **is** persisted (only `PLACEMENT_FAILED` entries are stripped by `_journal_for_persistence`), so `_reconcile_stale_placed` trues up a previous-session fill and rolls back a phantom position on CANCELLED/REJECTED exactly like `_apply_execution_event` does live — treating it as in-memory-only is what left resume passing `None` and skipping both repairs. Unsellable dust below ordermin is written off. BUY limit offsets capped (≤20 bps SOL/STABLE) for post-only fill rate.
- **Kelly / friction honesty (PR-D)** — PositionSizer uses excess-over-threshold Kelly (conf=min → edge 0.10, conf=1 → 1.0), not `(conf*2-1)`. Friction hurdle is timeframe-aware (≥2.0% on 1h+ bars). Go-live plumbing gates: `python scripts/go_live_gates.py`.
- **Quant/cross-pair (PR-E)** — `HYDRA_QUANT_INDICATORS_DISABLED=1` skips `apply_rules`/QFE (no R10 blackout). Rules re-applied after brain OVERRIDE. Rule 2 recovery preferred over Rule 3 swap; Rule 3 requires bridge `tradable` (emitted on engine state from `_build_state`). Always `tick(generate_only=True)` then post-coord execute. USDT pairs mapped in `SPOT_TO_DERIVATIVES`. Companion live (opt-in) registers orders on `ExecutionStream` but remains engine-inventory-blind until a full agent place adapter exists.
- **No blanket bar-count hold (was PR-F's 50)** — regime and signals use whatever history each indicator actually has. RSI is neutral until 15 closes, MACD is zero until 26, Bollinger does not count a collapsed band as a touch. EMA trend cannot classify until both averages exist. There is no "warming up indicators" hold.

Subsystem detail (indicators, regime, Kelly sizing, price precision,
execution stream lifecycle, resume reconciliation, forex modifier,
shutdown) lives in the `hydra_engine.py` / `hydra_agent.py` docstrings and `SKILL.md`.

## Modules (thin index — details in deep specs)

| id | file | role |
|---|---|---|
| engine | `hydra_engine.py` | indicators, regime, signals, sizing, hold-through rails, daily trend overlay, default-OFF daily trend sleeve |
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
| streams | `hydra_streams.py` | `BaseStream` + Candle/Ticker/Book/Balance/Execution WS streams (kraken CLI subprocesses) |
| kraken_cli | `hydra_kraken_cli.py` | `KrakenCLI` wrapper via WSL (pin + flag notes in docstring; `forward_credentials`) |
| ws_server | `hydra_ws_server.py` | dashboard WS: `DashboardBroadcaster`, auth handshake, `hydra_ws_token.json`; JWT when `HYDRA_PRODUCTION=1` |
| auth | `hydra_auth.py` | users DB + JWT/session; secrets persist in `hydra_auth_state.json` (env overrides) |
| history_store | `hydra_history_store.py` | canonical OHLC sqlite (`hydra_history.sqlite`) for backtest/research — not for trading decisions |
| tape_capture | `hydra_tape_capture.py` | CandleStream → history sqlite writer; bounded queue, drops on full (never stalls the tick) |
| kraken_trades | `hydra_kraken_trades.py` | sqlite of every Kraken fill (`hydra_kraken_trades.sqlite`) — accounting/P&L truth, ≠ order journal |
| walk_forward | `hydra_walk_forward.py` | anchored quarterly folds + paired Wilcoxon (Research Lab) |
| strategy_gate | `hydra_strategy_gate.py` | per-pair evidence gate for the trend sleeve: reads the `tools/trend_sleeve_gate.py` report; `python -m hydra_strategy_gate` prints what Hydra will trade. No orders, no state |

## Deep specs

- `SKILL.md` — trading formulas + risk rules · `CHANGELOG.md` · `SECURITY.md`
- `docs/BACKTEST.md` runbook · `docs/BACKTEST_SPEC.md` design archive (defaults: code)
- `docs/COMPANION_SPEC.md` · `docs/HOLD_THROUGH.md` · `docs/TRADE_PLAN.md` (the plan the sleeve trades, the evidence behind each rule, the per-session trade cycle)
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
| paper_state | `.hydra-paper/` | owner `agent` in `--paper`/`--demo`: that mode's snapshot, rolling journal, backfill and `hydra_params_<pair>.json`; never read by live (`state_dir_for_mode`); gitignored |
| params | `hydra_params_<pair>.json` | per-pair learned tuning params; gitignored |
| errors_log | `hydra_errors.log` | tick try/except writes here with full traceback; loop continues |
| companion_memory | `.hydra-companions/memory/{user}_{companion}.jsonl` | per-companion distilled facts; local JSONL, authoritative, 4KB LRU budget; gitignored |
| experiments_store | `.hydra-experiments/` | owner `experiments`; `presets.json` bootstraps from code on first init (delete to regenerate) |
| s3_shadow | `.hydra-s3/` | owner `s3` (`s3bounce.ShadowLedger` via `hydra_s3`); `events.jsonl` append-only audit + `state.json` open shadow positions/proposal dedupe (atomic `.tmp → os.replace`; garbage state treated as empty, events remain the audit trail); survives `--resume` independently of the snapshot; gitignored |
| flywheel_store | `.hydra-flywheel/` | owner `flywheel`; `state.json` paper ledger (atomic `.tmp → os.replace`), validation/carry/trend evidence JSONs, downloaded funding history; gitignored |
| history_db | `hydra_history.sqlite` | owner `history_store`; `tape_capture` upserts `source='tape'`; path via `HYDRA_HISTORY_DB`; gitignored |
| kraken_trades_db | `hydra_kraken_trades.sqlite` | owner `kraken_trades`; fill ledger-of-truth; gitignored |
| auth_secrets | `hydra_users.db`, `hydra_auth_state.json`, `hydra_ws_token.json` | owner `auth` (users/state) + `ws_server` (token); **secrets**; gitignored |

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
| `HYDRA_ORDER_REPRICE_S` | execution | Seconds a working order may rest before it is eligible for re-pricing. Default **`900`**; `=0` disables. Cancels only when the touch is also `HYDRA_ORDER_REPRICE_BPS` beyond the limit; the engine re-decides next tick (`_reprice_stale_resting`). |
| `HYDRA_ORDER_REPRICE_BPS` | execution | Minimum distance (bps) between a working order's limit and the current touch (bid for BUY, ask for SELL) before a re-price cancel. Default **`15`**. |
| `HYDRA_QUOTE` | config | Fallback quote for `--pairs auto` (`USD`/`USDC`/`USDT`). `--quote` > env > `DEFAULT_QUOTE` (USD). If that pool is unfunded, cores switch to the largest funded stable. Explicit `--pairs` is unchanged. |
| `HYDRA_BRIDGE_TRADING` | agent | `=1` re-enables SOL/BTC bridge trading. Default OFF (v2.28): the bridge runs exit_only drain mode — evidence in `.hydra-flywheel/bridge_isolation.json` (0 trades/1y; Sharpe drag 2y). Candles/synthetic funding still stream as signal input. |
| `HYDRA_TREND_OVERLAY` | engine | **Default ON** (v2.28). Daily trend-ensemble gate: BUY additionally requires daily ensemble long (0.4·sma200 + 0.4·ema20x100 + 0.2·don55 on daily closes, long ≥ 0.6); open positions flatten on ensemble flip. Fails OPEN (None) below 210 daily closes or when `=0`. Won 6/6 real-tape windows (`.hydra-flywheel/trend_overlay_gate.json`). |
| `HYDRA_TREND_CONVICTION_SIZING` | engine | **Default ON** (v2.28). Overlay-long entries allocate vol-target × max_position_pct of balance (Kelly is the floor). `=0` reverts to pure Kelly sizing. Won 3/3 windows (`.hydra-flywheel/conviction_sizing_gate.json`). |
| `HYDRA_TREND_SLEEVE` | engine/agent | **Default `auto`.** `auto` (or unset): a pair trades the daily trend sleeve (`Strategy.TREND`) only when the pre-registered gate passed, significantly, on the operator's own data for its base asset (`hydra_strategy_gate`; `run_strategy_gate.bat`). `1` forces it on every pair, `0` off. The sleeve decides on completed daily closes, re-sizes to the vol target every 30 days, and skips the 1h signals and hold-through rails. Read at agent start (restart to flip). Backtests run it only on `1`. |
| `HYDRA_TREND_SLEEVE_GATE` | agent | Path of the gate report `auto` reads. Default `research/data/trend_sleeve_gate.json`. |
| `HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS` | agent | Evidence older than this switches `auto` back off. Default `180`; re-run the gate quarterly. |
| `HYDRA_TREND_TARGET_VOL` | engine | Annualized vol target (percent) for overlay sizing. Default `30.0`. |
| `HYDRA_AUTO_QUOTE` | agent | Forces the satellite quote for `--pairs auto` (`USD`/`USDC`/`USDT`). Default unset: prefer USDC when funded, else `--quote`/`HYDRA_QUOTE`. USD stays required for USD-only listings (e.g. NIGHT/USD). |
| `HYDRA_TAPE_CAPTURE` | history | `=1` (default) wires CandleStream candle-close pushes into a bounded-queue writer that upserts to `hydra_history.sqlite` (`source='tape'`). Set `=0` to disable (e.g. paper-mode tests on a shared DB). |
| `HYDRA_HISTORY_DB` | history | Path override for the canonical OHLC store. Defaults to `hydra_history.sqlite` in the working directory. Used by the agent (tape capture), `tools/refresh_history.py`, and the SqliteSource backtest path. |
| `HYDRA_WSL_DISTRO` | cli | WSL distribution name for all `kraken` CLI invocations. Defaults to `Ubuntu`. Override if your distro is named differently (e.g. `Ubuntu-24.04`). Single source of truth: `hydra_kraken_cli.WSL_DISTRO`; isolated modules read the env var directly. |
| `HYDRA_FRICTION_GATE_DISABLED` | engine | `=1` disables the friction expectancy gate (v2.27): BUY entries whose strategy-implied expected move (BB-mid reversion distance or 2×ATR%) is under `FRICTION_HURDLE_MULT × ROUND_TRIP_FRICTION_PCT` (0.84%) are skipped (SKIP semantics). Entries only — exits never gated; fails open on insufficient history. Active on BOTH `tick()` and `execute_signal()` paths. |
| `HYDRA_HOLD_THROUGH` | engine | **Default ON** (all pairs). TREND_UP BUY ≥0.65, flatten `TREND_DOWN` (and `VOLATILE` over an EMA downtrend), ride mid-UP except extreme overbought. Only `0/false/no/off` turns a default-ON engine switch off (`_env_default_on`; an empty value used to disable it). `=0` = raw engine (research/tests). Does not disable friction or 15% CB. Spec: `docs/HOLD_THROUGH.md`. Replaces removed `HYDRA_REGIME_SELECTIVE`. |
| `HYDRA_S3_DISABLED` | s3 | `=1` removes the S3 signal surface entirely (no daily tracking, no `quant_indicators["s3"]`, no shadow). Read per call — live-flippable. Default unset (signal ON). |
| `HYDRA_S3_STRATEGY` | s3 | **Default OFF.** `=1` enables the S3 shadow strategy: gated entryable-b1 signals are logged as proposals with per-exit-arm paper positions in `.hydra-s3/`. Structurally shadow-only — this flag has NO code path to an order; live enablement is a future, gate-pending PR (needs the shadow window + `/bakeoff` to clear). |
| `HYDRA_S3_HEARTBEAT_STATUS_DIR` | s3 + heartbeat surface | Directory of heartbeat status files (`heartbeat_status_<PAIR>.json`). Default `heartbeat/data`. Missing/stale(>300s)/tainted ⇒ `no_opinion` (never fabricate 0.5). Used by S3 shadow confirmer and dashboard surface. |
| `HYDRA_HEARTBEAT_SURFACE` | agent/dashboard | **Default ON.** `=0` removes `quant_indicators["heartbeat"]` (P(up) display). Read-only — no order path. Requires separate `heartbeat run` process for live values. |
| `HYDRA_FEE_DEDUCTION_DISABLED` | agent | `=1` reverts fee-true accounting (v2.27): confirmed fills debit `lifecycle.fee_quote` from the engine's quote balance exactly once (idempotent via `lifecycle.fee_applied`). Default off (fees deducted) — pre-v2.27 live P&L was overstated ~16 bps/fill vs the backtest, which always deducted fees. |
| `HYDRA_WS_HOST` | dashboard | Dashboard WS bind address. Default **`127.0.0.1`** (v2.32.1). Auth, not the bind, protects account state (WS auth invariant). `=0.0.0.0` only behind an auth-terminating proxy. |
| `HYDRA_WS_AUTH_GRACE_S` | dashboard | Seconds an unauthenticated socket may stay open before the reaper closes it (`1008 auth required`). Default **`10`**. Unauthenticated sockets already receive nothing. |
| `HYDRA_RESET_CIRCUIT_BREAKER` | engine/agent | `=1` clears a persisted 15% CB halt once, at resume (per-engine `halted` + portfolio BUY halt). Default unset — a breach persists across `--resume`. Clears the FLAG only (peak/max-DD records kept); re-arms if the CURRENT drawdown is still ≥15% (CB invariant). Use after reviewing the drawdown, not routinely. |
| `HYDRA_CLI_LEGACY_SECRET_EXPORT` | cli | `=1` restores pre-v2.32 interpolation of `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` into the `bash -c` string — visible in `wsl` argv via `ps`/procfs. Default off: secrets go through the child ENVIRONMENT + `WSLENV` (`KrakenCLI.forward_credentials`, shared by the REST wrapper and the long-lived `hydra_streams.BaseStream` subprocesses). Multi-tenant path only. |
| `HYDRA_PRODUCTION` | dashboard | `=1` → WS clients must authenticate with a JWT (production mode). Default `0`: per-process token (`hydra_ws_token.json`). |
| `HYDRA_WS_PORT` | dashboard | Dashboard WS port. Default `8765`. |
| `HYDRA_DEBUG_TOOLS` | brain | `=1` prints the full traceback when a brain tool-use call throws (default: one-line message, engine fallback). |
| `HYDRA_DEMO_EXPORT` | agent | `=1` keeps demo-mode export dumps (default: cleaned up). |
| `HYDRA_NO_DOTENV` | companion | `=1` skips the companion subsystem's `.env` loading. |
| `HYDRA_AUTH_DB_PATH` / `HYDRA_AUTH_STATE_PATH` | auth | Override `hydra_users.db` / `hydra_auth_state.json`. |
| `HYDRA_JWT_SECRET` / `HYDRA_ENCRYPTION_KEY` / `HYDRA_ADMIN_PASSWORD` / `HYDRA_NEW_USER_PASSWORD` | auth | Secrets: JWT/encryption keys (env overrides the persisted state file); admin seed + new-user passwords. See `hydra_auth.py` docstring. Never commit. |

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
- `run_strategy_gate.bat` — refreshes `hydra_history.sqlite`, runs `tools/trend_sleeve_gate.py --engine`, prints the per-pair strategy `auto` will pick. No orders

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
- **Tests pass:** CI green (`watermark-gate` + `engine-tests` +
  `dashboard-build`). Mock harness (`tests/live_harness/harness.py --mode mock`)
  **MANDATORY** for any PR touching execution path. `watermark-gate` is a
  merge blocker: agent trailers / Generated-with banners / actionable
  invisible Unicode on files changed vs `main`. Not Layer B.
- **Enumerate first:** `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` before bumping (Rule 5)
- **Tag:** signed; verify (Rule 3)
- **Automation:** `/release` skill codifies the cycle. Never merge with red or pending CI.

Tests: `python -m pytest tests/` or individual `python tests/test_*.py`
(CI pattern). Live harness detail in `tests/live_harness/` (`harness.py`
modes: smoke/mock/validate/live).

## Audit

`/audit` drives the full cycle — its SKILL.md owns the 7-way partition (Rule 1) and triage.

**HIGH severity:** violations of backtest I1–I12 (`docs/BACKTEST_SPEC.md`),
limit-post-only, 2s rate-limit floor, 15% circuit breaker, Wilder-EMA RSI/ATR
spec, or `HYDRA_COMPANION_LIVE_EXECUTION` default-off.

**Two-phase protocol (Rule 4):** after fixing HIGH/MED, re-run partition
sweep against your diff, then full tests + `harness.py --mode mock`;
declare done only when phase 2 is clean.

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
- Don't reduce rate limiting below 2s — Kraken throttles or bans
- Don't merge engine instances across pairs — they must remain independent
- `.env` contains Kraken API keys — never commit
- On shutdown agent cancels all resting limit orders and flushes snapshot — do not bypass
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove
- **FEATURE GAP:** `CrossPairCoordinator` Rule 2 (BTC recovery BUY boost) + Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favors safer SELL); future: explicit priority or merge logic
- Companion live execution opt-in: `HYDRA_COMPANION_LIVE_EXECUTION=1`; confirm unset before live debugging
- Confirm `kraken --version` matches the **v0.4.1** pin (Defaults) before debugging `--validate` schema errors
