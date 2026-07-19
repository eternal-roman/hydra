# Pre-registration — S3 trail-exit gate (2026-07-19)

Follow-up gate required by the ABI funnel
`heartbeat/evidence/abi/s3_trail_funnel_2026-07-19.md` (M1 winner
truncation: +9.8%/+13.0% BTC/ETH average continuation in the 40 bars
after a target exit; M2 bounce-vigor watermark). Exits are never swapped
silently — this gate decides whether the trail constructions replace the
adopted X1 close-stop basis.

## Honesty preamble (bias disclosure)

This registration is NOT blind. Both candidate arms were simulated
exploratorily in the funnel and their pooled numbers were seen (X4a
+283.8% pooled sum, 8/13 fold consistency; X5 +228.8%, 9/13), including
per-year totals. No untouched holdout exists. The gate is honest anyway
because it hinges on measurements NOT yet computed:

- the exposure-matched blind time controls T_K* (trail holds averaged
  16–20d vs X1's 10–13d in exploration — drift exposure is the obvious
  confound and the criterion most likely to kill);
- LOYO stability of the *verdict itself*;
- tail bounds under the registered pooled framing;
- X5 with per-fold TRAIN-derived premium cuts (exploration used a
  global cut of 1.3 ≈ pooled median — a mild look-ahead this gate
  removes).

Final authority stays with the S3 paper-shadow window, which will log
all arms in parallel going forward regardless of this verdict.

## Frozen design

Everything upstream of the exit is IDENTICAL to the promoted S3 bakeoff
and the exit-policy gate: daily bars from 1h `hydra_history.sqlite`,
assets BTC/USD + ETH/USD (ZEC/USD reporting-only), setups from
`paper_bounce_sim.causal_setups`, frozen 6-feature logistic per
expanding yearly fold, gate = train-p75, entry b1 close, 26 bps/side.
One unified simulator; the incumbent X1 is re-run inside it.

**Sequencing delta vs the exit-policy runner (declared):** the
one-position lock carries across yearly folds (a late-year open position
blocks next-year entries) — the more realistic convention. X1 is re-run
under the same convention, so the baseline shifts identically
(exploratory: BTC 23 trades unchanged, ETH 28 → 27).

**Arms** (on each fold's gated pool):

| id | exit rule |
|---|---|
| X1 incumbent | stop on close<L0 (fill that close) → target 3.3·ATR → 200-bar horizon (anchored at low_idx) |
| X4a ride-MA9 | stop on close<L0 while UNARMED; ARM when close ≥ L0+3.3·ATR (the old target is an arming line, position not exited); once armed, exit at close < MA9 (9-bar simple MA of daily closes — the setup's own MA, no new parameter); horizon cap |
| X5 vigor-routed | entry-time route: premium_atr = (b1_close − L0)/ATR > cut → X4a rule, else X1 rule. Cut = median premium_atr of the fold's TRAIN-pool gated setups (train-derived, per fold, never global) |
| T_K blind time controls | exit at close of entry+K bars, K ∈ {5,10,20,30,50}; no signal content |

Convention notes (uniform): close-decided exits fill at the deciding
bar's close; MA9 computed on the same daily series the setups use;
ZEC's 2026 fold spans the known 1h gap.

## Registered criteria (BTC+ETH pooled; ZEC never gates)

Per candidate arm X ∈ {X4a, X5}, vs X1 on identical pools:

- **C1 expectancy:** pooled BTC+ETH avg %/trade (net) ≥ X1's + 0.5 pp.
- **C2 fold consistency:** over all (asset, year) folds where either arm
  logged ≥1 trade, X's fold return sum ≥ X1's in ≥ 60% of folds.
- **C3 information-over-exposure:** pooled avg %/trade ≥ T_K*'s
  + 0.5 pp, where K* = grid element nearest X's own pooled median hold
  (ties → lower K). Applies to BOTH candidates (both extend exposure).
- **C4 tail bound:** pooled worst trade no more than 10 pp worse than
  X1's worst, AND share of trades ≤ −15% no more than X1's + 10 pp.
- **C5 LOYO verdict stability:** recompute the C1–C4 verdict leaving
  each calendar year out; the adopted arm must remain passing (or the
  verdict degrade to no-adopt) in every LOYO replay — a verdict that
  flips between candidates across LOYO replays is unstable → no-adopt.

## Pre-committed decision rule

- **PASS (C1–C5):** the passing arm with the highest pooled avg %/trade
  becomes the artifact `exit_policy` for BTC/ETH; a second passing arm
  becomes a shadow arm.
- **FAIL any of C1/C2/C3/C5 but C4 clean:** shadow arm only
  (measurement, like `hold_k60_stop`); the X1 basis is unchanged.
- **C4 violation:** dropped entirely — not even a shadow arm.

T_K arms are controls and can never be adopted.

## Secondary registered measurements (reported, never gate)

1. Breadth-horizon inversion: fwd60 of gated entries by train-median
   breadth split (funnel M5: +23.3% low vs +1.3% high, seen).
2. leg_depth stop-rate split (funnel F3, seen).
3. Post-stop re-entry outcomes (funnel F11, n≈7, underpowered —
   monitored for the shadow window).

Runner: `tools/bakeoff_s3_trail_exit.py` → evidence
`evidence/bakeoffs/s3_trail_exit.json`. This file is committed before
the runner executes.
