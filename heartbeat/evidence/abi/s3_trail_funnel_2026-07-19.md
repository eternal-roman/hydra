# ABI Funnel — S3 loss watermarks & winner-truncation (2026-07-19)

Cycle: Anomaly → Bore → Ideate on the shipped S3 basis (v2.30.0,
`s3bounce/s3bounce/model_artifact.json`, X1 close-stop exit). Directive:
find the entry-time watermarks of losses and the mechanism for holding
real winners. **Everything below is EXPLORATORY — numbers were seen.
Nothing here is promotable; the exit is a pre-registered `/bakeoff`.**

All computations reproduce the promoted pools exactly (X1: BTC 23
trades / +1.17%/trade, ETH 28 / +3.34%) before extending them. Tools:
scratchpad forks of `tools/bakeoff_s3_exit_policy.py` (fold/gate/entry
machinery imported, not re-derived); one-position lock carried across
year folds (drops 1 ETH overlap → 27).

## 1. Anomaly inventory (committed evidence, numbers quoted)

| id | anomaly | source |
|---|---|---|
| A1 | Optimal-hold histograms bimodal on ALL assets — BTC: 8 trades at 1–5d, **0** at 6–10d, 8 at 46–60d; ETH 10/10. A mixture of two species, not one population with an optimal K | `s3_hold_horizon.json` |
| A2 | ETH K=60 boot-LB +4.6%, LOYO-stable 6/6, LB **still rising** at grid edge (40→1.67, 50→2.68, 60→4.62); BTC K*=40 LB −0.2, flips 3↔40 | `s3_hold_horizon.json` |
| A3 | Stops are pure cost: 23/23 stop exits are losses; avg −8.4/−8.6/−13.9% (BTC/ETH/ZEC); worst bled 39d. Design geometry 3.3:1 inverted into avg win +5.3% vs avg loss −8.4% (BTC) | `s3_exit_policy.json` + ledger |
| A4 | Half of X2 flip exits fired on bar 1 → many gated entries occur with daily ensemble already < 0.6 | `s3_exit_policy.json` caveats |
| A5 | Oracle-fake survivors are outsized winners (ETH 2021-05-25 +24.4%/98d; BTC 2026-06-27 +7.4%) — wicked below L0, never closed below | trade ledger |

## 2. Bore — mechanisms with verifying computations

Pooled BTC+ETH gated X1 trades, n=50 (17 stops / 33 targets).

**M1 — Winner truncation (the missing edge).** Return AFTER a target
exit: BTC +40d avg **+9.8%** (median +6.1%, 11/16 positive); ETH +40d
avg **+13.0%** (median +6.9%, 11/17). The fixed 3.3·ATR target donates
roughly half the move. Combined with A3 (stops never save into profit),
the strategy's asymmetry is inverted: winners truncated, losers held to
a slow stop.

**M2 — Bounce-vigor watermark (`premium_atr` = (b1_close − L0)/ATR).**
Median split: weak half stop-rate **0.48**, fwd60 **+3.8%**; strong half
stop-rate **0.20**, fwd60 **+20.7%**. Not target-proximity geometry —
the continuation differential is 5×. Strongest single entry-time
discriminator found (stop-vs-target effect −0.70 SD). Composite check:
"above MA200 & weak bounce" stops 57% (n=7); "below MA200 & strong
bounce" stops 21%, fwd60 +16.5% (n=14).

**M3 — Whipsaw stops.** 6–7 of 17 stops were thesis-correct: fwd60 ≫ 0
after stopping (BTC 2021-06-10 −16.5% → +25.7%; ETH 2022-06-17 −8.9% →
**+72.5%**; BTC 2021-09-15 −11.2% → +35.5%). True saves cluster at the
2021-top/2022-cascade onset (−44/−48/−60pp avoided). The close below L0
is often the actual capitulation low.

**M4 — Ensemble is NOT a loss watermark.** Entries with daily ensemble
< 0.6: stop-rate 0.31, fwd60 **+14.9%**; ensemble ≥ 0.6: stop-rate
0.38, fwd60 +9.4%. Confirms the flip-exit kill and the design decision
that S3 live entries must bypass the trend overlay.

**M5 — Breadth inverts across horizons (NEW anomaly, fed back).** High
breadth → fewer stops (0.25 vs 0.38 in stop/target means) but low
breadth → the continuation: fwd60 **+23.3% vs +1.3%**. Systemic
capitulations bounce reliably then chop; idiosyncratic washouts run for
months. Unmeasured before; candidate router/feature for the bakeoff's
secondary measurements.

