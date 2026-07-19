# Pre-registration — Cascade-week heartbeat AUC blackout (measurement)

ABI funnel: frame **F3** (options pin / cascade). Measurement only —
display policy candidate, **not** a trade block on S3 or engine.

## Hypothesis

Calibrated bounce+3 AUC collapses to ≤ 0.55 on events within 72h of a
multi-asset washout (breadth ≥ 2 assets making 20d lows), while quiet
weeks retain AUC ≥ 0.70 on the same weights.

## Protocol

1. Real-tape events + calibrated weights (BTC/ETH primary; SOL/ZEC report).
2. Tag cascade if ≥2 of {BTC,ETH,SOL} print a 20d low within ±72h of event.
3. Stratified AUC @ bounce+3.

## Decision rule

| result | consequence |
|---|---|
| cascade AUC ≤ 0.55 **and** quiet AUC ≥ 0.70 | Confirmer **display** may flag `cascade_suspect`; still **no_opinion** only if taint/stale — optional future: force no_opinion in cascade (shadow only) |
| otherwise | No blackout policy; keep logging |

## Result (90d real tape, 2026-07-19)

**NO_BLACKOUT.** Pooled AUC cascade **0.811** (n=66) vs quiet **0.703**
(n=156) — opposite of the hypothesized collapse. Do not implement cascade
no_opinion policy from this window. See
`cascade_week_heartbeat.json`.

## Runner

`heartbeat/tools/cascade_week_heartbeat_auc.py`  
Output: `heartbeat/evidence/bakeoffs/cascade_week_heartbeat.json`
