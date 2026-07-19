# ABI Discovery Funnel — "Stops are harvested; abstention + monitored exits are the risk layer" (2026-07-18)

> Produced per `/abi-discovery` by a dedicated agent against local data only.
> Every §2/§3/§5 number was computed in-session against `hydra_history.sqlite`
> (reusing `heartbeat/tools/paper_bounce_sim.py` setup/entry semantics
> verbatim; fees 26 bps/side = 52 bps RT) or is quoted from the committed
> evidence files cited inline. The computation scripts are committed as
> `heartbeat/tools/stophunt_control_study.py` (C1),
> `heartbeat/tools/exit_layer_lab.py` (C3), `heartbeat/tools/avalanche_burst_study.py`
> (C4), and `heartbeat/tools/overlay_vs_bh_study.py` (C6).
> Survivors are candidates only — promotion is `/bakeoff`'s job.

**Headline: the hypothesis is half right for the wrong reason.** Stops do lose here — but not because they are "harvested by intentional market geometry" (that mechanism largely failed its controls). They lose because they force short holds in fee-dominated, drift-carried tape, and their advertised risk cap is fictional on gap bars. And the "AI-monitored exit" arm wins — but the placebo control shows the entries contribute nothing: the monitored exit is not a risk layer on top of a strategy, it *is* the strategy (trend beta with drawdown control), and it has a named failure asset (ZEC).

## §1 Anomaly inventory (committed evidence, quoted)

| # | Anomaly | Numbers | Source |
|---|---|---|---|
| A1 | Exit construction destroyed *perfect* entries at 1h — but in the direction OPPOSITE the hypothesis | BTC 1h train: ORACLE + trailing flow exit **−11.67%** (hold 31.8h) vs ORACLE + fixed target **+4.33%**, PF 5.57 (hold 7.5h) | `ABI_FUNNEL_2026-07-18.md` (A8), from `paper_bounce_sim.json` |
| A2 | On ZEC 90d OOS the sign flips: every flow/trailing exit arm is positive — **even with inverse-gated (worst-classified) entries** — while every target/stop arm loses | test `b1.all.exitA_flow` **+13.67%** (PF 1.67, hold 28.8h); `b1.inverse_p50.exitA_flow` **+7.28%**; `b3.all.exitA_flow` **+30.18%**; vs `b1.all.exitC_tgt` **−20.86%** (17/30 exits = stop), `b3.all.exitC_tgt` **−20.44%**; B&H test **+33.16%** | `paper_bounce_sim_ZEC.json` |
| A3 | Stop exits dominate every losing arm; pooled 2013–2026 the tgt3.3 arms end 54–60% of trades at the stop and lose every asset | 1d `b1.all.tgt3.3` avg/trade: BTC **−1.344%** (stop-frac 0.60), ETH **−0.413%** (0.58), ZEC **−1.953%** (0.60); ORACLE ceilings +8.3/+14.2/+17.6%/trade | aggregated in-session from `bounce_geometry_1d.json` |
| A4 | But trailing exits do NOT rescue the unfiltered pools either | 1d `b1.all.trail1.5`: BTC −0.438%, ETH −0.363%, ZEC −2.017%; 1h BTC all three exits ≈ −0.31…−0.34%/trade, 0/14 positive years | same files; `bounce_geometry_study.json` |
| A5 | The production system's whole 90d edge is abstention | every bakeoff arm: `fills: 0` over 2160 candles (SOL+BTC, 90d); +0.035% 1y vs B&H SOL −52.3%/BTC −44.6% | `hydra_bakeoff.json`; `ABI_FUNNEL_2026-07-18.md` (A9) |
| A6 | The "AI-monitored exit" analog already won 6/6 real-tape windows | overlay ON/OFF: 1y ret **+0.01 vs −0.39**, dd 0.02 vs 0.43; 2y **+0.01 vs −0.52**, dd 0.25 vs 0.93; 3y **+0.30 vs +0.22**, dd 0.52 vs 1.83 | `.hydra-flywheel/trend_overlay_gate.json` |
| A7 | A portfolio-level stop produces loss ≈ threshold, at every threshold | raw engine: cb5 −6.19, cb8 −6.85, cb10 −9.80, cb12 −12.19, cb15 −15.25, cb20 −20.01; final config: identical 0.09/dd 1.05 from cb5→cb1000 | `.hydra-flywheel/cb_threshold_sweep.json` |
| A8 | Fees are the same order as the whole 1h edge | 1h fee/ATR **0.40–1.26** every year (coin); daily 0.02–0.22; no fixed-time exit rescues 1h (F9 KILLED) | `ABI_FUNNEL_2026-07-18.md` (M1, F9) |

