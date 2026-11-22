# Reference tables

Modules, env flags and state files, kept out of CLAUDE.md so they load on demand. Update the matching table in the same change as a module add/remove/rename/split, a new env flag or kill switch, or a state-file ownership change (CLAUDE.md HARD REQUIREMENT). CLAUDE.md repeats the money- and security-critical defaults; keep the two in sync.

## Modules

| id | file | role |
|---|---|---|
| engine | `hydra_engine.py` | indicators, regime, signals, sizing, hold-through rails, daily trend overlay, evidence-gated daily trend sleeve (`trend_sleeve.plan`) |
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
| heartbeat | `heartbeat/` + `hydra_heartbeat_surface.py` | Order-flow P(up) display + shadow co-log. The 2026-07 BTC/ETH PASS is withdrawn (leak-fixed re-run 2026-10-02). **No order path.** Separate `heartbeat run` (`start_heartbeat.bat`); status `heartbeat_status_<PAIR>.json`; USDC/USDT engines fall back to the USD tape (same-base order flow). Agent → `quant_indicators["heartbeat"]` + dashboard; kill `HYDRA_HEARTBEAT_SURFACE=0`. Ledger: `heartbeat/HONEST_FINDINGS.md` |
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

## Env flags (kill switches and opt-ins)

| flag | scope | effect |
|---|---|---|
| `HYDRA_BACKTEST_DISABLED` | backtest | kill when `=1` only; worker pool off, WS rejects backtest msgs |
| `HYDRA_BRAIN_TOOLS_ENABLED` | brain | enables Anthropic tool-use for Analyst+RM (Grok stays text-only) |
| `HYDRA_QUANT_INDICATORS_DISABLED` | brain/quant | `=1` skips DerivativesStream + R1-R11 quant rules; Quant sees no funding/OI/CVD block and no force_hold from rules |
| `HYDRA_TAX_FRICTION_FLOOR_USD` | brain | Tax/fee friction floor in USD (default `50.0`; `hydra_brain.TAX_FRICTION_FLOOR_USD`). On a SELL that would realize a gain below the floor, the analyst prompt gets a soft advisory line — **advisory only, never a gate**. `=0` suppresses it; cutting a loss or banking a gain ≥ floor never triggers it. |
| `HYDRA_COMPANION_DISABLED` | companion | kill (no orb) |
| `HYDRA_COMPANION_PROPOSALS_ENABLED` | companion | default on; `=0` for no trade cards |
| `HYDRA_COMPANION_NUDGES` | companion | **default OFF**; `=1` opts in. Nudges fire on 1h regime flips (no measured edge) and each costs an LLM call; `start_hydra_companion.bat` (paper) turns them on |
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
| `HYDRA_TREND_SLEEVE` | engine/agent | **Default `auto`.** `auto` (or unset/empty): a pair trades the daily trend sleeve (`Strategy.TREND`) only when the pre-registered gate passed, significantly, on the operator's own data for its base asset (`hydra_strategy_gate`; `run_strategy_gate.bat`). `1` forces it on every pair, `0` off; any other value is off and the `[STRATEGY]` reason names it. The sleeve decides on completed daily closes, re-sizes to the vol target every 30 days, and skips the 1h signals and hold-through rails. Read at agent start (restart to flip). Backtests run it only on `1`. |
| `HYDRA_TREND_SLEEVE_GATE` | agent | Path of the gate report `auto` reads. Default `research/data/trend_sleeve_gate.json`. |
| `HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS` | agent | Evidence older than this switches `auto` back off. Default `180`; re-run the gate quarterly. |
| `HYDRA_TREND_TARGET_VOL` | engine | Annualized vol target (percent) for overlay and sleeve sizing. Default `30.0`; non-finite or unparseable values are the default (`trend_target_vol_setting`). The gate records the value it tested, and `auto` refuses a report built for another one: re-run the gate after changing it. |
| `HYDRA_AUTO_QUOTE` | agent | Forces the satellite quote for `--pairs auto` (`USD`/`USDC`/`USDT`). Default unset: prefer USDC when funded, else `--quote`/`HYDRA_QUOTE`. USD stays required for USD-only listings (e.g. NIGHT/USD). |
| `HYDRA_TAPE_CAPTURE` | history | `=1` (default) wires CandleStream candle-close pushes into a bounded-queue writer that upserts to `hydra_history.sqlite` (`source='tape'`). Set `=0` to disable (e.g. paper-mode tests on a shared DB). |
| `HYDRA_HISTORY_DB` | history | Path override for the canonical OHLC store. Defaults to `hydra_history.sqlite` in the working directory. Used by the agent (tape capture), `tools/refresh_history.py`, `tools/trend_sleeve_gate.py`, and the SqliteSource backtest path. |
| `HYDRA_WSL_DISTRO` | cli | WSL distribution name for all `kraken` CLI invocations. Defaults to `Ubuntu`. Override if your distro is named differently (e.g. `Ubuntu-24.04`). Single source of truth: `hydra_kraken_cli.WSL_DISTRO`; isolated modules read the env var directly. |
| `HYDRA_FRICTION_GATE_DISABLED` | engine | `=1` disables the friction expectancy gate (v2.27): BUY entries whose strategy-implied expected move (BB-mid reversion distance or 2×ATR%) is under `FRICTION_HURDLE_MULT × ROUND_TRIP_FRICTION_PCT` (0.84%) are skipped (SKIP semantics). Entries only — exits never gated; fails open on insufficient history. Active on BOTH `tick()` and `execute_signal()` paths. |
| `HYDRA_HOLD_THROUGH` | engine | **Default ON** (all pairs). TREND_UP BUY ≥0.65, flatten `TREND_DOWN` (and `VOLATILE` over an EMA downtrend), ride mid-UP except extreme overbought. Only `0/false/no/off` turns a default-ON engine switch off (`_env_default_on`; an empty value used to disable it). `=0` = raw engine (research/tests). Does not disable friction or 15% CB. Spec: `docs/TRADE_PLAN.md` (Hold-through rails). Replaces removed `HYDRA_REGIME_SELECTIVE`. |
| `HYDRA_S3_DISABLED` | s3 | `=1` removes the S3 signal surface entirely (no daily tracking, no `quant_indicators["s3"]`, no shadow). Read per call — live-flippable. Default unset (signal ON). |
| `HYDRA_S3_STRATEGY` | s3 | **Default OFF.** `=1` enables the S3 shadow strategy: gated entryable-b1 signals are logged as proposals with per-exit-arm paper positions in `.hydra-s3/`. Structurally shadow-only — this flag has NO code path to an order; live enablement is a future, gate-pending PR (needs the shadow window + `/bakeoff` to clear). |
| `HYDRA_S3_HEARTBEAT_STATUS_DIR` | s3 + heartbeat surface | Directory of heartbeat status files (`heartbeat_status_<PAIR>.json`). Default `heartbeat/data`. Missing/stale(>300s)/tainted ⇒ `no_opinion` (never fabricate 0.5). Used by S3 shadow confirmer and dashboard surface. |
| `HYDRA_START_HEARTBEAT` | launcher | `=1` makes `start_hydra.bat` start the heartbeat research process. **Default off**: no order path, and its committed AUCs predate the 2026-10 label leak fix. |
| `HYDRA_HEARTBEAT_SURFACE` | agent/dashboard | **Default ON.** `=0` removes `quant_indicators["heartbeat"]` (P(up) display). Read-only — no order path. Requires separate `heartbeat run` process for live values. |
| `HYDRA_FEE_DEDUCTION_DISABLED` | agent | `=1` reverts fee-true accounting (v2.27): confirmed fills debit `lifecycle.fee_quote` from the engine's quote balance exactly once (idempotent via `lifecycle.fee_applied`). Default off (fees deducted) — pre-v2.27 live P&L was overstated ~16 bps/fill vs the backtest, which always deducted fees. |
| `HYDRA_WS_HOST` | dashboard | Dashboard WS bind address. Default **`127.0.0.1`** (v2.32.1). Auth, not the bind, protects account state (WS auth invariant). `=0.0.0.0` only behind an auth-terminating proxy. |
| `HYDRA_WS_AUTH_GRACE_S` | dashboard | Seconds an unauthenticated socket may stay open before the reaper closes it (`1008 auth required`). Default **`10`**. Unauthenticated sockets already receive nothing. |
| `HYDRA_RESET_CIRCUIT_BREAKER` | engine/agent | `=1` clears a persisted 15% CB halt at resume (per-engine `halted` + portfolio BUY halt) and starts a new drawdown epoch: the peak moves to the equity at the first tick after the reset, the max-DD record is kept, and the breaker arms again 15% below the new peak (CB invariant). Default unset — a breach persists across `--resume`. It acts on EVERY start while set, so unset it after the start that cleared the halt. Use after reviewing the drawdown, not routinely. |
| `HYDRA_CLI_LEGACY_SECRET_EXPORT` | cli | `=1` restores pre-v2.32 interpolation of `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` into the `bash -c` string — visible in `wsl` argv via `ps`/procfs. Default off: secrets go through the child ENVIRONMENT + `WSLENV` (`KrakenCLI.forward_credentials`, shared by the REST wrapper and the long-lived `hydra_streams.BaseStream` subprocesses). Multi-tenant path only. |
| `HYDRA_PRODUCTION` | dashboard | `=1` → WS clients must authenticate with a JWT (production mode). Default `0`: per-process token (`hydra_ws_token.json`). |
| `HYDRA_WS_PORT` | dashboard | Dashboard WS port. Default `8765`. |
| `HYDRA_DEBUG_TOOLS` | brain | `=1` prints the full traceback when a brain tool-use call throws (default: one-line message, engine fallback). |
| `HYDRA_BRAIN_JSONL` | brain | Path override for the per-call brain decision log (default `hydra_brain.jsonl` beside `hydra_brain.py`; the parent dir is created if missing). The `tools/grok_paper_*` emulators point it at their own output dir. |
| `HYDRA_DEMO_EXPORT` | agent | `=1` keeps demo-mode export dumps (default: cleaned up). |
| `HYDRA_NO_DOTENV` | companion | `=1` skips the companion subsystem's `.env` loading. |
| `HYDRA_AUTH_DB_PATH` / `HYDRA_AUTH_STATE_PATH` | auth | Override `hydra_users.db` / `hydra_auth_state.json`. |
| `HYDRA_JWT_SECRET` / `HYDRA_ENCRYPTION_KEY` / `HYDRA_ADMIN_PASSWORD` / `HYDRA_NEW_USER_PASSWORD` | auth | Secrets: JWT/encryption keys (env overrides the persisted state file); admin seed + new-user passwords. See `hydra_auth.py` docstring. Never commit. |

