# Changelog

All notable changes to HYDRA are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.34.5] — 2026-11-14

### Fixed

- **The 15% breakers measure trading losses on Hydra's own books.** The portfolio BUY halt was measured on the whole Kraken account. A fall in coins no engine held, or a withdrawal, read as drawdown and blocked every BUY. It now reads the engine books (quote cash plus positions the engines hold), the same sum the backtest uses. A held position with no price yet is skipped for that tick instead of being counted as worth nothing.
- **A deposit, a withdrawal, or the re-split of the quote pool at boot is not a gain or a loss.** A flat engine re-seeded with a smaller slice kept its old peak and halted on the next tick, including the engine that had made money before an equal re-split. A larger slice lifted equity past the peak and erased a real drawdown. Seeding now rescales the peak so the drawdown fraction is the same before and after, for each engine and for the portfolio. With no cash moving the peak stays exactly where it was.
- **The operator reset works, and one reset is enough.** `HYDRA_RESET_CIRCUIT_BREAKER=1` used to clear the flag and keep the peak. A breaker halt flattens the book, so what is left is cash 15% under that peak: the next tick halted again, every time. The reset now starts a new drawdown epoch at the equity on the first tick after it. The maximum drawdown stays as the record, and the breaker arms again 15% below the new peak. An order placed before the reset no longer brings the old peak back when it is rolled back.
- **The portfolio halt is saved as a flag.** Resume re-derived it from the recorded maximum drawdown, which never falls, so every later start halted again after a reset, even with the account at a new high. The snapshot now carries the halt itself.

### Changed

- **Upgrading.** A snapshot written before this release holds a whole-account portfolio peak. That peak is dropped and restarts at the first tick. A breach it recorded stays halted until the operator reset, as before. An engine that was halted by a withdrawal under the old rule also stays halted until the reset.
- **`HYDRA_RESET_CIRCUIT_BREAKER=1` acts on every start while it is set.** Boot prints a warning. Unset it after the start that cleared the halt.
- **Paper sessions.** The portfolio halt in `--paper` was measured on the real account balance. It is now measured on the paper books.

### Also in this release

Merged in #245 after v2.34.4 and not released until now. Each has a regression test.

- **Paper never touches live state.** Paper and demo persist under `.hydra-paper/`. Live resume refuses a paper snapshot and drops paper rows from the journal merge.
- **An ambiguous placement is not rolled back.** A placement whose reply was lost stays `PLACED` as unconfirmed and is adopted from the execution stream by userref. Rolling it back let the next tick place a duplicate order. Rows the stream cannot finalize are re-queried in session.
- **A working order the market left behind is re-priced.** After 15 minutes and 15 bps it is cancelled so the engine decides again at the touch. `HYDRA_ORDER_REPRICE_S` and `HYDRA_ORDER_REPRICE_BPS` tune or disable it.
- **The LLM may veto or exit, never open.** One rule applies its verdict on fresh and cached ticks. A SELL with no inventory no longer calls it.
- **Funding is scaled to the 8-hour basis R1 and R2 are written in.** Kraken PF funding settles hourly, so unscaled the rules could not fire.
- **Finished bars reach the engine in final form**, and a live pair with no pushed candle ticks from `kraken ohlc` instead of being skipped.
- **Engine.** The hold-through flatten fires when a dump is labelled VOLATILE. CVD divergence is normalised by window volume. The trend vol multiplier fails open until the overlay can score. One equity point per candle.
- **Evidence tooling.** The Monte Carlo trade bootstrap is circular with a short block. Walk-forward slices pairs on one clock. Synthetic bars use a fixed epoch. Heartbeat evaluation skips resolved checkpoints. A filled backtest order keeps the strategy, confidence and reason of its decision.

---

## [2.34.4] — 2026-09-27

### Fixed

- **`KrakenCLI.ticker()` reads the pinned CLI's ticker.** kraken-cli v0.4.1 returns named fields per pair (`last_price`, `bid_price`, `ask_price`, ...), not the legacy `c`/`b`/`a` arrays, so `ticker()` returned the raw envelope with no bid or ask. It now returns the same flat row for both shapes and still passes errors and unknown payloads through. The live harness L1/L2 scenarios can read a price again. The live order path is unchanged: it prices from `TickerStream` only.

---

## [2.34.3] — 2026-09-26

### Fixed

- **A buy cancel no longer clears a real 15% halt.** The halt is dropped on cancel only when the pre-buy book was under 15% and the unfilled buy is what armed it. That mark is saved on the engine snapshot. A restarted process marks it again while the live book is still past 15%. A real breach stays until the operator reset, including after the account recovers.
- **A brain timeout no longer waits out the call.** The tick keeps the pre-brain state and runs the deterministic rules for any pair that did not finish. A call that returns after that does not become the next tick's cached decision. The thread pool does not block shutdown on the workers still running.

---

## [2.34.2] — 2026-09-26

### Fixed

- **The order is the decision that survived the rules.** A rules HOLD is no longer sold by the downtrend rail. A halt flatten and a hold-through flatten are not rewritten to HOLD. A resting buy is cancelled by an entry veto or the portfolio buy halt. An ordinary HOLD still leaves a working order alone.
- **The portfolio 15% buy halt arms before the order.** The crossing tick no longer buys. A halt that existed only because an unfilled buy was already on the book is cleared when that cancel restores a book under 15%.
- **Size 1.5 means 1.5.** The engine no longer expands it to 2. Non-numeric size is neutral in the brain and sends nothing if it reaches the engine. A sell at multiplier 0 does not send, except a flatten the rail already chose. A partial sell penalty still full-closes, and the decision records that the penalty was not applied to the quantity. The API-cost haircut scores the order about to be sent.
- **An unsellable sell stays flat.** When the exchange gross base is below the minimum and nothing is staked, the restored book is written off instead of being retried forever. A clamped sell keeps only coins the exchange still has. An unknown order side does not restore the book first.
- **Coordinator.** Rule 2 boosts confidence and does not force a buy. A halt flatten is not overwritten. A cross-pair sell is allowed through an uptrend ride. A halted engine publishes the real regime. Swap legs are scored by the rules, and a paper swap restores the sell if the buy does not place.
- **Replay.** The backtest applies the portfolio halt, the UTC session weight, coordinator prices, and swap legs. It does not invent a derivatives feed, an order book, or a brain.
- **Indicators.** A tuned RSI upper band of 85 or more still exits at RSI 100. MACD keeps its first valid bar. Volume confirmation uses the bars before the one being judged. A non-finite basis is missing, not a live value. An unknown base is not sized or written off at 0.02.

---

## [2.34.1] — 2026-09-26

### Fixed