The tension motivating this cycle: A2 (stops lose / loose exits win on ZEC OOS) vs A1 (fixed target beats trailing for oracle entries at 1h) — the hypothesis cannot be globally true.

## §2 Mechanisms bored, with verifying computations

**M1 — Stops/targets lose because they force short holds in drift-carried, fee-dominated tape — not because the stop level is special.**
Verification (arithmetic on `paper_bounce_sim_ZEC.json`): test segment = 1781312400→1784422800 = **864h**, B&H +33.16% ⇒ log-drift **0.033%/h**. The winning `exitA_flow` arms hold 28.8–79.6h ⇒ **+0.95…+2.6%/trade of pure drift**, matching their +0.64…+4.37% averages. The losing tgt/stop arms hold 5.6–6.2h ⇒ drift ≈ **+0.21%**, below the 0.52% RT fee — structurally negative before any selection, and the stop then converts intrabar noise into realized loss (observed −0.75…−1.0%/trade). Cross-checked on 13y in the exit lab (§3, arms A0 vs A4): identical entries, stop+target = −81…−99% total on all three assets; ensemble-flip exit (long holds) = +508% BTC, huge ETH. **Accepted.**

**M2 — There is no privileged "harvest zone": pierce-then-recover is generic level-recross, only weakly enhanced at swing-low anchors.**
Verification (C1, `tools/stophunt_control_study.py`: 14,070 BTC-1h swing lows + 3× random-anchor control, first pierce of `low − d·ATR` within 200 bars, recovery = close back above the anchor low within 5 bars):

| BTC/USD 1h | pierce prob (anchored/control) | recover-after-pierce (anchored/control) |
|---|---|---|
| d=0.25 ATR | 0.853 / 0.918 | **0.716 / 0.669** |
| d=1.00 ATR | 0.789 / 0.854 | 0.469 / 0.438 |
| d=2.00 ATR | 0.703 / 0.770 | 0.255 / 0.243 |

Same pattern on ETH/ZEC/SOL 1h and BTC/ETH/ZEC 1d (35 cells): anchored excess recovery is **+3 to +7 pts, everywhere, at every distance** — a real but second-order liquidity effect. The first-order fact is that *any* shallow level, anchored or random, is pierced ~85–94% of the time and recovers 60–77% of the time. Tight stops die of generic churn, not sniper fire. Corroborated by the stop-distance ladder (§5 F2): moving the stop 0.5→4 ATR away never restores positive expectancy — there is no distance that "escapes the hunters". **Accepted (and it kills the strong "intentional geometry" form of the hypothesis).**

**M3 — A stop on a negative-expectancy stream is a loss *floor*, not loss *prevention*.**
Verification (arithmetic on `cb_threshold_sweep.json`): realized-loss/threshold = 6.19/5 = **1.24**, 6.85/8 = 0.86, 9.80/10 = 0.98, 12.19/12 = 1.02, 15.25/15 = 1.02, 20.01/20 = **1.00**. You lose approximately what the stop permits, at every setting. The breaker has value only as a dormant backstop under a profitable policy (final config: dd 1.05%, CB immaterial from 5%→1000%). **Accepted.**