## State files

Operating Rule 2: find the owner here and stop it before editing its state.

| id | path | ownership / notes |
|---|---|---|
| snapshot | `hydra_session_snapshot.json` | atomic `.tmp → os.replace`; `--resume` target; embeds v2.18.0 `derivatives_history` (OI + mark-price deques, rehydrated with 30 min staleness gate). Same-base stable remap on resume when triangle is None (BTC/USD → BTC/USDC); journal pair fields stay on the market they traded. `portfolio_drawdown` holds the portfolio halt itself (`buy_halted`) and what its peak measures (`basis: engine_books`); each engine carries `peak_epoch` and `peak_rebase_pending` for the operator reset |
| order_journal | `hydra_order_journal.json` | snapshots immediately on any tick that appends (crash cannot lose since last successful tick); gitignored |
| paper_state | `.hydra-paper/` | owner `agent` in `--paper`/`--demo`: that mode's snapshot, rolling journal, backfill and `hydra_params_<pair>.json`; never read by live (`state_dir_for_mode`); gitignored |
| params | `hydra_params_<pair>.json` | per-pair learned tuning params; gitignored |
| errors_log | `hydra_errors.log` | tick try/except writes here with full traceback; loop continues |
| brain_log | `hydra_brain.jsonl` | owner `brain`; one JSON line per brain call for brain-vs-engine A/B review; path via `HYDRA_BRAIN_JSONL`; gitignored |
| companion_memory | `.hydra-companions/memory/{user}_{companion}.jsonl` | per-companion distilled facts; local JSONL, authoritative, 4KB LRU budget; gitignored |
| experiments_store | `.hydra-experiments/` | owner `experiments`; `presets.json` bootstraps from code on first init (delete to regenerate) |
| s3_shadow | `.hydra-s3/` | owner `s3` (`s3bounce.ShadowLedger` via `hydra_s3`); `events.jsonl` append-only audit + `state.json` open shadow positions/proposal dedupe (atomic `.tmp → os.replace`; garbage state treated as empty, events remain the audit trail); survives `--resume` independently of the snapshot; gitignored |
| flywheel_store | `.hydra-flywheel/` | owner `flywheel`; `state.json` paper ledger (atomic `.tmp → os.replace`), validation/carry/trend evidence JSONs, downloaded funding history; gitignored |
| trend_sleeve_gate | `research/data/trend_sleeve_gate.json` | owner `tools/trend_sleeve_gate.py` (`run_strategy_gate.bat`); read once at agent start by `hydra_strategy_gate` under `HYDRA_TREND_SLEEVE=auto`; gitignored — a committed copy would switch every checkout that pulls it |
| history_db | `hydra_history.sqlite` | owner `history_store`; `tape_capture` upserts `source='tape'`; path via `HYDRA_HISTORY_DB`; gitignored |
| kraken_trades_db | `hydra_kraken_trades.sqlite` | owner `kraken_trades`; fill ledger-of-truth; gitignored |
| auth_secrets | `hydra_users.db`, `hydra_auth_state.json`, `hydra_ws_token.json` | owner `auth` (users/state) + `ws_server` (token); **secrets**; gitignored |