## 3. Frames (bisociation) and kill-tests

| # | domain | mapping → implication | kill-test result |
|---|---|---|---|
| F1 | Insurance/actuarial | stop = policy; price premium by regime (widen when already capitulated) | **KILLED** — save-value symmetric above/below MA200 (4/8 vs 4/9 saves) |
| F2 | Emergency triage | bounce vigor = vital signs; weak → palliative fixed target, strong → full treatment (trail) | **SURVIVOR** = X5 router: fold-consistency 9/13 = 0.69, pooled +228.8% vs X1 +115.4% |
| F3 | Fire ecology | deepest burns → strongest regrowth (leg_depth sizing) | stop-avoidance only (0.20 vs 0.48); fwd60 flat (13.0 vs 11.6) — feature candidate, no gate |
| F4 | Queueing | long holds queue-block later entries | negligible: ETH forfeits 1–2 small X1 wins under trail |
| F5 | Auctions/winner's curse | high margin = overpaying | **KILLED** — margin mildly *good* (−0.25 SD, corr +0.16) |
| F6 | Epidemiology | high breadth = herd capitulation | INVERTED — see M5; recorded as secondary measurement |
| F7 | Materials annealing | shock_recency resets vol | **KILLED** — effect −0.05 SD |
| F8 | Serve-and-volley | point isn't over at the serve; play the continuation | **SURVIVOR** = X4a trail: 8/13 = 0.62, pooled +283.8% |
| F9 | Cold-chain logistics | signal decays in illiquid carriage | supports ZEC exclusion (avg loss −13.9%; trail FAILS on ZEC: −2.05%/trade) |
| F10 | Meteorology fronts | MA200 side as front boundary | **KILLED** — fwd60 12.6 vs 12.0, washes out alone |
| F11 | Immunology | post-stop re-entry = vaccinated (true low in) | direction strong (fwd60 +29/+35% vs +8/+10%) but n=7 — **no power**, monitored only |
| F12 | Catastrophe bonds | ATH-proximity as cascade trigger | folded into F1 — killed with it |
| F13 | r/K selection | ETH r-strategist (long continuation), BTC K-strategist | consistent with A2; justifies per-asset exit arms |
| F14 | Behavioral (disposition) | incumbent exit = encoded disposition effect; trail un-inverts it | same implication as F8 |
| F15 | Dead reckoning | time-stop staleness after 20d | **KILLED** — 1–2 qualifying trades, no power |

## 4. Survivors → pre-registered /bakeoff (the only promotion path)

Candidate arms (constructions frozen here, BEFORE registration):

- **X4a "ride-MA9"**: stop close<L0 until close ≥ L0+3.3·ATR (old
  target level, now an arming line); thereafter exit at close < MA9.
  MA9 is the setup's own MA — no new fitted parameter.
- **X5 "vigor-routed"**: premium_atr > cut → X4a; else X1 fixed target.
  Cut MUST be train-fold-derived (exploratory global 1.3 ≈ pooled
  median); never the global value.

Registered controls REQUIRED (the flip-exit precedent):
1. **T_K exposure controls at matched median hold** — trail holds run
   16–20d vs X1's 10–13d; part of the gain is drift exposure. The arm
   must beat the time-blind control, not just X1.
2. Fold consistency ≥ 0.60 across (asset, year); LOYO stability of the
   verdict; bootstrap LB on per-trade mean.
3. Tail bounds vs X1 (worst trade; share ≤ −15%). Same-bar close-fill
   conventions identical across arms.
4. ZEC reporting-only (trail already shown to FAIL there).
5. Secondary registered measurements (no gating): breadth-horizon
   inversion (M5), leg_depth (F3), post-stop re-entry (F11).

Honesty: exploratory expectancies above WILL shrink under registration —
the X1 numbers themselves came in at roughly half their exploratory
analogues. The shadow window remains the final live authority.

## 5. Hydra component review (directive item)

- **Trend overlay / flip exits**: correctly excluded from the S3 path
  (M4) — would gate out the better-continuing half of entries.
- **Heartbeat confirmer**: orthogonal (intraday tape confirmation);
  untouched by this cycle; still gated on the WS soak.
- **R10/derivatives, hold-through, CB**: no interaction with daily-bar
  S3 exits; the 15% CB applies to any future live arm regardless.
- **Kelly/sizer**: F3 depth-sizing idea killed as a gate; sizing stays
  as shipped.
