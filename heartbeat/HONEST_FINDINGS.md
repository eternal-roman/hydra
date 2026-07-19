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

## ZEC/USD (added 2026-07-18, after the full ZEC pipeline)

Archive imported (7.03M trades 2016→2025-12-31 → sqlite `kraken_archive`),
90d sided tape backfilled (1.49M trades), healed, verified
(`evidence/tape_verify_ZEC_USD.json`: 2159/2159 hours, 0 bad).

- **Classifier: FAIL.** 75 events (33 rev / 42 fake); walk-forward
  calibrated bounce+3 AUC 0.61/0.76/0.46 (mean **0.61** < 0.70 bar) —
  `evidence/real_tape/calibrate_ZEC_USD.txt`. The final fold (the
  late-June/July selloff) collapses to 0.46, same failure shape as SOL.
  CLV is again the top weight (+0.32).
- **Paper sim: first positive OOS arm, but it is regime beta, not
  classifier alpha.** `evidence/paper_bounce_sim_ZEC.json`: B&H in the
  ~36-day OOS slice is **+33.2%**; the mechanically train-selected arm
  (`b1.gate_p50.exitA_flow`) makes **+6.2%** OOS (n=12, PF 1.39) —
  positive but far under B&H. The unselected `b3.all.exitA_flow` arm
  makes +30.2% (PF 2.43) and the **inverse control makes +24.6%** —
  everything long ZEC made money in that window. Flow-exit (trailing)
  arms are positive across all/gated/inverse pools while target/stop
  arms all lose, consistent with the geometry study: ZEC's returns come
  from riding its regime, not from picking bounces.
- Net: ZEC joins SOL on the classifier exclusion list; its trading case
  is the **daily-bar trend/bounce construction** (ZEC-2025 daily target
  arm +94% in `bounce_geometry_1d.json`), not 1h flow confirmation.

A stale early-run eval artifact (`gate3_eval_ZEC.txt`, written hours
before the backfill finished, 107 events with garbage timestamps) was
deleted; `evidence/real_tape/eval_ZEC_USD_1h.*` is authoritative.

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

## S3 exit-policy gate (2026-07-19, pre-registered)

Registration committed before arms ran
(`evidence/bakeoffs/s3_exit_policy_REGISTRATION.md`; bias disclosure
inside — the exploratory pooled numbers had been seen, the deciding
measurements had not). Runner `tools/bakeoff_s3_exit_policy.py`,
evidence `evidence/bakeoffs/s3_exit_policy.json`. The incumbent arm
reproduces the promoted S3 gated P&L exactly (BTC 23/+1.03, ETH
30/+2.25 — same simulator, so arm deltas are pure exit effects).

- **ADOPTED: X1 close-fill stop** (stop on close<L0, filled at that
  close; target/horizon unchanged). Pooled BTC+ETH +2.36%/trade vs
  incumbent +1.72 (C1 ✓ by 0.64pp ≥ 0.5), fold consistency 8/13 =
  61.5% (C2 ✓), tails strictly better: worst −16.5% vs −36.6%, share
  ≤−15% 2% vs 3.8% (C4 ✓). Consistent with the round-2 finding that
  close-FILL is legitimate mechanics (touch-fills sell the wick low).
- **KILLED: X2 flip and X3 hybrid — C2 fold consistency 5/13 =
  38.5%.** Their +13.2%/trade pooled averages are fold-concentrated;
  flip loses to the incumbent in 8/13 (asset,year) folds incl. a −57%
  BTC-2021 fold sum. M-R3's compounding payoff is real (T_K controls
  rise monotonically: +1.9 → +9.7%/trade from K=5→50) but so are the
  time-control tails (worst −49%, share ≤−15% up to 22%): long holds
  are regime beta with regime-beta drawdowns, and the ensemble-flip
  signal does not time them reliably. Degenerate-construction note:
  flip median hold = 1 bar — about half the gated entries occur with
  the daily ensemble already < 0.6, so the "exit signal" fires
  immediately; any future flip-style candidate must first fix its
  entry/exit-state interaction (new anomaly required to revisit,
  per the funnel rules).