**M4 — Abstention has informational value because conditional drift differs by regime state — on 2 of 3 assets.**
Verification (C6, `tools/overlay_vs_bh_study.py`: pure ensemble overlay — long at close when daily score ≥0.6, flat below; 0.4·sma200 + 0.4·ema20×100 + 0.2·don55/20, no entries, no stops, 26 bps/side): BTC 12y **+21,980% (maxDD 63.9%, 29 round trips)** vs B&H +14,966% (maxDD 83.6%); ETH **+50,485% (72.4%)** vs +18,983% (94.1%); **ZEC −60.9% (98.2%) vs B&H +177.4%** — the overlay is destroyed by a secular-decline-with-violent-rallies asset (46 whipsaw round trips, wr 0.20). **Accepted, with ZEC as the standing falsification.**

## §3 New anomalies generated (exit-layer lab, `tools/exit_layer_lab.py`: identical harness entries, only the risk layer varies; BTC/ETH/ZEC 1d + BTC 1h)

- **N1 — The entries contribute nothing; the "risk layer" is the whole strategy.** Placebo entries (every 10th bar, no construction at all) under gate+flip perform as well as bounce entries under gate+flip: BTC 1h **+2.04%/trade (n=284, total +6,553%)** vs bounce **+1.89%/trade (n=264, +3,027%)**; BTC 1d +30.2 vs +41.3%/trade; ETH 1d +156 vs +42%/trade. The celebrated "AI-monitored exit" arm is regime beta wearing whatever entries you hand it (confirmed by M4's pure overlay). This dissolves the hypothesis's framing: it isn't "smarter risk management for your trades" — it's a different strategy that ignores your trades.
- **N2 — Close-confirmation is the only stop repair that replicates.** Exit-at-close-when-close<L0 vs touch-stop, same entries: BTC 1d **−0.996 vs −1.634**, ETH 1d −0.066 vs −0.653, ZEC 1d −0.397 vs −2.227, BTC 1h −0.589 vs −0.686 %/trade — better on **4/4 datasets**, sign flipped on 0/4.
- **N3 — The deductible ladder raises win rate and buys nothing.** Stop at L0−m·ATR, m∈{0.5,1,2,4}: BTC 1d wr 0.38→0.45→0.49→0.57→0.65 while avg/trade stays −1.0…−2.2%; BTC 1h avg pinned at −0.61…−0.71% across the whole ladder. Expectancy is conserved — there is nothing at any distance to stop being "harvested" of.
- **N4 — Stops do not even cap the tail.** A0's stop sits 0 ATR below the entry low, yet max single-trade losses are **−22.6% (BTC 1d), −36.6% (ETH 1d), −50.5% (ZEC 1d)** — gap bars fill at `min(close, L0)`, far below the level. The advertised risk cap is fictional exactly when it matters.
- **N5 — Heavy sell-taker bursts at 20-bar-low breaks revert within 6h on 3/4 assets** (C4, 90d sided tape, `tools/avalanche_burst_study.py`): fwd-6h heavy-burst vs quiet-break: BTC **+0.159 vs −0.102%**, ETH +0.238 vs −0.149%, SOL +0.304 vs −0.060%; ZEC contradicts (−1.15 vs −0.70%); effect gone by 24h. Real cascade-exhaustion asymmetry, but ~25–40 bps gross < 52 bps RT fee — signal-input grade only.

## §4 Hybrid frames (foreign domain → mapping → falsifiable implication)

Habitual frame: "protect each trade with a price stop; or replace it with an AI exit." Forced reframes:

| # | Domain | Mapping | Falsifiable implication |
|---|---|---|---|
| F1 | Cybersecurity honeypots | Swing-low stops = credentials left where the attacker expects; harvesting requires anchored levels recrossed far more than arbitrary ones | Pierce→recover excess at swing-low anchors ≫ random-level control, peaking in a distance band |
| F2 | Insurance deductible / retention theory | Tight stop = zero-deductible policy on every wiggle; optimal retention self-insures small losses | Expectancy improves steeply as the stop moves out of the "claim zone", then plateaus |
| F3 | Auction sniping (eBay late bidding) | Intrabar touch-stop = early bid revealing your reservation price; acting at the close = sniping with full information | Close-confirmed stops beat touch-stops on identical entries, consistently |
| F4 | Aviation envelope protection (fly-by-wire) | Stop = ejection seat; envelope protection limits attitude (regime gate + flatten) so ejection is never pulled | No-stop + regime-flip flatten beats stop+target on identical entries, with smaller equity maxDD |
| F5 | Epidemiology quarantine | Enter only in permissive regime = quarantine before infection; stops = treating the infected | Regime-gating entries improves every exit type; gating + monitored exit goes positive where stops can't |
| F6 | Wildfire suppression paradox (inverse control) | Refusing all exits = suppressing every burn until the megafire | No-exit arm shows catastrophic left tail vs any exit — the bound stops must beat |
| F7 | Control theory: bang-bang vs proportional | Binary stop = bang-bang controller; scale out = proportional control | 50% at L0 / 50% at L0−2ATR beats full exit at L0 |
| F8 | Avalanche mechanics (slab vs sluff) | Heavy sell-taker burst at a level break = slab release that exhausts the start zone | Heavy-burst breaks revert faster than quiet (erosive) breaks on sided tape |
| F9 | RCT placebo arm (clinical medicine) | The risk layer is the drug; entries are the patient cohort — test the drug on placebo entries | If gate+flip works equally on arbitrary entries, the "smart exit" is not exit skill but regime beta |
| F10 | Pharmacology: taper vs cold turkey | Regime-flip flatten = cold turkey; 3-bar scale-out = taper avoiding rebound | Tapered flip exit beats instant flatten |
| F11 | Materials science: brittle vs ductile failure | Touch-stop = brittle fracture at the stress concentration (fills at the worst print, gaps through); sizing/regime = ductile deformation | Stop arms' realized max loss ≫ advertised stop distance (gap risk); ductile arms cap equity DD better |
| F12 | Search theory / reservation wage (McCall) | Abstention = rejecting offers below the reservation wage; only rational if the offer distribution is state-dependent | Conditional drift long-vs-short ensemble states differs materially; else abstention is superstition |

## §5 Kill-test results

| Frame | Cheapest kill-test result | Verdict |
|---|---|---|
| F1 honeypot | C1: anchored excess recovery only +3…+7 pts over random control, flat in distance, all 7 datasets; base recross 60–77% everywhere | **KILLED** as "intentional harvest"; survives only as a small generic liquidity effect |
| F2 deductible | N3 ladder: wr 0.38→0.65 with expectancy pinned negative; no plateau, no escape distance; 1h flat −0.6…−0.7% across ladder | **KILLED** |
| F3 sniping | N2: close-confirm better 4/4 (Δ +0.10…+1.83%/trade), sign flipped 0/4 | **SURVIVES** as a repair (never a strategy) |
| F4 envelope protection | A0 vs A4/A5: BTC 1d −1.63 → **+5.30**%/trade; ETH −0.65 → +38.1; BTC 1h A5 **+1.89%/trade, n=264, +3,027% total at taker fees** (hold 188 bars); equity maxDD 94→59% (BTC 1d). ZEC 1d **fails: A5 −6.68%/trade** | **SURVIVES**, with ZEC as named failure mode and F9's confound attached |
| F5 quarantine | Gate improves every exit: A6 (gate+stop) −0.67 vs A0 −1.63 (BTC 1d) — better but **still negative with stops**; positive only with flip exit (A5) | **SURVIVES folded into F4** — abstention alone insufficient, must pair with the monitored exit |
| F6 wildfire (inverse) | A9 no-exit: avg +53%/trade (BTC 1d) but maxloss −65.9…−71.8%, MAE −28…−44%, eqMaxDD 69–95% | **CONFIRMED as bound**: some exit is mandatory; flip exit already beats stops at the tail-control job (maxloss −21…−35%) |
| F7 proportional | A7: BTC 1d −2.15 (worse than A0), ETH −0.10, ZEC −0.76; never positive, inconsistent | **KILLED** |
| F8 avalanche | N5: 3/4 assets +25…+40 bps @6h, gone @24h, ZEC contradicts; below RT fee | **KILLED** for standalone monetization; note as possible exit-timing signal input |
| F9 placebo | N1: placebo entries ≈ or > bounce entries under gate+flip on all 3 positive datasets | **SURVIVED (inverted)** — the damning control: exit "skill" = regime beta, entries irrelevant |
| F10 taper | A8 vs A4: BTC +5.77 vs +5.30, ETH +18.6 vs **38.1**, ZEC +0.83 vs −0.14 — 2/3, inconsistent, small deltas | **KILLED** |
| F11 brittle/ductile | N4: realized maxloss −22.6/−36.6/−50.5% (1d) with a 0-ATR stop — cap is fictional on gaps; flip arms cap eqMaxDD better | **CONFIRMED as diagnostic** (feeds "where stops are right/wrong") |
| F12 reservation wage | M4/C6: overlay vs B&H — BTC +21,980%/dd 64 vs +14,966/84; ETH +50,485/72 vs +18,983/94; **ZEC −60.9 vs +177.4** | **SURVIVES 2/3** — abstention's value is real, state-dependent, and already shipped as `HYDRA_TREND_OVERLAY` |

**Multiple-comparisons warning (honest).** This cycle evaluated ~12 frames × up to 4 datasets × 13 exit arms ≈ 500+ cells, plus a 5-point stop-distance grid and a 5-point pierce-distance grid. The most spectacular single cells (ETH 1d A4 "+157,622% total", BTC 1d A5 +41%/trade on n=16) are exactly what compounding regime beta over a 2016–2021 bull plus noise mining produce, and are **not** treated as evidence. Only effects consistent in sign across ≥3 independent datasets (N1, N2, M2's anchored excess, F4's direction) were allowed to survive, and survivors go to pre-registered `/bakeoff` with frozen parameters — nothing is promoted here.

## §6 Ranked survivors — pre-registered /bakeoff designs

**S1 — Close-confirmed exits replace touch-stops (F3/N2).** The only stop-side repair that replicated 4/4. Mechanism-backed (M2: wick pierces recover 60–77%; anchored excess +3–7 pts).
*Pre-registered design:* harness `b1` entries, BTC/ETH/ZEC 1d + BTC 1h, yearly folds 2016→2026 (2013–15 BTC included), **frozen**: exit at bar close when close < L0, target 3.3·ATR, horizon 200, 26 bps. Three arms isolate the mechanism: (i) touch-stop filled `min(close, L0)` (baseline A0); (ii) *mechanical control*: exit at close whenever **low** < L0 (touch trigger, close fill — separates fill mechanics from confirmation); (iii) close-confirm (trigger and fill on close < L0). **Inverse control:** arm (iii) with exit on close < L0 evaluated one bar *late* (stale confirmation) must give back the gain. **Promote iff** (iii) − (i) ≥ +0.3%/trade pooled AND (iii) > (ii) on ≥60% of folds (proving it's confirmation, not fills) AND the effect holds per-asset on ≥3/4. Scope on promotion: any level-triggered SELL path in HYDRA research code — *not* a reason to add stops to production, which has none.

**S2 — Envelope-protection risk layer for satellite pairs (F4+F5+F12, carrying F9's confound as a design constraint).** The honest statement of the user's hypothesis that survived: *no price stop; abstain unless the daily ensemble is long; flatten on flip* — knowing this is regime beta, not exit skill (N1), and that it already exists in production for the triangle (`HYDRA_TREND_OVERLAY`, 6/6 windows). The open, promotable question is the **1h-execution satellite version**: gate+flip cleared taker fees at 1h (+1.89–2.04%/trade, n=264/284, the only positive 1h construction ever found in this program).
*Pre-registered design:* assets BTC, ETH, SOL 1h (SOL = out-of-construction asset), **ZEC 1h pre-registered as expected-FAIL control** (M4); frozen params: ensemble 0.4·sma200 + 0.4·ema20×100 + 0.2·don(55/20 close-based), long ≥0.6, act at first 1h close after the prior completed UTC day's score crosses; entries = every 10th bar placebo (deliberately — N1 says construction is irrelevant, so pre-register the cheapest); no price stop, 15% CB retained as dormant backstop; 26 bps. Folds: expanding-window yearly 2016→2026. Arms: (a) gate+flip, (b) B&H exposure-matched (long the same fraction of time, random placement — the beta control), (c) **inverse gate** (enter only score <0.6, flip-up exit — must lose), (d) always-in B&H. **Promote iff** (a) > (b) pooled AND on ≥60% of folds AND maxDD(a) < 0.7·maxDD(d) AND (c) loses AND ZEC fails as predicted (if ZEC *passes*, the mechanism story is wrong and everything returns to the funnel). Explicitly *not* promotable as "AI exits rescue bounce entries" — N1 forbids that reading.

Not promoted, recorded as kills: F1, F2, F7, F8, F10; plus the strong form of the user hypothesis — "stop-based risk management invariably gets liquidated/harvested by intentional market geometry" — is **rejected on the controls**: the random-anchor control absorbs most of the recovery effect (M2), and no stop distance escapes (N3), which is incompatible with targeted harvesting and fully compatible with fees + churn + drift (M1).

## Where stops are right (required honesty)

1. **Short-horizon edge, banked fast:** with oracle-grade 1h entries, fixed target+stop **beat** the monitored/trailing exit (+4.33% PF 5.57 vs −11.67%) and ORACLE.tgt3.3 out-earns ORACLE.trail per-trade on BTC 1h (+2.62 vs +2.04%/trade) and ZEC 1d (+17.6 vs +15.2). If you genuinely predict the next few bars, the stop/target pair converts prediction into cash before drift and fees dissolve it.
2. **As a catastrophe floor on a broken policy:** the CB sweep shows loss ≈ threshold — tautological, but that *is* the job: it converts unbounded ruin into a chosen number. Keep the 15% breaker dormant, exactly as production does.
3. **Against the no-exit megafire:** A9's −66…−72% single-trade tails are the bound any risk layer must beat. Stops do beat *nothing* — they just lose to the flip exit at the same job, and their cap is gap-fictional (N4), so they must be sized as soft expectations, not guarantees.
4. **Defined-risk sizing:** a stop distance is still the cleanest per-trade risk denominator for Kelly-style sizing; removing stops shifts that burden entirely onto vol-target sizing (`HYDRA_TREND_CONVICTION_SIZING` already does this in production).

## Files used

Evidence read: `heartbeat/evidence/ABI_FUNNEL_2026-07-18.md`, `paper_bounce_sim_ZEC.json`, `bounce_geometry_1d.json`, `bounce_geometry_study.json`, `hydra_bakeoff.json`, `.hydra-flywheel/trend_overlay_gate.json`, `.hydra-flywheel/cb_threshold_sweep.json`. Code reused: `heartbeat/tools/paper_bounce_sim.py` (`causal_setups`/`entry_index`/FEE), `heartbeat/tools/bounce_geometry_study.py` (`candles_from_sqlite`), ensemble definition frozen from `hydra_engine.py`. Data: `hydra_history.sqlite` (`ohlc` 1h 2013–2026 BTC/ETH/ZEC/SOL; `trades` 90d sided). Computation scripts committed under `heartbeat/tools/` (see header). Known data caveat: ZEC 1h has a healed gap (2025-12→2026-04) spanned by resampled bars; ZEC daily results after 2025 carry that caveat.