- **Heartbeat scores the first print.** The robust scaler used to return no z-score until 30 closed bars, and an OHLC bootstrap has no aggressor volume, so live order-flow features stayed blank while the dashboard hid them. The first print is now the score. Robust scaling starts once a scale exists.
- **A sub-lot Kelly buy still runs the guardrails.** Conviction sizing can lift that crumb into a real order. Skipping R1–R11 because `calculate()` returned 0 let that order through extreme funding and a stale derivatives feed.
- **A circuit-breaker flatten is not turned into a hold** when the derivatives feed is stale, and a brain override cannot replace it. A stale squeeze print no longer blocks the profit-exit escape. A derivatives feed that has never updated is recorded as stale instead of age-unknown.
- **An older candle is dropped** instead of becoming the latest price. A timestamp of 0 stays epoch 0, so a following historical bar is not treated as late.
- **The 50-price hold is gone.** Signals and the regime use each indicator as soon as that indicator has the closes it needs. The dashboard draws the candle chart from the first bar and shows RSI instead of "warming up indicators". A collapsed Bollinger band is not treated as a touch.

---

## [2.34.0] — 2026-09-27

### Changed

- **Python floor is 3.11.** The 3.10 engine job is gone from CI and from branch protection. `websockets` is `>=17.1`. Heartbeat and s3bounce declare the same floor.
- **Anthropic SDK `>=1.6,<2`.** Companion Anthropic calls omit temperature. Opus 5.5 rejects it. The xAI path still sends temperature.
- **Dashboard eslint 10.11.0.**
- **Models.** Quant, risk manager, and Athena's deep intents use `claude-opus-5-5` at effort `max`. The strategist and the trading companion routes use `grok-4.7` at `reasoning_effort` `xhigh`. Greetings and banter stay on `grok-4.3`. Output budgets floor at 8192 tokens so thinking does not eat the answer. The companion panel waits 120 seconds.
- **Brain budget is $50/day**, both the disclosure line and the hard stop. Companion budgets stay separate.
- **BUY size covers the decision.** Expected dollar profit is the position cap times the strategy's expected move. If that profit does not cover the API cost of the deliberation, the buy is skipped. If it covers part of the cost, size is scaled by the share that remains. Sells are not scaled. A missing expected move fails open, the same way the friction gate does.

---

## [2.33.3] — 2026-09-26

### Added

- **Watermark Gate CI job** (`watermark-gate`): deterministic check of
  PR diffs vs `main` for `Co-Authored-By` / Generated-with banners and
  actionable invisible Unicode. Blocks merge. Script:
  `scripts/watermark_gate.py`. Not a Layer B rewrite and not an
  official-detector claim.

### Fixed

- **Stale agent docs.** CLAUDE.md now indexes 8 unlisted modules, 7
  undocumented env vars, and 3 state files, and drops rules it stated
  twice. The `/audit` skill cited CLAUDE.md sections that do not exist
  (§Backtesting/§Naming/§Testing) — now points at `docs/BACKTEST_SPEC.md`
  and the pytest suite. Companion comments claimed default-off behind
  `HYDRA_COMPANION_ENABLED`, which nothing reads; the subsystem is
  default-on with the `HYDRA_COMPANION_DISABLED` kill switch.
- **Resume re-split cash across an open book.** `--resume` wrote `free/N` onto engines that already had a position or a `PLACED` order and kept the optimistic coins. Free quote excludes the resting buy's hold, so that cash could not fund the coins, and a later `restore_position` put one engine's pre-split cash back. Those books keep their restored cash. Flat engines with no working order are still seeded from the free pool that is not already on a locked book.
- **A fully locked free balance sized against gross.** `free_balance()` and `BalanceStream` dropped a free amount of 0, so an empty free map looked like a missing feed and the next buy used gross. A successful read with free 0 (asset present at 0, or an empty map because every unit is on hold) is $0 spendable. Equity still uses gross. Fail open to gross only when the free read failed.
- **Same-candle size stack.** After the brain ran, later ticks of that candle multiplied the rule penalties again, so a 0.7 basis cut became 0.49, then 0.34, and could fall through the exchange minimum. The re-score now multiplies the stored brain factor. A non-finite multiplier is neutral, not an upsize.
- **Conviction sizing on a small book.** The 40% overlay size never ran when the Kelly crumb was below the exchange minimum, so `calculate()` returned 0 and the entry was dropped. A zero risk multiplier is still a veto. A non-positive or non-finite candle is ignored, and a SELL at a non-positive price no longer writes the position off for no cash.
- **One working order per pair.** A second signal while a post-only order was still `PLACED` booked another optimistic fill. The first order's true-up then restored the older snapshot and left the second order live against a book that no longer matched it, so a flatten tried to sell coins the entry had not bought. Same-side signals now wait. An opposite signal cancels the resting order and places on a later tick.
- **Fill true-up survived restart.** A confirmed fill rewrote the journal row in place, so the snapshot cadence (new rows, or every 120 ticks) could reload the pre-fill engine book under `--resume`. The rolling journal is written after the execution-stream drain, and any book change saves the snapshot on that tick.
- **Dashboard BUY halt.** The portfolio breaker stays on after drawdown recovers under 15%. The banner now follows the sticky `buy_halted` flag, and a resting order is kept in the journal strip when later failures would have pushed it out of the last 20 rows.
- **Same-tick circuit-breaker flatten.** The tick that arms the 15% breaker returns a `HALT FLATTEN` sell when inventory is open, instead of waiting for the next tick. A flat book arms and does not sell.
- **Tuner learns the confirmed fill.** An all-loss update clears its observation window. `min_confidence_threshold` cannot be tuned below 0.65. Win/loss is recorded when the sell fill is confirmed, net of the fee, and a cancel does not leave a placement-time observation.
- **Backtest end-of-tape books.** An order placed on the final bar is restored and counted as a reject, and that pair's last equity point matches the restored book. Closed-trade profit nets both the buy fee and the sell fee without debiting cash twice. Lab walk-forward still warms up on the pad, and the reported fold metrics cover only the out-of-sample window.
- **Dashboard state matches the book.** The pair card shows current drawdown and a separate max. Paper and demo do not claim the dead-man switch is on. An untradable pair says entries are blocked and exits are allowed. `exit_only` is broadcast and shown. Live frames are applied after `auth_ack`.
- **Companion live rejects.** With the live flag on, a BUY is refused when the engine is `exit_only` or not tradable, and a SELL larger than `position.size` is refused. The flag stays off by default. The card stop is still not an order.
- **kraken-cli v0.4.1 `ohlc` object-array** was parsed as empty, so warmup
  left engines with one forming bar and the dashboard hid every chart.
- **`--pairs auto` cores stayed on unfunded USD** while the book held
  USDC — engines printed `$0` equity. Cores now follow the funded
  stable; `pairs --pair A,B,C` retries per-pair when one name is unlisted
  (ZEC/USDC).
