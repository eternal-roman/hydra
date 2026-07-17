# HONEST_FINDINGS — heartbeat v0.1.0 (2026-07-17)

## The core question

> Does the heartbeat separate fakes from reversals at bounce+3, and at
> what AUC per asset?

**On real Kraken tape: UNANSWERED.** This build environment has no
network egress to api.kraken.com (the egress proxy policy-denies the
CONNECT — verified, not assumed). Every result below is from
deterministic **synthetic** tapes whose bounce events encode the
hypothesis by construction. Synthetic results validate the *machinery*
(can the pipeline separate the two archetypes when they exist in the
flow?), not the *market hypothesis* (do they exist in Kraken's flow?).
Do not promote to live confirmation duty on these numbers.

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

## Recommendation

**Extend, do not promote and do not kill.** The machinery is sound
(gates 1–5 pass offline; the pipeline provably recovers injected
flow-persistence signal, held-out AUC 0.92 on clean archetypes). The
hypothesis itself remains untested against reality. Next steps, in
order:

1. Run `heartbeat backfill --days 90` for BTC/ETH/ZEC on a connected
   machine (budget: hours per asset — Kraken rate limits).
2. `heartbeat eval` + `heartbeat calibrate --walk-forward` on the real
   tapes. Promote only if AUC ≥ 0.70 by bounce+3, walk-forward, ≥60
   events, on ≥2 of the 3 assets.
3. If Tier 0 clears on ≥2 assets: enable Tier 1
   (`features.enabled_tiers: [0, 1]`) and re-gate.
4. If real-tape AUC lands in the 0.55–0.65 band, the highest-leverage
   additions per the synthetic feature ranking are flow-slope variants
   (Tier 0 `ofi_momentum` refinements) before any Tier 2 exotica.
