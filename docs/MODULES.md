# Modules (thin index)

Moved from `CLAUDE.md` so it loads on demand. Update this table in the same
change as any module add/remove/rename/split (CLAUDE.md HARD REQUIREMENT).

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