- **EXEC/BALANCE streams restarted every 300s tick.** v0.4.1 swallows
  WS heartbeats on stdout; private streams are healthy after snapshot
  if the process and reader are alive.
- Heartbeat RESEARCH row: USDC engines read the calibrated USD tape;
  `start_heartbeat.bat` launches BTC/ETH `heartbeat run`.
  `start_hydra.bat` starts it once before the watchdog loop.
- `--resume` after a funded-stable core switch (BTC/USD snap,
  BTC/USDC engines, `triangle=None`) restores same-base engine
  state instead of dropping positions/peak/halt. Mixed leftover
  quotes (ZEC/USD) stay exact — no global rewrite to ZEC/USDC.
- S3 shadow maps BTC/USDC → BTC/USD universe so USDC cores are
  not silently inert. Tainted heartbeat status does not fall
  through to the USD tape.
- Private-stream `start()` no longer clears `_got_data` after the
  reader thread has begun (snapshot-vs-reset race).
- Login: `python hydra_auth.py reset-password <user>` (`create-user` is insert-only). Usernames trim and match case-insensitively.
- `RULES_ONLY` ticks no longer `KeyError: final_signal`.
- Demo Kraken Account empty copy (not forever "Loading...").
- Fee key `XZECZUSD` → `ZEC/USD` (`XZEC` alias).
- Unfunded USD cash: constructor `--balance` is not a real peak (no false 100% DD / BUY halt). Snapshot peaks stay.
- Filter kraken-cli subscribe ACK deprecations on stream stderr.
- Launchers exit if :8765 is already listening.

---

## [2.33.2] — 2026-08-14

Audit patch. Accounting, rails, dashboard, and docs. No new live gates
(S3 / heartbeat stay display/shadow).

### Fixed

- **Portfolio equity in gapped backtests was index-zipped.** After
  timestamp-align in `_loop`, a pair without a bar did not record
  equity, so `_finalize_metrics` summed pair A at time T with pair B
  at T+n. Every pair now gets a frontier point (forward-fill last
  mark). Flywheel validation reads this curve.
- **Resume applied the fill fee then `true_up_fill`.** Restore wiped
  the debit; live P&L was ~16 bps/fill rich vs backtest. True-up
  first, then fee.
- **`bool("false")` was True** on Quant `force_hold`. A string false
  from the LLM halted the pair. `_coerce_bool` treats only
  true/1/yes as true.
- **QFE / HALT FLATTEN SELLs died in ride-trend.** The agent rewrites
  the reason; hold-through only allowed `"extreme overbought"`. Those
  prefixes now pass.
- **Same-candle brain cache skipped R1–R11.** Intra-bar ticks reused
  the LLM decision and never re-ran rules. Cache still skips the LLM;
  `_apply_quant_guardrails` still runs.
- **ExecutionStream `ensure_healthy` lived in the try body.** A tick
  crash skipped exec restart and fill drain. Both now run in
  `finally` with the market streams.
- **`write_off_dust` dropped accumulated `realized_pnl`** instead of
  booking the close like a full SELL.
- **CVD slope window dropped the latest bar** (`range` exclusive of
  `len`). Inclusive of the current candle.
- **`tradable=False` SELL test asserted the pre–PR-A contract**
  (block flatten). It now requires flatten; hold-through off.
- **Lab `research_lab_result` cleared progress**, so the pane never
  saw `done`/`error` and stayed on Running. Result is terminal.
- **Dashboard WS default `localhost` misses IPv6 `::1`** on Windows
  when the agent binds `127.0.0.1`. Default is now `ws://127.0.0.1:8765`.
- **Auth handshake / sends omitted `jwt`.** Production WS reads `jwt`.
  Handshake and `sendMessage` now send both `auth` and `jwt`.
- **Circuit-breaker halt was invisible** in the sidebar. Banner when
  a pair is `halted` or portfolio DD ≥ 15%.
- **`--quote` / `HYDRA_QUOTE` were advertised but unused.**
  `add_config_args` now drives `--pairs auto` fallback quote.
- **USDT (and ETH/USDC, ZEC/USDC) missing from the fallback catalog**,
  so `HydraConfig.from_quote("USDT")` raised. Catalog complete.
- Docs: `metrics.sharpe` (not `sharpe_ratio`), BTC ordermin 0.0001,
  kraken-cli pin v0.4.1, launcher `--pairs auto`, journal_maintenance
  and `--quote` semantics.

### Not done (evidence / scope)

S3 and heartbeat stay off the live BUY/SELL path (`HONEST_FINDINGS`).
Companion live-exec rails, same-tick CB flatten, and Lab worker-pool
cap remain follow-ups.

---

## [2.33.1] — 2026-08-08

Launcher fix. Production was starting on the pair set v2.29 retired.

### Fixed

- **`start_hydra.bat` and `start_hydra_companion.bat` hardcoded the legacy
  `--pairs SOL/USD,SOL/BTC,BTC/USD` triangle.** An explicit `--pairs`
  overrides the code default and does not drift with it, so when v2.29 moved
  the default to the three independent cores the launchers kept production on
  the retired triangle through every release since. The live agent therefore
  traded a pair set the evidence ledger had already rejected — 90d real tape
  found no SOL edge (`calibrate_SOL_USD.txt`, AUC 0.56 FAIL) and the SOL/BTC
  bridge only ever runs `exit_only` drain (`bridge_isolation.json`, 0
  trades/1y) — while **never trading ETH or ZEC at all**. Both launchers now
  pass `--pairs auto`, which seeds BTC/USD + ETH/USD + ZEC/USD and adds one
  satellite per additional held asset, so held SOL is still worked as an
  ordinary satellite rather than a default core with a dead bridge.
  `--mode competition --resume` unchanged.

  Verified against the real account: `discover_portfolio_pairs("USD")`
  resolves to `['BTC/USD', 'ETH/USD', 'ZEC/USD']`.

  Root launchers, `scripts/` and `.github/workflows/` sit in no audit
  partition and no test asserts `.bat` content, which is why three
  consecutive audit releases did not see it. CLAUDE.md now records that a
  default-pair change must be re-checked against these two files.

---

## [2.33.0] — 2026-08-08

Audit release. A 7-way parallel sweep found a **silent stop-trading
regression shipped in v2.32.0**, an unauthenticated account-state read on the
dashboard socket, a credential left in process argv, and four ways the
exchange could strand or mis-price real inventory. kraken-cli re-verified
against v0.4.1.

### Fixed — the agent placed no orders at all without an LLM key

