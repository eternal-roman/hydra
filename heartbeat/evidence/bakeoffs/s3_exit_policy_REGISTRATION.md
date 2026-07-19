# Pre-registration — S3 exit-policy gate (2026-07-19)

Follow-up gate required by `ABI_FUNNEL_ROUND3_2026-07-18.md` exit-state
item 1(b): M-R3 (bounce payoff compounds with horizon) says the
registered tgt3.3 exit truncates the payoff — but exits must never be
swapped silently. This gate decides the S3 exit policy.

## Honesty preamble (bias disclosure)

The pooled exploratory numbers for two of the candidate arms
(`gated_close_stop`, `gated_flip`) were already computed and seen in
`s3_daily_classifier.json` — this registration is NOT blind to them, and
no untouched holdout exists (the full archive was consumed). The gate is
honest anyway because the pass/fail criteria below hinge on measurements
that have NOT been seen: per-fold consistency, exposure-matched blind
time controls (the M-R3 confound), tail-risk bounds, and the hybrid arm
(never simulated). Final authority stays with the S3 paper-shadow
window, which logs all exit arms in parallel going forward.

## Frozen design

Everything upstream of the exit is IDENTICAL to the promoted S3 bakeoff
(`bakeoff_s3_daily_classifier.py`): daily bars resampled from 1h
`hydra_history.sqlite` (refreshed 2026-07-19 before running), assets
BTC/USD + ETH/USD (ZEC/USD reporting-only — S3 excluded it), setups from
`paper_bounce_sim.causal_setups`, frozen 6-feature logistic per
expanding yearly fold Y=2016..2026, gate = train-p75, entry b1 close,
one-position sequencing, 26 bps/side. Only the exit rule varies, in one
unified simulator (the incumbent is re-run in the same simulator so arm
deltas cannot come from harness mismatches).

**Arms** (on each fold's gated pool):

| id | exit rule |
|---|---|
| X0 incumbent | stop on touch low<L0 (fill min(close,L0)) → target 3.3·ATR → 200-bar horizon (anchored at low_idx), close fill |
| X1 close-fill stop | stop on close<L0 (fill that close) → target 3.3·ATR → horizon |
| X2 flip | exit at close when daily trend ensemble < 0.6 (`exit_layer_lab.daily_scores`, mark-to-close engine semantics, causal donchian); no stop, no target; horizon cap |
| X3 hybrid (new, never simulated) | exit at close when close<L0 OR ensemble<0.6; no target; horizon cap |
| T_K blind time controls | exit at close of entry+K bars, K ∈ {5,10,20,30,50}; no signal content |

Exposure-matched control for X2/X3: T_K* where K* is the grid element
nearest that arm's own pooled BTC+ETH median hold (ties → lower K).
Deterministic, chosen after the arm runs, from a fixed grid.

Convention notes (uniform across arms, stated): close-decided exits fill
at the deciding bar's close (same convention as the incumbent's
min(close,L0) stop fill); yearly folds simulate independently; the ZEC
2026 fold spans the known 1h gap.

## Registered criteria (BTC+ETH only; ZEC never gates)

Per candidate arm X ∈ {X1, X2, X3}, vs X0 on identical pools:

- **C1 expectancy:** pooled BTC+ETH avg %/trade (net of fees) ≥ X0's + 0.5 pp.
- **C2 fold consistency:** over all (asset, year) folds where either arm
  logged ≥1 trade, X's fold return sum ≥ X0's in ≥ 60% of folds.
- **C3 information-over-exposure (X2/X3 only):** pooled BTC+ETH avg
  %/trade ≥ T_K*'s + 0.5 pp. A flip exit that cannot beat a blind
  time exit of the same average exposure is "hold longer", not an exit
  signal (the control that killed envelope-1h).
- **C4 tail bound:** pooled worst single trade no more than 10 pp worse
  than X0's worst, AND share of trades ≤ −15% no more than X0's + 10 pp.

**Adoption rule:** X1 passes on C1+C2+C4; X2/X3 pass on C1+C2+C3+C4.
Among passing arms the one with the highest pooled BTC+ETH avg %/trade
becomes S3's registered exit (provisional — shadow window logs all arms
in parallel). No passing arm → X0 stays. T_K arms are controls and can
never be adopted from this gate.

Runner: `tools/bakeoff_s3_exit_policy.py` → evidence
`evidence/bakeoffs/s3_exit_policy.json`. This file is committed before
the runner executes.
