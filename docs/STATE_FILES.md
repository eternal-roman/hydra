# State files

Moved from `CLAUDE.md` so it loads on demand. Operating Rule 2: check the
owner here and stop it before editing its state. Update this table in the
same change as any state-file ownership change.

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
