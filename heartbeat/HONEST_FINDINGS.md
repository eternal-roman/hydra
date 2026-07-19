# HONEST_FINDINGS — heartbeat v0.1.0 (2026-07-17; real-tape update 2026-07-19)

## The core question

> Does the heartbeat separate fakes from reversals at bounce+3, and at
> what AUC per asset?

**ANSWERED on real Kraken tape (2026-07-19).** 90 days of real trades
per asset (SOL 1.84M, BTC 4.80M, ETH 2.06M), backfilled via REST at the
project's 2s floor, cross-verified against `hydra_history.sqlite`
(candle aggregation matched exactly; see `evidence/tape_verify_*.json`)
and durably mirrored into that DB's `trades` table. Walk-forward
calibration, train strictly before test, ≥60 events per asset
(full reports: `evidence/real_tape/`):

| asset | events | fold AUCs @ bounce+3 (calibrated) | verdict |
|---|---|---|---|
| BTC/USD | 69 (31 rev / 38 fake) | **0.90, 0.55, 0.84** | PASS (mean 0.76) |
| ETH/USD | 77 (38 / 39) | **0.73, 0.77, 0.69** | PASS (mean 0.73) |
| SOL/USD | 80 (31 / 49) | 0.62, 0.65, 0.40 | FAIL (mean 0.56) |

**Promote gate (AUC ≥ 0.70 walk-forward on ≥2 of 3 assets): PASSED**
— by BTC and ETH; SOL shows no exploitable flow signal. The pattern is
liquidity-consistent: the hypothesis holds on deep majors, not on SOL.
Uncalibrated (default-weight) AUC is ~0.55 everywhere — calibration is
mandatory, as the synthetic study predicted. Real-tape weights rank
`clv` and `ofi_momentum` on BTC/ETH broadly in line with the synthetic
ranking, but SOL's fit is unstable; the synthetic negative-`ofi` tell
did not replicate.

## HYDRA integration bake-off (2026-07-19): structurally inconclusive

Gating HYDRA BUY entries on the posterior was baked off on the same
90-day window (baseline / P20-P65 train-percentile gates / inverse
controls / OOS split — `tools/hydra_bakeoff.py`, evidence
`evidence/hydra_bakeoff*.json`). Verdict: **no evidence either way,
and none was obtainable** — the v2.28 production config (trend overlay
+ hold-through + friction hurdle) generated **zero** BUY entries across
the entire window (a 90-day downtrend it correctly sat out in cash),
and even with `HYDRA_TREND_OVERLAY=0` the raw engine attempted exactly
one entry. A BUY-confirmation gate cannot add value where there are no
BUYs to confirm. Do NOT interpret this as "gate is useless" or "gate is
safe to wire in" — it is untested against actual entries. The
data-indicated path is the one heartbeat was designed for: confirming
counter-trend bottom-buy entries — an entry family HYDRA currently does
not take at all in downtrends. That would be NEW strategy surface and
needs its own evidence-gated bake-off before any live wiring.

## Canonical-store defects found by the tape audit (fixed)

Cross-verification exposed two `hydra_history.sqlite` defects inherited
by every backtest: ~50% of 1h rows missing in the 90d window per pair
(Kraken REST OHLC cannot paginate deep history), and frozen
still-forming candles (volume ~10x low; SOL and BTC corrupt at the same
hour). Fixed: `tools/heal_ohlc_from_trades.py` repaired the store from
trade-level truth (post-heal: 2159/2159 hours exact on both pairs), and
`tools/refresh_history.py` now skips the forming candle on both refresh
paths (regression-tested).

## What was verified (with evidence)

* **No lookahead** — incremental heartbeat state at every cut point is
  bit-identical to a from-scratch replay of the prefix tape
  (`tests/test_no_lookahead.py`, 5 cut points + prefix-invariance test).
* **Determinism** — two replays of the 150-day BTC synth tape produce
  identical SHA-256 digests
  (`evidence/gate2_replay_determinism.txt`:
  `3dd5600ed9d4e152...` twice); also asserted through a parquet-store
  round-trip with deliberately overlapping part files.
* **Candle-unit memory** — with constant evidence, the recursion sampled
  at candle closes matches the candle-level recursion `L ← λL + wz`
  within 5% for 1, 10, and 60 heartbeats/candle, and converges to the
  same fixed point (`test_candle_unit_memory`). Memory really is defined
  in candle units.
* **Exact calibration transfer** — `L = Σ wᵢSᵢ` exactly, so logistic
  weights fit on snapshot S-vectors are the live posterior's weights
  with zero approximation gap (`test_L_equals_weighted_S`).