- S3's registered exit is now **entry b1 close / tgt 3.3·ATR /
  close-fill stop at L0 / 200-bar horizon**. Expectancy under it:
  BTC +1.17, ETH +3.34 %/trade net of 26 bps/side (ZEC +1.22 but
  stays excluded — its classifier gate failed). Still provisional
  pending the paper-shadow window, which logs all exit arms in
  parallel.

## S3 hold-horizon study — per-coin, confidence-bounded (2026-07-19)

User-directed precision pass before any live wiring: is the classifier
"right every time" at K=20/50 continuation holds? **No — and the claim
dies on per-entry data.** Tool `tools/s3_hold_horizon_study.py`,
evidence `evidence/s3_hold_horizon.json`. Per-entry forward curves
(k=1..60, close-fill L0 stop composed, 26 bps/side), Wilson 95% CIs,
10k-draw bootstrap LB on the mean, LOYO stability of the K* choice:

- **Per-entry hit rates at K≥20 are coin-flip on every asset.** BTC
  K=20: 14/24 = 58.3% [CI 38.8–75.5]; K=50: 12/23 = 52.2% [33.0–70.8].
  ETH K=20: 16/32 = 50.0% [33.6–66.4]; K=50: 15/32 = 46.9%. ZEC K=50:
  5/21 = 23.8%. Every CI includes 0.5. The earlier T_K sequenced win
  rates (0.61–0.72) were flattered by one-position sequencing dropping
  clustered entries.
- **Large-K averages are lottery-shaped.** ETH K=50: avg +13.1% but
  MEDIAN −4.1%; top-3 trades carry +252pp of the +519pp K=60 total.
  BTC K=40: top-3 carry +141pp of +183pp. Optimal-hold distributions
  are bimodal (peaks at 1–5 and 46–60 bars): a leg either fails fast
  or rides a regime for months — there is no single "right" K.
- **Fold-level the long-hold construction is still coherent:** BTC
  K=40 positive fold sums 7/7 years, ETH K=60 5/6 — but BTC's
  bootstrap 2.5% LB is NEGATIVE (−0.2%/trade) and its LOYO K* flips
  3↔40 (unstable). **ETH K=60 is the one significant long-hold:**
  boot LB +4.6%/trade, LOYO-stable at 60 across all folds.
- **Stop frequency:** 11/24 BTC, 17/32 ETH, 15/21 ZEC gated entries
  hit the L0 close-stop within 60 bars — the classifier picks legs
  whose low holds only about half the time; its real, CI-supported
  edge is short-horizon bounce quality under the X1 target/stop
  construction (win rates 0.64–0.70, bounded tails).

**Registered per-coin algorithm basis (what real money may use):**

| asset | basis | status |
|---|---|---|
| BTC/USD | X1 exit (close-fill stop L0 / tgt 3.3·ATR / 200-bar horizon), +1.17%/trade | tradable basis, provisional pending shadow |
| ETH/USD | X1 exit, +3.34%/trade | tradable basis, provisional pending shadow |
| ETH/USD K=60 long-hold | boot-LB +4.6%/trade but 44% hit rate, median −4.9%, top-3 = half of P&L | shadow-tracked candidate arm ONLY — not a live basis |
| ZEC/USD | none — classifier FAIL + no K with positive LB | excluded |

No blind K is adopted anywhere. Long-hold continuation is NOT part of
the tradable basis; it survives only as a parallel shadow arm on ETH.

## Shipped (2026-07-19): s3bounce package + HYDRA shadow integration

The S3 basis above is now productized: standalone stdlib-only package
`s3bounce/` (own pyproject/tests; golden parity fixtures pin it to this
pipeline at 1e-9 — regenerate via `tools/export_s3_model.py`), plus
agent integration `hydra_s3.py`: read-only `quant_indicators["s3"]`
signal surface + `HYDRA_S3_STRATEGY` shadow phase logging per-exit-arm
paper positions to `.hydra-s3/` (x0/x1 both assets, hold_k60 ETH only;
heartbeat confirmer payload recorded, both-arm decisions). NO live
order path exists — live enablement remains gate-pending on this
shadow window per the funnel rules.