- **Actionable signals were dropped before execution when `brain is None`.**
  v2.32.0 added the brain-free guardrail branch to the Phase 2 loop as an
  `elif` above the `else` that owned `all_states[pair] = state`. A BUY or SELL
  with no LLM configured therefore ran R1-R11 + QFE, logged normally — and was
  then absent from `all_states`, so Phase 2.5 (`state = all_states.get(pair);
  if not state: continue`) skipped the pair entirely. **No orders were placed,
  not entries and not exits**: a losing position rode through every
  hold-through flatten and every circuit-breaker flatten while the console
  showed a healthy agent generating signals. The assignment now sits outside
  the branch, where it was before v2.32.0. Regression test drives the real
  `run()` loop with `brain = None` and asserts `execute_signal` is reached;
  re-nesting the assignment fails it.

### Fixed — exchange inventory could be stranded or mis-priced

- **A non-tradable engine could not SELL.** `_maybe_execute` refused
  everything when `tradable=False`, but that flag is derived from the QUOTE
  balance and a SELL spends the BASE already held. The bridge in `exit_only`
  drain mode, or any satellite whose quote pool emptied while holding the
  base, could never flatten — and the engine breaker is suppressed while not
  tradable, so nothing else forced the exit. SELL is now allowed whenever
  `position.size > 0`; entries stay refused.
- **The 15% circuit breaker armed off a monotone high-water mark.** Both the
  engine and the portfolio halt compared `max_drawdown` — a running max
  nothing ever lowers — against the threshold, so `HYDRA_RESET_CIRCUIT_BREAKER=1`
  cleared the flag and the very next tick re-armed it, even from a fully
  recovered account back at its old peak. The one documented escape hatch was
  a no-op and new BUYs were dead for good. Both now arm on the CURRENT
  drawdown; the running max is preserved purely as the record, and the halt
  stays sticky within a session.
- **Resume threw away the data needed to true up a fill.**
  `_reconcile_stale_placed` passed `pre_trade_snapshot=None` and never called
  `true_up_fill` or `restore_position`, on the stated grounds that
  previous-session orders have no snapshot. They do — `_place_order` writes it
  onto the journal entry and `_journal_for_persistence` does not strip it. A
  resting BUY that filled while offline left the engine on its optimistic
  candle-close entry price; one cancelled by the dead man's switch left a
  **phantom position never bought**, so the next SELL sized against inventory
  that did not exist. Now mirrors `_apply_execution_event`.
- **A renamed `kraken pairs` key would have silently erased positions.**
  `load_pair_constants` substituted generic literals (`ordermin` 0.02,
  `pair_decimals` 8) for missing keys and `apply_pair_constants` overlaid them
  onto the registry, replacing correct per-pair values with wrong ones.
  At ordermin 0.02, `write_off_dust` silently zeroes any BTC position under
  ~0.02 BTC. Missing or unparseable constants now keep the registry's values
  and log loudly.

### Fixed — security

- **The dashboard socket served account state with no credential.** Binding to
  loopback (v2.32.1) narrowed who could reach :8765 but the OUTBOUND path
  still had no auth: `_handler` added every socket to `clients` and
  immediately pushed `latest_state` — balance, per-asset holdings, portfolio
  drawdown, open positions, the last 20 journal entries. The token gated
  inbound commands only, and `_origin_allowed` returns True for a missing
  Origin, so any local process (any host under `HYDRA_WS_HOST=0.0.0.0`) could
  read the whole account by connecting and sending nothing. Sockets now wait
  in `pending`, receive nothing, and are closed after
  `HYDRA_WS_AUTH_GRACE_S` (default 10s) unless they complete an `auth`
  handshake. `_origin_allowed` is now documented as CSRF defence only.
- **The API secret sat in `wsl` process argv for the whole session.**
  v2.15.0 moved credentials to WSLENV for REST calls but
  `hydra_streams.BaseStream.start` kept the inline `export KRAKEN_API_SECRET=`
  form for all five long-lived stream subprocesses — a strictly longer
  disclosure window than the path that was fixed. Both now share
  `KrakenCLI.forward_credentials`; `HYDRA_CLI_LEGACY_SECRET_EXPORT=1` reverts.
- **The dashboard login screen opened a socket before authenticating** and
  never closed it on error, and its `finally` cleared `loading` synchronously
  so the submit button was never disabled. Socket lifecycle is now ref-held
  and closed on every terminal path.

### Fixed — companion money safety

- **A confirmed companion BUY bypassed the portfolio circuit breaker.**
  `_system_healthy` checked only per-engine `halted`; the portfolio halt arms
  off summed equity and fires while every individual engine is still under its
  own threshold. SELL remains allowed (PR-A).
- **The Kraken minimum-size check was dead code.** It read
  `agent.kraken_cli` (an attribute `HydraAgent` never assigns) for
  `MIN_ORDER_SIZE`/`MIN_COST`, which live on `PositionSizer`, so both lookups
  yielded `{}` and every undersized proposal passed. Now reads
  `hydra_pair_registry`.
- **Risk % was not a size cap.** `|limit - stop| * size` is small whenever the
  stop is tight, so 0.7 SOL @ 141 with a 2-wide stop read as 1.4% risk while
  being 98.7% of a 100-equity account. Gross notional is now bounded by the
  engine's own `max_position_pct`. The risk cap also **fails closed** when
  equity is unavailable (it was skipped entirely at boot, when the least is
  known), and proposals for pairs the agent does not run are rejected — an
  unknown pair previously also disabled the price-band check.

### Changed

- **kraken-cli pin v0.3.2 → v0.4.1**, re-verified per subcommand: every flag
  Hydra passes (`--type/--price/--oflags/--userref/--validate/--yes`,
  `cancel`, `cancel-all`, `cancel-after`, `pairs`, `ticker`, `ohlc`,
  `balance`, `extended-balance`, `trades-history`, `volume`, `query-orders`)
  still exists and is unchanged. v0.4.1 adds `order amend`, batch orders,
  `--cl-ord-id`, `--reduce-only` and the `workspace`/`session`/`tape`/`lab`
  families; Hydra deliberately adopts none of them.
- `balance()` skips unparseable entries instead of raising. The three callers
  have no try/except, so a shape change previously killed `main()` before the
  trading loop and `start_hydra.bat` restarted into the same crash forever.

### Documentation

Docs audited for truthfulness; claims that no longer matched the code were
corrected rather than the code bent to fit them. Notably:
`model_routing.json` advertised `max_concurrent_risk_pct_equity` and
`volatile_regime_policy` as "enforced in code" — no code reads either key;
`COMPANION_SPEC` referenced a `_execute_companion_proposal` that does not
exist and claimed the compiler renders `beliefs`/`past_selves` (it reads them
into unused locals); `BACKTEST.md` claimed all twelve invariants are enforced
when I8/I9 are moot; harness scenario counts and several file paths were
stale.

---

## [2.32.1] — 2026-08-06