* **Feed integrity** — reconnect + REST gap backfill emits trades in
  order and taints only when backfill is incomplete (mock-transport
  tests); clock skew >2s and sequence violations taint; tainted events
  are excluded from eval.
* **Every Tier 0/1 feature** against hand-computed fixtures.
* **Labeler** — 84–131 events per synthetic asset over 150 days
  (≥60 required); crash-regime and chop exclusions behave as specified.

## Synthetic-tape results (150 days, 1h, three seeds as BTC/ETH/ZEC)

Walk-forward, expanding window, train strictly before test
(`evidence/gate4_walkforward.txt`):

| asset (seed) | events | fold AUCs @ bounce+3 |
|---|---|---|
| BTC (7)  | 84 (43 rev / 41 fake) | 0.52, **0.78, 0.79** |
| ETH (21) | 131 (51 / 80) | 0.58, 0.54, 0.50, 0.64 |
| ZEC (99) | 107 (47 / 60) | 0.58, 0.68, 0.65, 0.62 |

The e2e suite's cleaner 40-day tape reaches held-out **0.92** at
bounce+3. Why the spread? The labeler also finds *organic* bounces in
the random-walk down-legs — events whose flow is genuinely
uninformative but whose labels are decided by future noise. They dilute
AUC toward 0.5 exactly as ambiguous real-market events would. This is
the honest behavior you want from the harness: it does not manufacture
separation that is not in the tape.

## What discriminates (on synthetic flow)

Consistent across all three tapes (final fitted weights):

* **`ofi_momentum` dominates with a positive weight** (+0.50…+0.96) —
  supporting the spec's hypothesis that *sign and slope of flow, not
  level*, separates fake from reversal.
* **`clv` is second** (+0.24…+0.59): reversal candles close upper-third.
* **Decayed `ofi` level fits NEGATIVE** (−0.13…−0.52): after a long
  down-leg, a *less* negative 30-candle flow memory going into the
  bounce was, on these tapes, a fake tell. Worth re-testing on real
  tape before believing.
* `range_atr` ≈ 0 and `vol_z` small positive — non-directional alone;
  naive equal weights are near chance (bounce+3 AUC 0.53 on BTC synth),
  which is precisely why calibration is not optional.

## Known limitations / deviations

1. **Network gates not run**: real 90-day backfill, live WS soak (24h),
   and the live socket-kill drill require a machine with Kraken egress.
   Exact commands are in README "Verification gates". The WS/REST code
   paths are tested against mocked transports that mimic Kraken's
   documented v2/REST shapes — first contact with the real endpoints may
   still surface schema drift; the parsers fail loudly if so.
2. **Micro-bucketing rarely triggered on synth tapes** (~40 trades/h ≪
   20/s threshold), so the >20/s bucketing path is covered by unit
   logic, not by a realistic firehose.
3. **Tier 2 features are stubs** by design (book channel, cancel rates,
   BTC-lead, funding) — they return None until their data sources are
   wired and individually evidenced. BOCD λ-modulation is an engine hook
   (`lambda_modulator`), not yet implemented.
4. **P(up) absolute level is regime-dominated**: after a 20-candle
   down-leg the 30-candle memory pins P(up) near 0 for both classes;
   discrimination lives in the *relative* posterior at checkpoints. A
   consumer wanting an absolute threshold should use calibrated weights
   and compare against the event-conditional distribution, not 0.5.

## Recommendation (updated 2026-07-19, real-tape)

**Promote the classifier on BTC+ETH to the next gate (live soak);
exclude SOL; do NOT wire into HYDRA entry gating yet.**

1. ~~Backfill real tape~~ DONE (SOL/BTC/ETH, 90d, verified, mirrored
   to sqlite).
2. ~~Eval + calibrate walk-forward~~ DONE — gate PASSED on BTC+ETH
   (see table above); SOL failed and stays out.
3. Tier 0 cleared on 2 assets ⇒ per the original plan, enabling Tier 1
   (`features.enabled_tiers: [0, 1]`) and re-gating on BTC/ETH is the
   next classifier improvement step.
4. Remaining network gate: 24h live WS soak on BTC (`heartbeat run`)
   plus the socket-kill drill — the WS reconnect/dedup path is
   mock-tested (`tests/test_ws.py`) but has not met the real socket.
5. Integration with HYDRA: the bake-off proved current HYDRA takes no
   entries heartbeat could confirm (see above). The evidence-backed
   route is a dedicated bounce-entry strategy (trailing-stop bottom-buy
   on BTC/ETH) using heartbeat as its confirmation layer, run through
   `/bakeoff` as new strategy surface: paper first, pre-registered
   criteria, its own gate JSON. No live wiring before that passes.
