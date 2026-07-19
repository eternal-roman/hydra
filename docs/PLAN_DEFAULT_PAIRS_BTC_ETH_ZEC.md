# Plan: Remove SOL pairs as defaults; move to BTC/USD + ETH/USD + ZEC/USD

> Produced 2026-07-19 by a dedicated planning agent (live-verified against
> Kraken Futures/AssetPairs and hydra_history.sqlite) after 90-day
> real-tape studies showed SOL flow-classifier AUC ~0.56 (vs 0.76-0.82
> BTC/ETH) and `.hydra-flywheel/bridge_isolation.json` had already proven
> the SOL/BTC bridge dead. DEFAULT flip only — explicit SOL pairs, the
> TradingTriangle machinery, and `HYDRA_BRIDGE_TRADING` stay fully
> functional. Status: PLAN ONLY, not yet implemented.

**Session update to the agent's findings:** ETH/USD archive depth is
DONE (87,689 hourly candles 2015→2026 imported from the Kraken CSV dump
after the agent's snapshot); ZEC/USD archive runs 2016-10→2025-12-31 and
the 90d trade backfill heals 2026-04→now, leaving a Jan–Apr 2026 gap
until Kraken publishes the Q1/Q2 2026 dumps (bootstrap_history path).

## Key structural verdicts

1. **Triangle: recommend option (c)** — default to three independent
   stable-quoted pairs with NO triangle/coordinator (both
   `_derive_triangle`s already return None gracefully; per-quote balance
   pools are quote-driven, not triangle-driven; zero changes to
   `hydra_config.py`). ETH/BTC triangle (a) has zero evidence; role
   generalization (b) is a ~250-assertion refactor to generalize rules
   that encoded a SOL thesis — do later only if a /bakeoff shows
   cross-asset coordination edge.
2. **Derivatives coverage:** `PF_ETHUSD` exists with FF quarterlies →
   full R1-R11. `PF_ZECUSD` exists but has NO quarterlies → without a
   fix, ZEC sits permanently at 1/2 stale fields (basis_apr_pct None)
   and any transient miss trips a structural R10 force-hold. Fix: new
   `basis_available=False` structural flag (map-driven, mirrors the
   synthetic/uncovered invariants) so R10 tracks 4 fields for ZEC. New
   CLAUDE.md invariant bullet required.
3. **Production transition risk:** `start_hydra.bat --resume` + flipped
   pair list silently orphans SOL inventory in the snapshot. Drain first
   (interim mixed-pairs session or manual sell), verify snapshot clean,
   then flip.
4. **MINOR bump v2.29.0**, full 9-site version protocol + mock harness
   mandatory (agent boot path).

## Enumeration (defaults that change)

hydra_agent.py: argparse default 4331-4336, demo-auto fallback 4366,
discover_portfolio_pairs core list 4257 + skip set 4286-4287
(→ `("BTC","ETH","ZEC")`), docstring 6-15, `_DEMO_SEED_PRICES` (add
ZEC/USD). hydra_engine.py: `MIN_ORDER_SIZE` add `"ZEC": 0.01` (742-745).
hydra_pair_registry.py `_FALLBACK_PAIRS` 349-380 (add ETH/USD ordermin
0.001 costmin 0.5 dec 2 tick 0.01; ZEC/USD ordermin 0.01 costmin 0.5
dec 2 tick 0.01). hydra_derivatives_stream.py SPOT_TO_DERIVATIVES 69-80
(add ETH/USD(+USDC/USDT) w/ FF prefix; ZEC/USD perp-only). launchers:
start_hydra.bat:12, start_hydra_companion.bat:35 (KEEP `--mode
competition --resume`). hydra_backtest.py 73/1181; hydra_backtest_tool.py
100/106/541/705; hydra_backtest_server.py 661-662 whitelist (superset:
keep SOL, add ETH/ZEC); hydra_experiments.py 445/1037;
scripts/go_live_gates.py 65-75; dashboard LabPane.jsx:21 (superset),
DatasetPane.jsx 153-156; hydra_brain.py prompt narrative
102/202-203/226-241/270/303/361/1275 + hydra_agent.py
`_build_triangle_context` net_exposure 2281-2323 (generalize per-base);
tools/bootstrap_history.py 25-26/131-132 (add ETHUSD/ZECUSD);
heartbeat tool defaults; tests: test_config.py default-assertions,
test_derivatives_stream.py 119-126 fixture (ETH/USDC becomes mapped —
switch to NIGHT/USD).

**Must NOT change:** hydra_config.py TradingTriangle/_BRIDGE_BASE,
CrossPairCoordinator rules, both _derive_triangle implementations,
_BUY_LIMIT_OFFSET_BPS (missing key → 0 bps, safe for ETH/ZEC),
state migrator, test_cross_pair.py, live_harness scenarios,
HYDRA_BRIDGE_TRADING. **Frozen:** research/data, evidence dirs,
CHANGELOG history, .hydra-flywheel gates.

## Sequenced steps

0. Data readiness: ETH archive DONE; ZEC 2026 gap via bootstrap when
   dumps publish (non-blocking — backtests handle the gap per-year).
1. Derivatives coverage (additive): ETH entries, ZEC perp-only +
   `basis_available` flag through DerivativesSnapshot →
   `_build_quant_indicators` → `_count_stale_fields` (4-field track);
   tests for 4/5/synthetic/uncovered tracks. Verify: derivatives+quant
   test suites; live read-only futures tickers parse; short --paper run.
2. Registry + sizing constants (additive). Verify: registry/engine
   tests; `--demo --pairs BTC/USD,ETH/USD,ZEC/USD` boots and seeds.
3. The default flip (single PR): all §enumeration sites + docs
   (CLAUDE.md 42-54/77/86-87/94/194-205, SKILL.md, README, docs/).
   Verify: full pytest; harness --mode mock (MANDATORY); --demo run
   with no --pairs shows new trio + coordinator no-op; go_live_gates.
4. Production transition: stop agent → inspect snapshot SOL positions →
   drain if not flat → flip → verify snapshot/journal clean.
5. Brain prompt + net_exposure generalization (separable PR).
6. Release v2.29.0 via /release (9 sites, alignment gate).

## Open decisions (user)

1. Confirm triangle option (c). [recommended]
2. `--pairs auto`: seed all three cores always, or only held cores?
3. Held SOL under auto: satellite-trade it, exclude, or drain-only?
4. ETH/USDC+USDT derivative map parity now or USD-only?
5. go_live_gates: BTC-only or all three?
6. ZEC/ETH have no hold-through/friction/overlay gate evidence (those
   were SOL/BTC-calibrated): require /bakeoff before live sizing, or
   accept fail-open defaults?
7. ETH/BTC pair out of scope. [recommended]
8. hydra_flywheel ASSETS swap now or defer. [defer recommended]
9. SOL inventory drain method.
10. Version v2.29.0. [recommended]
