# ABI Discovery Cycle — Bounce/Heartbeat Edge Funnel (2026-07-18)

> Produced by a dedicated ABI (Anomaly → Bore → Ideate) agent per the
> `/abi-discovery` skill. Every number below was computed in-session
> against `hydra_history.sqlite` or quoted from committed evidence files
> (cited at the bottom). Survivors are candidates only — promotion is
> `/bakeoff`'s job, never this document's.

Scope: `heartbeat/` evidence + `.hydra-flywheel/` gates + `hydra_history.sqlite`
(computations reproduced setup/label semantics by importing
`heartbeat/tools/paper_bounce_sim.py` — `causal_setups`/`simulate`/`stats` —
with `heartbeat/tools/bounce_geometry_study.candles_from_sqlite`).
Fees 26 bps/side (52 bps RT) unless stated. Data note: the `trades` table
holds **only BTC/ETH/SOL vs USD** (90d each, ~4.79M/2.06M/1.84M rows) at
computation time — ZEC analysis here is candle-only (the ZEC 90d trade
backfill completed after this cycle ran).

## 1. Anomaly inventory (quoted from committed evidence)

- **A1 — A "flow" classifier led by a candle-shape feature.** Final real-tape weights (`heartbeat/evidence/real_tape/weights_*.json`): ETH `clv=+0.885` vs `ofi=+0.033`; SOL `clv=+0.552` vs `ofi_momentum=+0.107`; BTC `ofi_momentum=+0.700` with `clv=+0.434` second. CLV needs no trade-side data at all.
- **A2 — Oracle/ALL chasm at 1h.** `bounce_geometry_study.json` (BTC/USD, 4851 setups, 101,155 candles): ALL-entries `b1.all.tgt3.3` loses **every year 2014–2026** (−69.2% to −96.1%/yr; only 2013 +13.2%) while ORACLE earns +35.7% to **+1226.0%** (2017) with win rates 0.64–1.00 vs 0.27–0.45.
- **A3 — Sign flips at daily bars.** `bounce_geometry_1d.json`: unfiltered goes positive in 14 of 50 pair-years — ZEC 2025 **+94.3%** (oracle +308.3%), ETH 2016 +94.0%, BTC 2024 +13.7% — same construction, same fees.
- **A4 — SOL has no flow signal.** `HONEST_FINDINGS.md` + `calibrate_*.txt`: walk-forward calibrated bounce+3 AUC BTC 0.90/0.55/0.84 (mean 0.76 PASS), ETH 0.73/0.77/0.69 (0.73 PASS), SOL 0.62/0.65/**0.40** (0.56 FAIL). In `hydra_bakeoff.json` SOL's fitted `ofi_momentum` goes **negative** (−0.216) with test AUC 0.487.
- **A5 — Confident-wrong fakes are cross-asset synchronized.** The worst-classified events (`eval_*_1h.md`) cluster in 2026-06-02…06-05 on **all three assets** (e.g. `2026-06-02 23:00` appears in BTC, ETH, and SOL worst-5; P(up) 0.993–0.9998, all fakes).
- **A6 — Posterior saturation.** ETH calibration: 53/77 events land in the 0.9–1.0 bin with observed frequency **0.53**; BTC 0.8–0.9 bin obs 0.27. Uncalibrated AUC ~0.55 everywhere.
- **A7 — Fees flip signs.** `paper_bounce_sim.json` (26 bps): every causal arm negative; ETH selected arm at 16 bps maker (`paper_bounce_sim_maker16.json`) turns +0.88% train PF 1.26. Fee is the same order as the whole edge.
- **A8 — Even ORACLE loses with the flow exit.** `paper_bounce_sim.json` BTC train: ORACLE+exitA_flow **−11.67%** (hold 31.8h) vs ORACLE+exitC_tgt **+4.33%** PF 5.57 (hold 7.5h). Exit construction destroys perfect entry selection.
- **A9 — The production system's whole edge is abstention.** `hydra_bakeoff.json`: v2.28 config produced **zero** BUY entries in 90d; `monthly_roi_1y.json`: +0.035% total vs B&H SOL −52.3%/BTC −44.6%; `cb_threshold_sweep.json`: raw-engine loss ≈ CB threshold ~1:1 at every setting ("no threshold flips the cycle to profitable").

## 2. Root-cause mechanisms (each with the verifying computation)

**M1 — At 1h the construction is a zero-gross-edge coin and fees make it a deterministic loser; at daily, fees stop binding and the residual is regime beta.**
Computation 1: per-pair-year median setup ATR% and fee/ATR (52 bps RT ÷ median ATR%): 1h BTC fee/ATR is **0.40–1.26** every year (gross expectancy = avg_ret + 0.52 ≈ −0.6…+0.02%/trade — a coin); at 1d fee/ATR is 0.02–0.22. Every one of the 14 positive pair-years has fee/ATR ≤ 0.22 (mean 0.104 vs 0.357 for losers), but *within* daily the correlation of fee/ATR with expectancy is −0.10 — fees explain the timeframe cliff, not the daily winners.
Computation 2: corr(year B&H, daily bounce avg-ret) = **+0.638** (n=34 pair-years with ≥4 trades); bounce expectancy **+1.09%/trade in up-years vs −3.44% in down-years**. The daily "bounce edge" is mostly the year's trend carried through the payoff.

**M2 — CLV is not integrated taker flow; it is a nearly orthogonal signal.**
Computation: single-pass hourly bars from the real `trades` table (2,161 hours/pair): corr(CLV, net signed taker flow) = **BTC 0.274, ETH 0.176, SOL 0.244** (sign agreement 57–62%). So A1 does not mean "flow leaks into shape" — the classifier's top feature carries information the trade-side features don't, and it is computable on **13 years** of archive candles (4,721 labeled 1h BTC setups) instead of 69–80 events on 90 days.

**M3 — Regime enters through payoff, not hit-rate; per-asset classifiers fail together during leader cascades.**
Computation 1: 90d causal setups per asset — June 1–6 hourly return correlations BTC-ETH 0.88, BTC-SOL 0.91, ETH-SOL 0.89 during a −17% BTC leg; that window contains every worst-classified fake. But fake/reversal cross-clustering is weak (fake 0.65–0.80 vs reversal 0.66–0.72 within ±3h) — the tell is not co-occurrence.
Computation 2: fake-rate by causal trend state: 200d-SMA side changes nothing (0.49–0.55 both sides, all 3 assets, 13y daily); BTC-24h-return sign moves BTC (0.37 vs 0.55) and SOL (0.50 vs 0.60) modestly, ETH not at all. Label base-rate is ~50/50 nearly everywhere — **selection value must come from conditioning the payoff distribution, not predicting the label from trend state.**

## 3. New anomalies generated (fed back into the loop)

- **N1:** CLV ⟂ flow (0.18–0.27) — measured above, feeds F14.
- **N2:** Label base-rate invariance to trend state despite payoff-carried regime — feeds the "underwrite the payoff, not the event" framing.
- **N3 (surprise, inverted from the frame that predicted it):** bounces arriving ≤2 bars after a >2σ daily shock are **better**, not worse: BTC daily avg −0.02% (fk 0.39) vs −1.66% (fk 0.56) stale; ZEC +0.20% (fk **0.25**) vs −2.40% (fk 0.58). Replicates as a *label* effect at 1h on 13y BTC: fake-rate 0.41 fresh vs 0.54 stale (n=3,363 trades) — though 1h fees still eat the P&L (−0.84% vs −0.65% avg).
- **N4:** Contagion breadth is non-monotone — the *middle* is best on all 3 assets (§5, F2).

## 4. Hybrid frames (foreign domain → mapping → falsifiable implication)

Habitual frame: "predict fake vs reversal from order flow at the bounce, gate entries on the posterior." Forced reframes:

| # | Domain | Mapping | Falsifiable implication |
|---|---|---|---|
| F1 | Insurance underwriting (post-cat rate hardening) | ATR expansion = hardened premium; only underwrite when premium (ATR%) rich vs actuarial history | Setups with ATR% ≥ expanding median outperform |
| F2 | Epidemiology (contagion breadth / R₀) | Assets making 20d lows = infected; bounce quality depends on epidemic phase | Expectancy differs by breadth count (0/1/2-3 assets making fresh 20d lows) |
| F3 | Seismology (Omori aftershock decay) | >2σ candle = mainshock; hazard decays in time | Bounce quality is a function of bars-since-shock |
| F4 | Auction theory (winner's curse; second-price) | First bounce = winning vs informed sellers; retest = curse resolved | Retested lows (prior touch within 0.25·ATR) outperform first-touch |
| F5 | Materials fatigue (crack propagation) | Each retest weakens support | Retested lows *under*perform — direct opposite of F4; one measurement kills one |
| F6 | Ecology (succession after wildfire) | Alts = pioneer species; BTC = climate; recovery needs climate stability | ETH/ZEC bounces conditional on BTC making no fresh 20d low outperform |
| F7 | Queueing theory (server saturation) | Panic churn = arrival rate > service rate | Trade-count z at bounce hour separates fake from reversal on real tape |
| F8 | Sports physiology (pitch-count fatigue) | Down-leg length = seller fatigue | Long legs (≥8 bars below MA9) bounce better |
| F9 | Foraging (marginal value theorem) | Leave patch when gain rate < ambient average | Fixed time-boxed exits beat target/stop constructions |
| F10 | Casino economics (table selection > bet sizing) | Pair-year is the table; entry skill is play | Only playing the current top-vol (TR%/fee) asset flips expectancy |
| F11 | Options (insurance IV vs RV) | Bounce ≈ short-put replication; sell only when implied > realized | Needs options IV data — **not testable with data on hand** |
| F12 | Shift work / labor economics | Liquidity provision runs in shifts; thin shifts fail | Fake-rate varies by UTC session / weekend |
| F13 | Immunology (sentinel patient) | BTC resolves first; its last resolved bounce label immunizes/warns alts | Alt bounces after a BTC *reversal* resolution outperform |
| F14 | Remote sensing (photometry without spectroscopy) | CLV = photometric signature; trade side = spectroscopy you rarely have | Candle-only classifier trained on 13y ≥ flow classifier trained on 90d |

## 5. Kill-test results (all run against `hydra_history.sqlite` unless marked)

Test harness: `b1.all.tgt3.3` trade simulation (entry bounce+1 close, target 3.3·ATR, stop at low, 52 bps RT), pools split by the frame's causal condition; fk = oracle-label fake-rate of the pool.

| Frame | Result | Verdict |
|---|---|---|
| F1 insurance | Daily, 3 pairs: hard-market pool no better (BTC −1.39% vs −1.08%; ZEC −2.23% vs −1.10%; ETH +0.61% vs −0.84% — 1/3, inconsistent) | **KILLED** |
| F2 epidemiology | Non-monotone, consistent on 3/3: breadth1 best everywhere — BTC **+0.04%** (fk 0.45) vs −1.55/−1.78 at breadth 0/2+; ETH **+3.52%** (fk 0.33, n=21) vs −0.99/−2.38; ZEC **−0.43%** (fk 0.44) vs −1.00/−4.59. Full-epidemic (2-3) worst, as predicted; solo weakness (0) also bad | **SURVIVES** (small n mid-bucket) |
| F3 seismology | *Inverted* but real: fresh-shock (≤2 bars) beats stale on BTC daily (−0.02% fk 0.39 vs −1.66% fk 0.56) and ZEC (+0.20% fk 0.25 vs −2.40% fk 0.58); ETH contradicts (−2.72% vs −0.43%). Label effect replicates at 1h BTC 13y: fk 0.41 vs 0.54 (n=3,363) — P&L doesn't (fees). Interaction fresh∧retest: ZEC +24.5% wr 0.83 but n=6 (anecdote) | **SURVIVES, inverted** ("buy capitulation, not erosion"); ETH is the falsification control |
| F4 auction | Retest helps ETH (+0.18% vs −0.71%) and ZEC (−0.05% vs −4.53%), hurts BTC (−1.20% vs −0.79%) — 2/3 | **MARGINAL** (keep only as a feature inside F14) |
| F5 fatigue | Same measurement, opposite prediction — falsified on ETH/ZEC | **KILLED** |
| F6 ecology | No effect / wrong direction (ETH stable −0.03% vs unstable −0.35%; fk higher when "stable") | **KILLED** |
| F7 queueing | Real-tape trade-count z: AUC 0.528/0.529/0.526 (BTC/ETH/SOL); direction consistent (rev z +0.25 vs fake −0.04) but useless | **KILLED** |
| F8 sports | Long legs *worse* (BTC −1.77% vs −1.17%; all 3 same sign) — exhaustion falsified | **KILLED** |
| F9 foraging | Time-boxed exits all negative: daily k=1..10 avg −0.98…+0.37% (no pair-k combination profitable net); 1h k=6/12/24 → −100% total | **KILLED** — exits are not the missing edge |
| F10 casino | Top-vol asset not better (ZEC when top-vol −2.24% vs −0.14% when not; BTC n=2) | **KILLED** (inverted hint ignored — multiple-comparison bait) |
| F11 options | No IV data in repo | **NOT RUN** (honest gap) |
| F12 shift work | Fake-rate 0.51–0.53 and avg −0.55…−0.74% across all sessions/weekend, 13y BTC 1h | **KILLED** |
| F13 sentinel | ETH: −0.45% vs −0.89%; ZEC −2.32% vs −3.22% — noise-level | **KILLED** |
| F14 remote sensing | Bounce-candle CLV alone: AUC > 0.5 in **13 of 14 years** (pooled 0.567, n=4,721; 2026 = 0.648), BTC 1h 13y. Weak alone, but the only signal with 13 years of stability, and orthogonal to flow (M2) | **SURVIVES** as training-set-extension play |

Multiple-comparisons warning (honest): ~12 frames × 3 assets were tested; the best single cells (ETH breadth1 +3.52%, ZEC fresh-shock fk 0.25) are exactly what noise mining produces. That is why survivors go to pre-registered `/bakeoff`, not to code.

## 6. Ranked survivors + pre-registered /bakeoff designs

**S1 — Capitulation-freshness gate (F3, inverted Omori).** The one signal that replicated across two timeframes and 2/3 assets on both label and payoff, with a mechanism (forced-liquidation overshoot vs grinding distribution) that explains why trend state doesn't show in label base-rates.
*Bakeoff design:* daily bars, BTC+ZEC (ETH pre-registered as expected-fail control); arms = {all-setups, fresh≤2-gated, stale-only (inverse control), ORACLE ceiling}, entry b1, exit tgt3.3, 26 bps; walk-forward yearly folds 2016→2025, gate parameters (2σ, ≤2 bars) frozen now — **no sweeps**. Promote iff gated total > 0 on ≥60% of folds AND gated − ungated > +1%/trade pooled AND inverse arm worse than ungated. Data: existing sqlite only.

**S2 — Middle-breadth contagion filter (F2).** Consistent sign on 3/3 assets, non-monotone as the epidemic frame predicts, and composable with S1 (both are "which kind of selloff is this" classifiers using no flow data).
*Bakeoff design:* same harness as S1; arms = breadth-1-only vs all vs breadth-{0,2,3} (inverse); breadth definition frozen (20d low within 3 bars, 3-asset universe). Promote iff breadth-1 pooled expectancy > 0 and beats both complements per-asset on ≥2/3 assets. Risk flagged up front: mid-bucket n ≈ 21–35/asset — likely underpowered; if inconclusive, fold breadth in as a feature of S3 instead of a standalone gate.

**S3 — Candle-only classifier trained on 13 years (F14 + M2).** Replace the 90d/69-event flow-fit with a logistic on candle-computable features only (`clv`, `range_atr`, `vol_z`, shock-recency, breadth, retest) over 4,721 BTC 1h + 182 daily labeled setups, walk-forward by year; heartbeat's real-tape flow posterior then becomes a *second-stage confirmer* on BTC/ETH only (where it passed its gate). Rationale: 50× more training events, and M2 proves the two information sources are orthogonal (corr ≤ 0.27), so stacking is legitimate.
*Bakeoff design:* pre-register feature list (above, frozen), yearly expanding-window folds 2016→2026, metric = calibrated AUC at bounce+3 AND gated `b1.tgt3.3` daily P&L at the p75 train-posterior threshold; promote iff walk-forward AUC ≥ 0.60 pooled and gated daily expectancy > 0 net of 26 bps on ≥2 of 3 assets, inverse gate worse. This is the direct successor to HONEST_FINDINGS' "dedicated bounce-entry strategy" recommendation and stays paper until it clears.

Not promoted, recorded: everything in §5 marked KILLED is a negative result worth keeping (esp. F9: *no* fixed-time exit rescues the 1h construction — the 1h bounce family is dead at taker fees, consistent with fee/ATR 0.4–1.26, and should not be revisited).

---

**Files cited:** `heartbeat/HONEST_FINDINGS.md`, `heartbeat/evidence/bounce_geometry_study.json`, `bounce_geometry_1d.json`, `paper_bounce_sim.json`, `paper_bounce_sim_maker16.json`, `hydra_bakeoff.json`, `real_tape/{weights,calibrate,eval}_*`, `.hydra-flywheel/{cb_threshold_sweep,monthly_roi_1y,trend_overlay_gate,trend_entry_gate,bridge_isolation}.json`; harness reused verbatim from `heartbeat/tools/paper_bounce_sim.py`. All numbers come from the quoted evidence files or from computations executed against `hydra_history.sqlite`.