Audit backlog closeout. Research-layer correctness, an unauthenticated data
exposure, two dashboard defects, and four tests that could not fail.

### Fixed — security

- **Dashboard WS bound `0.0.0.0` and served account state with no credential.**
  Inbound *commands* require the per-process token, but the OUTBOUND path never
  did: `_handler` adds the socket to `self.clients` and immediately sends
  `latest_state`, which carries `balance`, `balance_usd.assets` (full per-asset
  holdings), portfolio drawdown, every open position and the last 20
  order-journal entries. `_origin_allowed` returns True for a missing Origin
  header, so any host that could reach :8765 got the whole account. Now binds
  `127.0.0.1` by default; `HYDRA_WS_HOST=0.0.0.0` re-exposes it deliberately.

### Fixed — research layer

- **Monte Carlo inflated Sharpe by ~93x.** `monte_carlo_resample` annualized
  with `annualization_factor(candle_interval_min)` — sqrt(BARS per year), 93.6
  on 60m bars — applied to one return per TRADE. A mundane 42-trade series
  reported a Sharpe CI of ~[25.4, 31.5]. `docs/BACKTEST_SPEC.md` makes
  `mc_ci_lower_positive` one of seven rigor gates for auto-apply eligibility,
  and at that inflation the gate could never bind. Now takes an optional
  `trades_per_year` and reports an honest UNANNUALIZED per-trade Sharpe when
  it is absent (same series: [0.36, 0.43]). The default
  `candle_interval_min` also moved 15 → 60 to match the repo default.
- **Multi-pair backtests zipped candles by INDEX, not timestamp.** `_loop`
  called `next()` on every pair's iterator each tick. `SqliteSource` yields
  only rows that exist and `HistoryStore.coverage` tracks `gap_count`
  precisely because gaps are real, so the first missing bar in one pair skewed
  every later bar of that pair against the others, and the skew accumulated.
  The coordinator then compared one pair's regime at time T against another's
  at T−n, and `_finalize_metrics` summed per-pair equity curves standing at
  different instants — which is what `tools/flywheel_validation.py` writes as
  the evidence gate `engine_sleeve_allowed()` reads. Now advances only the
  pairs sitting at the earliest timestamp; a pair with no bar at that
  timestamp is simply absent for that tick. Verified on a deliberately gapped
  series: **zero** lookahead events, gap preserved rather than papered over.
