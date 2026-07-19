---
name: abi-discovery
description: Use when asked to find NEW sources of edge, "improve profitable outcomes", explore strategy ideas, or run an ABI cycle — and whenever research is stuck iterating parameters on a construction that keeps losing (threshold sweeps, exit tweaks, timeframe shifts on the same idea).
---

# ABI Discovery — Anomaly → Bore → Ideate (bisociative)

Divergent discovery cycle for trading-edge research. Counter-programs
the default failure mode: convergent parameter iteration on a losing
construction. Grounded in Koestler's bisociation — a new idea is two
habitually incompatible frames connected, not a neighbor of the old one.

**ABI cycles generate candidates; they never bypass evidence gates.**
Every surviving frame exits into `/bakeoff` (pre-registered, real data).

## The cycle

1. **Anomaly inventory.** List everything in the EVIDENCE (gate JSONs,
   `heartbeat/evidence/`, `.hydra-flywheel/*.json`, HONEST_FINDINGS,
   backtest results) that is surprising, asymmetric, or contradicts the
   design story. Not opinions — numbers that don't fit. (e.g. "the
   'flow' classifier's top weight is CLV, a candle-shape feature";
   "oracle P&L is huge while ALL-entries is catastrophic"; "SOL has no
   flow signal but BTC does".)
2. **Bore (root-cause probing).** For the 2-3 sharpest anomalies, ask
   why until the mechanism is structural: fees-vs-ATR ratios, class
   balance, regime composition, liquidity tiers, who is on the other
   side of the trade. Verify each proposed mechanism with one cheap
   computation against stored data before accepting it.
3. **Generate new anomalies.** Each root cause predicts something not
   yet measured. Run the cheap measurement; new surprises feed back
   into step 1 (one loop minimum).
4. **Bisociate / Reframe.** State the problem in its habitual frame,
   then force 8-15 HYBRID FRAMES by fusing the mechanism with unrelated
   domains (insurance, ecology, queueing, epidemiology, auctions,
   materials science, sports, logistics...). A valid frame names: the
   foreign domain, the mapping, and the testable trading implication.
   "Trailing stop but tighter" is not a frame; "bounce entries as
   insurance underwriting — price the premium (stop distance) only when
   implied vol overpays actuarial loss (realized ATR distribution)" is.
5. **Falsification funnel.** For every frame: one cheap kill-test
   against data already in `hydra_history.sqlite` / the tape store
   (minutes, not hours). Kill most. 1-3 survivors get pre-registered
   `/bakeoff` runs. Record kills in the evidence dir — negative results
   are output, not waste.

## Rules

- Anomalies come from committed evidence files, never from memory or
  narrative. Quote the number.
- A root cause is accepted only with a verifying computation attached.
- Frames without a named foreign domain + falsifiable implication don't
  count toward the 8-15.
- The cycle output is a ranked funnel document (anomalies → mechanisms
  → frames → kill-test results → survivors), committed alongside the
  evidence it cites.
- Never promote a survivor on funnel results — that is `/bakeoff`'s job.

## Red flags — you are back in convergent mode

- Sweeping a threshold/exit/timeframe on the same construction twice
- Every "new idea" shares the losing construction's entry geometry
- No frame names a domain outside markets
- Kill-tests being skipped because a frame "obviously" works