- **`sweep_param` ran synchronously on the live tick thread.**
  `_tool_run_backtest` was explicitly fixed for this ("I1: never block the live
  tick"); the sweep was not. It is reachable from the brain's tool-use loop,
  and the schema permits `n_candles` 20000 × 10 values — ~200,000 engine ticks
  executed inline with real positions open, during which the agent does not
  tick, does not reconcile fills, and does not honour its shutdown path. Now
  routed through `BacktestWorkerPool` when mounted.

### Fixed — operator-facing

- **Rolling order journal was capped at 200, corrupting realized P&L.**
  `_journal_for_persistence` applied one 200-entry cap to BOTH write paths,
  but `_merge_order_journal` documents the rolling file as "the authoritative
  long-horizon record" and `ORDER_JOURNAL_CAP` is 2000 in memory. A session
  with 800 fills kept only the last 200 on disk; after a restart
  `_compute_pair_realized_pnl` walked a truncated journal, valuing SELLs
  against a missing cost basis. Now parameterized: snapshot keeps 200, the
  rolling file keeps everything.
- **No paper/live indicator.** A paper session was pixel-identical to a live
  one — and in paper mode `balance_usd` is still the REAL Kraken account, so
  "Total Balance" showed real money beside simulated P&L. Backend now
  broadcasts `paper`/`demo`; the UI renders a PAPER/DEMO pill and the footer
  no longer claims "Real money at risk" in those modes.
- **"Total Balance $0.00" while pairs held equity.** The engine-equity
  fallback was unreachable: the backend always emits `balance_usd` and emits a
  literal `0` (not null) when the balance is unknown, so `!= null` always took
  the first branch. Affected the balance card and the history sparkline.
- **Research Lab pane hammered the agent.** `clearLabRunState` was an inline
  arrow, so a new identity every render; LabPane's effect depends on it, closing a
  self-sustaining loop (effect → `research_params_current` → ack → state
  change → re-render → new identity → effect). Each iteration ran a
  synchronous `hydra_params_<pair>.json` disk read on the asyncio loop thread
  that also serves every broadcast, and wiped the operator's dragged params.
  Now `useCallback([])`.

### Fixed — test integrity

- **Two tuner tests asserted nothing.** `test_params_at_entry_stored_on_buy`
  and `..._cleared_on_full_sell` wrapped their assertions in `if trade:` /
  `if engine.position.size > 0:`, and since the v2.27 friction gate landed the
  synthetic tape never cleared the hurdle — so the bodies never ran and
  `params_at_entry` was completely untested. Assertions are now unconditional
  with the friction gate explicitly disabled. **Mutation-tested:** sabotaging
  `params_at_entry` now fails the test; before, it passed.
- **The quant kill-switch test was tautological.** It re-implemented the gate
  inside the test and asserted on its own simulation (`called == []` was
  unfalsifiable); `hydra_agent` was never imported, so deleting the entire
  kill-switch branch left it green. Now spies on the real
  `hydra_quant_rules.apply_rules` and drives the real
  `_apply_quant_guardrails`, **with a positive control** asserting the rules DO
  run and DO force_hold when the switch is unset.
- **`SkipTest` diverged between runners.** `tests/test_engine.py` defined its
  own `class SkipTest(Exception)`, which its `run_tests()` runner reports as
  SKIP but pytest treats as a hard FAILURE — so the two runners disagreed
  about the same tree whenever the optional anthropic/openai SDKs resolved
  differently. Now aliases `pytest.skip.Exception` when pytest is importable.
- **The harness could leave live state renamed away.** `isolate_environment()`
  renames the operator's real `hydra_session_snapshot.json` /
  `hydra_order_journal.json` to `*.harness_stash`, but the mock/validate/live
  path had no `try/finally` — an exception out of the `scenarios` import or a
  Ctrl-C during `--mode validate` skipped the restore, and a later `--resume`
  would start from nothing. Now `try/finally` + `atexit`, and
  `restore_environment` was made **idempotent** (it is now called twice; a
  second pass over the old non-empty list would have deleted the
  just-restored file).
- **Bare `pytest` failed on every file.** No `conftest.py` existed, so only
  `python -m pytest` worked (it injects CWD into `sys.path`). Added one at the
  repo root.

### Removed

- `.claude/DEPENDABOT_REMEDIATION_STATE.md` — working file for the v2.32
  Dependabot sweep; all 10 PRs are resolved, so it has served its purpose.

Verified: `python -m pytest tests/` **1451 passed, 0 failed**; harness
`--mode mock` **36/36**, `--mode smoke` exit 0; `npm ci` + build + lint exit 0;
`check_release_alignment.py` exit 0 at 2.32.1.

---

## [2.32.0] — 2026-08-05

Resilience release. Five defects that each ended with the agent quietly not
trading — or trading on corrupted numbers — while looking healthy in the log.
All were found by the 7-way audit sweep and verified before fixing.

### Fixed — the agent silently stops working

- **A dead CandleStream permanently wedged the tick loop.**
  `_fetch_and_tick` returns `None` in live mode when the candle stream is
  unhealthy, and the caller stores that `None` under the pair key.
  `_build_portfolio_review_state` read it with `engine_states.get(pair, {})` —
  which does NOT apply its default, because the key exists — and raised
  `AttributeError` on the next `.get()`. That was not a one-tick blip: the
  crash fired *before* the stream `ensure_healthy()` block later in the same
  try body, so the dead stream was never restarted and every subsequent tick
  died at the same line. No signals, no placements, and **no exits** — not even
  circuit-breaker flattens. Fixed with `or {}`, and stream recovery moved into
  the tick's `finally` so it can never again be gated on the rest of the tick
  succeeding (each `ensure_healthy()` individually wrapped so one stream's
  failure cannot block the others).
- **The 15% circuit breaker was a permanent one-way ratchet.** `halted`
  restores from the snapshot and nothing outside `__init__` ever cleared it;
  `_portfolio_max_drawdown_pct` is a running max, so the portfolio halt
  re-armed on every future resume. Production runs `--mode competition
  --resume`, so a single breach disabled all new BUYs across every future
  session forever, with no log line explaining why — SELL kept working, so the
  bot looked alive while it had quietly stopped entering. CLAUDE.md described
  this as "for session". Both halts now print a RESUMED-STILL-HALTED banner at
  boot and are cleared by an explicit `HYDRA_RESET_CIRCUIT_BREAKER=1`, which
  clears the flag only — the drawdown record is preserved so the breaker
  re-arms immediately if still underwater. Deliberately NOT auto-cleared:
  that would silently re-enable risk after a breach.

### Fixed — corrupted execution accounting

- **ExecutionStream double-folded replayed fills after a subprocess restart.**
  The stream runs with `--snap-trades true` and `_on_start_reset` deliberately
  preserves `_known_orders` so in-flight orders can still finalize. Those two
  correct behaviours combined into double-counting: a 0.4 fill replayed after a
  restart pushed `vol_exec_running` to 0.8, so a fully-filled 1.0 order
  reported 1.4 — classified `PARTIALLY_FILLED` instead of `FILLED` (tolerance
  is 1e-6), booked more inventory than was bought via
  `reconcile_partial_fill`, produced a garbage cost-weighted `avg_fill_price`,
  and debited roughly double the real fee from engine cash. Now deduped on
  `exec_id` (Kraken's per-execution primary key), with a clamp to
  `placed_amount` for entries that carry no `exec_id`.
- **Unsellable dust looped forever and stranded inventory.** When the exchange
  balance for a base asset fell below `ordermin`, the SELL preflight journalled
  a failure and rolled the engine back — leaving the engine still believing it
  held a sellable position, so every subsequent tick rebuilt the entry,
  re-failed, and appended another `PLACEMENT_FAILED`, indefinitely. PR-C's
  `write_off_dust()` never fired here because it keys off the *engine's* book,
  not the engine-vs-exchange divergence that actually detects this. Now written
  off so the engine returns to flat. **Staked holdings are excluded**:
  `_get_real_quote_balance` skips staked assets by design, so a fully-staked
  position reads 0.0 and would otherwise have been written off as dust —
  destroying a real, recoverable position from our books. New
  `_get_staked_balance` tells the two apart and surfaces the staked case
  instead.

### Fixed — guardrails that were not guarding

- **R1-R11 and QFE did not run at all without an LLM key.** `hydra_quant_rules`
  describes itself as "the guardrails the LLMs cannot talk around", but every
  call to `apply_rules` / `evaluate_qfe` lived inside `_apply_brain`, which
  returns immediately when `self.brain is None`. So on any deployment with an
  unset or expired `ANTHROPIC_API_KEY` — or a `HydraBrain` constructor that
  raised and was swallowed — the entire deterministic stack was inert while the
  DerivativesStream kept feeding live funding/OI/CVD to the dashboard. Funding
  could sit at +140 bps with R1 never firing; the feed could go dark entirely
  and R10's blackout never fire. Nothing printed to say so. Added
  `_apply_quant_guardrails`, a brain-free path called from the tick loop for
  any actionable signal when no brain is configured. Deliberately additive —
  the brain-present path is untouched. `positioning_bias` is `""`, so R8's
  contrarian boost cannot fire (the same degradation already documented for the
  fallback path), and `HYDRA_QUANT_INDICATORS_DISABLED=1` still skips it. Boot
  now prints which guardrail layer is live.

### Tests

`tests/test_guardrails_resilience.py` — 15 cases, each paired with a negative
control where one exists (benign funding must still let a BUY through; only the
exact `=1` opt-in clears the halt; distinct `exec_id`s must still accumulate).
The audit that produced these fixes also found tests that could not fail, so
these were written to be falsifiable.

Verified: `pytest tests/` **1450 passed, 0 failed**; `harness --mode mock`
**36/36**; dashboard build + lint exit 0.

---

## [Unreleased]

kraken-cli contract re-alignment plus the money-path fixes found by the
7-way audit sweep. No version bump: the release cycle (`/release`) owns
the version-site update.

### kraken-cli upstream re-verification

Checked `krakenfx/kraken-cli` on 2026-08-05: **v0.3.2 (2026-04-20) is
still the latest tagged release and `aa32814` is still branch HEAD** — the
same commit Hydra was already pinned to. There is no newer CLI to adopt.
What *had* drifted was Hydra's use of the CLI's documented contract
(`agents/tool-catalog.json`, `AGENTS.md`, `CONTEXT.md`):

- **Fixed — every paper order failed at the CLI boundary.**
  `KrakenCLI.paper_buy/paper_sell` sent `--volume <V>`, but upstream takes
  VOLUME **positionally** (`kraken paper buy <PAIR> <VOLUME>`); `--volume`
  is not a flag on that subcommand. `--type limit` was also sent with no
  `--price`, which upstream rejects. Volume is now positional, the limit
  price is threaded through from `_place_paper_order`, and a limit with no
  usable price degrades to `market` rather than emitting a
  guaranteed-invalid command. Invisible to tests because the entire suite
  (and the live harness) stubs `KrakenCLI._run`, i.e. above argv
  construction — `tests/test_kraken_cli.py::TestPaperArgv` now asserts on
  the argv itself.
- **Errors are category slugs, not sentences.** Upstream returns
  `{"error": "<category>", "message": ...}` with an optional `retryable`;
  CONTEXT.md: *"Route handling decisions based on the `error` field rather
  than message text."* Hydra wrote its own human strings into that same key,
  colliding with the slug namespace, and callers printed the bare slug —
  hence log lines like `PLACEMENT_FAILED: validation` with the actionable
  detail discarded. `_normalize_error` now adds `error_category` /
  `error_message` / `retryable` alongside an unchanged `error` (byte-compatible
  for existing `"error" in result` call sites), and `describe_error()`
  renders `category: message (suggestion)`.
- **Bounded retry for read-only calls.** AGENTS.md: *"The CLI does not
  pre-throttle or retry rate-limited requests"* — an unretried transient
  blanked an indicator for a whole tick. `_run` now retries on a retryable
  failure, **read-only commands only** (`_is_retry_safe`). Order placement,
  cancellation and paper trades are never retried: an ambiguous timeout can
  double-place and the spot path carries no idempotency key. The 2s REST
  floor still gates every attempt. This also confirms the 2s floor is
  load-bearing — Hydra is the only throttle in the chain.
- **Secrets out of the process argv.** `KRAKEN_API_KEY`/`KRAKEN_API_SECRET`
  were interpolated into the `bash -c` string, putting the live trading
  secret in the `wsl` argv where any local user could read it from `ps` /
  procfs. `shlex.quote` stopped injection but never disclosure. They now
  travel in the child's environment, forwarded by `WSLENV`. Revert with
  `HYDRA_CLI_LEGACY_SECRET_EXPORT=1`.
- **Adopted `kraken extended-balance`** for a hold-netted balance view (see
  below).

### Fixed — money path

- **Conviction sizing silently nullified every de-risking decision.**
  `_maybe_execute` applied `effective_mult` and then did
  `size = max(size, conviction_value / price)` where `conviction_value` was
  derived from `balance` alone. Whenever the daily ensemble was long — the
  default live path, `HYDRA_TREND_OVERLAY` and
  `HYDRA_TREND_CONVICTION_SIZING` both default ON — an RM `ADJUST,
  size_multiplier=0.3` and a clean `1.0` produced **byte-identical
  notional**; the R3/R5/R7 penalty stack was equally inert. Only an exact
  `0.0` survived, via the `size > 0` guard. The floor is now scaled by
  `effective_mult`, restoring PR-B.
- **Gross balance double-counted funds held in resting orders.** Kraken's
  `balance` includes quote currency locked behind our own unfilled post-only
  orders, so the sizer re-committed money an order already owned and the
  exchange rejected the follow-up — the `PLACEMENT_FAILED:
  insufficient_<quote>_balance` loop. `BalanceStream` now tracks gross and
  free separately (`latest_free_balances()`), `KrakenCLI.free_balance()`
  reads `extended-balance`, and `_get_real_quote_balance` — the spendability
  chokepoint — prefers the free view. Equity, peak equity and the drawdown
  breaker deliberately stay on GROSS. Fails open to gross on any
  unrecognized payload shape.
- **Post-OVERRIDE rule re-apply dropped the recomputed size multiplier.**
  When the brain OVERRODE the engine's direction, the rules were correctly
  re-scored for the new side but only `force_hold` was consumed —
  `rr2.size_multiplier` was discarded and the **pre-override** multiplier
  executed. An engine SELL overridden to BUY under a short squeeze in
  euphoric contango ran at R8's contrarian ×1.15 instead of R3×R5's ×0.35:
  3.3× the mandated size, into exactly the setup the rules guard.
- **Gross-inventory cap could emit a sub-ordermin BUY.** `PositionSizer`
  enforces ordermin/costmin *before* the `max_position_pct` cap shrinks the
  size, so an engine sitting at the cap emitted dust orders (~7e-08 BTC /
  $0.007) that the agent preflight passes (it checks the balance, not the
  order size) and Kraken rejects — re-fired every tick. Floors re-asserted
  after the cap.
- **`write_off_dust()` leaked `realized_pnl` into the next position**, which
  then booked that stale profit on a flat close — inflating
  `win_count`/`gross_profit` and feeding `ParameterTracker` a fabricated win
  to learn from.
- **`get_performance_report()` divided by zero** when `initial_balance == 0`
  (signal-only / informational engines). `_print_final_report` loops every
  pair *outside* the tick try/except, so one such engine aborted the whole
  shutdown report.
- **A no-op tuner update destroyed the rollback point.** `_param_history` is
  `deque(maxlen=1)`; the previous snapshot was appended (evicting the real
  one) before the code discovered nothing would be applied, then popped —
  leaving the deque empty and `rollback_to_previous()` returning False.

### Fixed — CI / test integrity

- **A CI step ran zero tests and was permanently green.**
  `tests/test_companion_mode_scrub.py` has no `__main__` runner, so
  `python tests/test_companion_mode_scrub.py` imported the module and exited
  0. 26 tests — including the soul-prompt mode-label safeguards — had never
  run. Now invoked via pytest.
- **CI enumerated test files by hand**, so 4 files never ran — including the
  grep-guards enforcing *"research surfaces never place orders"*
  (`test_s3_shadow.py`, `test_heartbeat_surface.py`). Added a catch-all
  `python -m pytest tests/ -q` step so new test files are covered by default.
- **`pyyaml` added to `requirements.txt`** — `heartbeat/src/heartbeat/config.py`
  imports it, so the catch-all step would have gone red immediately.
- **The mandatory mock harness exited 0 when zero scenarios ran.** Emptying
  `ALL_SCENARIOS`, mistagging modes or breaking the scenario import turned
  the gate permanently green. It now exits 2 on an empty run, and on fewer
  than 30 unfiltered mock scenarios.
- **`.claude/settings.json` pointed the post-edit hook at an absolute
  Windows path** (`C:/Users/.../post-edit.py`), so the hook failed for every
  other machine and in CI. Now `$CLAUDE_PROJECT_DIR`-relative.

---

## [2.31.0] — 2026-07-19

Publishable **heartbeat-flow** package (dataset → P(up) indicator), live
agent research surfaces (HB + S3 display/shadow only), product thesis
docs densify, and audit fixes. **No live order path** from heartbeat or S3.

### Added
- `heartbeat` package as installable **`heartbeat-flow`**: `run_dataset`,
  `load_trades`, structured `MissingDatasetError` / `InvalidDatasetError`,
  CLI `heartbeat run-dataset`, agent tools (`call_tool` / `TOOL_SCHEMAS`),
  `AGENT.md`, packaged `default.yaml`, AAPL fixture CSV.
- Live agent `hydra_heartbeat_surface` → `quant_indicators["heartbeat"]`
  every tick (incl. HOLD); dashboard RESEARCH band (P(up) sparkline + S3
  stage); kill `HYDRA_HEARTBEAT_SURFACE=0`.
- Brain advisory text for HB/S3 (explicit never force_hold / no order
  path); QUANT_PROMPT research-surface bullet.
- ABI funnel + bakeoff registrations/results (engine co-occurrence FORBID
  engine claims; confirmer INCONCLUSIVE n=1; cascade NO_BLACKOUT).
- Multi-pair status file contract (`heartbeat_status_<PAIR>.json`);
  auto-load calibrated weights on `heartbeat run`.

### Changed
- Docs densify: CB BUY-only wording, default pairs, BACKTEST runbook,
  companion manifest, HOLD_THROUGH, README thesis framing.
- S3 confirmer shares `read_status` with surface; dashboard S3 uses
  neutral (not buy-green) gated·shadow labeling.
- Empty / non-overlapping weight maps no longer report status=ok
  (uncalibrated degraded).

### Fixed (audit)
- Silent coin-flip when `weights={}` or keys miss enabled features.
- Pipeline ValueError (unordered trades / bad tf) → InvalidDatasetError.
- Absolute never-force_hold language for research surfaces in brain.

## [2.30.1] — 2026-07-19

Trail-exit research arc closed under a pre-registered gate: **no basis
flip** — X1 close-stop remains the tradable basis; two trailing-exit
constructions enter the shadow ledger as measurement-only arms. Research
corpus consolidated into `research/`.

### Added
- Pre-registered trail-exit gate (`heartbeat/evidence/bakeoffs/
  s3_trail_exit_REGISTRATION.md` committed before the runner;
  `heartbeat/tools/bakeoff_s3_trail_exit.py` →
  `s3_trail_exit.json`): X4a ride-MA9 passed expectancy, fold
  consistency, and the matched blind-time exposure control but FAILED
  LOYO verdict stability; X5 vigor-routed failed the exposure control
  with train-derived premium cuts. Pre-committed decision rule →
  both SHADOW_ARM_ONLY.
- `s3bounce` exit arms `x4a_trail_ma9` + `x5_vigor_routed` (shadow
  ledger only): `evaluate(..., ma9=)` keeps research parity via the
  caller-owned bar series; `OpenPosition.armed`/`premium_cut`
  persisted across restarts; artifact carries per-asset
  `premium_cut` (BTC 1.449 / ETH 1.585, final-fold train medians);
  `hydra_s3.py` computes MA9 from its series and flows the cut.
  New tests: 8 package (exits/shadow/model), s3bounce total 53.
- ABI discovery funnel committed (`heartbeat/evidence/abi/
  s3_trail_funnel_2026-07-19.md` + `heartbeat/tools/
  abi_s3_watermarks.py`): winner-truncation (+9.8/+13.0% post-target
  40d continuation) and bounce-vigor loss watermark (premium_atr)
  mechanisms, 15 frames, kill-tests recorded.
- `research/S3_BOUNCE_EDGE_2026.md` — formal research paper for the
  full S3 arc (gates, per-trade ledger, refutations, lessons);
  promoted study data in `research/data/s3/` (hold-horizon JSON,
  X1 per-trade watermark ledger, killed-line evidence under
  `killed/`).

### Changed
- `heartbeat/tools/export_s3_model.py` cross-check extended: hard-fails
  if the artifact ever promotes a trail arm past its gate decision or
  drifts a premium cut.
- `heartbeat/HONEST_FINDINGS.md` compacted into a verdict ledger
  (narratives moved to the research paper); killed-line bakeoff JSONs
  and the hold-horizon study relocated to `research/data/s3/` with all
  tool/artifact pointers updated.

## [2.30.0] — 2026-07-19

S3 daily bounce classifier productized: standalone `s3bounce/` package +
read-only agent signal surface + env-gated shadow strategy (no live
order path — live enablement remains gate-pending on the paper-shadow
window). Research evidence: pre-registered exit-policy gate (close-fill
stop ADOPTED; flip exits KILLED on fold consistency) and per-coin
hold-horizon study (long-hold "always right" claim REFUTED with Wilson
CIs; ETH K=60 shadow arm only; ZEC excluded).

### Added
- `s3bounce/` — standalone, stdlib-only, publishable package (own
  pyproject + 45 tests): daily-bar construction, verbatim ports of the
  research geometry/features (golden parity fixtures pin them to the
  heartbeat pipeline at 1e-9; `heartbeat/tools/export_s3_model.py` is
  the yearly refit path), frozen `model_artifact.json` (BTC/ETH 2026
  walk-forward fold; ZEC structurally absent), exit policies
  (x0/x1/hold_k60), causal stage machine (SW-lag documented), shadow
  ledger (JSONL events + atomic state, restart-safe dedupe).
- `hydra_s3.py` — agent adapter: confirmation-based candle folding
  (data clock, never wall time), registry-alias pair mapping, inert on
  error; `quant_indicators["s3"]` block (R10-safe by whitelist);
  Phase-2.6 shadow evaluation (`HYDRA_S3_STRATEGY`, default OFF) with
  per-exit-arm paper positions in `.hydra-s3/` and heartbeat confirmer
  payloads recorded (missing/stale/tainted => no_opinion, both-arm
  decisions logged). Kill switch `HYDRA_S3_DISABLED`.
- Harness scenario S3SH1: gated shadow proposal places ZERO orders.
- heartbeat evidence: `evidence/bakeoffs/s3_exit_policy*` (registration
  committed before arms ran), `evidence/s3_hold_horizon.json`,
  `tools/bakeoff_s3_exit_policy.py`, `tools/s3_hold_horizon_study.py`.
- `heartbeat/` — standalone research subsystem (own package, own test
  suite; zero coupling to the HYDRA engine): recursive Bayesian
  order-flow posterior P(up) from Kraken trade tape, built as a
  confirmation classifier for bounce fake-vs-reversal discrimination.
  Kraken WS v2 + REST feed with gap backfill and taint tracking,
  incremental candle engine, tiered feature registry (Tier 0 live,
  Tier 1 registered, Tier 2 stubbed), log-odds recursion with
  candle-unit memory, bounce labeler, AUC/Brier eval harness,
  walk-forward calibration, parquet store, live status-file + TCP
  confirmation API, deterministic synthetic-tape validation.
  Offline gates pass (70 tests: no-lookahead, determinism,
  hand-computed fixtures, mocked-transport reconnect/backfill);
  real-tape gates and the promote/kill decision are pending network
  access — see `heartbeat/HONEST_FINDINGS.md`.

---

## Older releases

Entries for 1.0.0 through 2.29.0 are in git: `git show v2.30.0:CHANGELOG.md`. GitHub Releases carry summaries from v2.10.3.
