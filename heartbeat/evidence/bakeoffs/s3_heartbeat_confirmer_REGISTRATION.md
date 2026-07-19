# Pre-registration — S3 × heartbeat confirmer (2026-07-19)

ABI funnel: `heartbeat/evidence/ABI_FUNNEL_2026-07-19.md` frame **F2**
(two-stage medical assay). Thesis: S3 daily logistic is the screen;
calibrated 1h heartbeat posterior is a **confirmatory assay on BTC/ETH
only**. This gate measures whether confirmer filtering improves the
**shadow book** — it does **not** authorize live orders or engine BUY
gates.

## Honesty preamble

- Heartbeat real-tape promote already known (BTC/ETH PASS, SOL/ZEC FAIL).
- S3 X1 basis already known (+1–3%/trade gated).
- Live co-logging fields `decision_s3_only` / `decision_s3_plus_confirmer`
  exist in `hydra_s3.shadow_step` but have **no P&L bakeoff** yet.
- Engine+HB entry bakeoff was **structurally inconclusive** (0 BUYs under
  rails) — this registration is **S3-book only**, not engine path.
- No live SKIP/force_hold may be shipped from a FAIL or underpowered result.

## Frozen design

| item | freeze |
|---|---|
| Universe | BTC/USD, ETH/USD only (ZEC reporting-only / never gates) |
| Screen | Frozen `s3bounce` artifact; gated `entryable_b1` (train-p75) |
| Exit | **X1 only** (close-fill stop L0 / tgt 3.3·ATR / 200-bar horizon) |
| Fees | 26 bps/side (research convention) |
| Confirmer | Calibrated heartbeat weights from `evidence/real_tape/weights_*`; p_up at the 1h candle whose close aligns with entry decision time |
| Fail-open | Missing/tainted/stale posterior → treat as **keep** (same as S3-only), recorded as `no_opinion` coverage |
| Threshold | Primary θ = **0.50** (matches shipped shadow `p_up >= 0.5`); secondary report θ ∈ {0.45, 0.55, 0.60} train-free absolute (direction check only — not for promotion if only one wins) |
| Data | Real tape + `hydra_history.sqlite` daily fold of 1h; window = intersection of tape coverage and S3 bars |

## Arms (identical pools except confirmer filter)

| id | rule |
|---|---|
| A_s3_only | All gated entryable_b1 → X1 |
| B_s3_plus_hb | Keep only if confirmer status ok **and** p_up ≥ 0.50 |
| C_inverse | Keep only if confirmer ok **and** p_up < 0.50 |
| D_random50 | Keep each entry with p=0.5 RNG seed=42 (power / trade-count control) |

## Registered criteria (promote confirmer as **shadow filter recommendation**)

On BTC+ETH pooled X1 trades:

- **C1 stop-rate:** stop_rate(B) ≤ stop_rate(A) − 0.10 (absolute).
- **C2 expectancy floor:** avg%/trade(B) ≥ avg%/trade(A) − 0.30 pp.
- **C3 direction:** stop_rate(C) ≥ stop_rate(A) (inverse not better).
- **C4 not noise:** |n(B) − n(D)| does not explain C1 alone — require
  stop_rate(B) ≤ stop_rate(D) − 0.05 **or** avg(B) ≥ avg(D).
- **C5 coverage:** fraction of A entries with confirmer status=ok ≥ 0.50;
  else **INCONCLUSIVE** (fail-open coverage too low).
- **C6 power:** n(A) ≥ 15; else **INCONCLUSIVE**.

## Pre-committed decision rule

- **PASS C1–C6:** recommend confirmer filter for **shadow proposals only**
  (still no order path). Document θ=0.50 as shadow default.
- **INCONCLUSIVE:** keep dual logging; no filter recommendation.
- **FAIL:** do not filter; leave dual logging for observation.
- **Never:** promote to `HYDRA_S3_LIVE`, engine BUY gate, or force_hold.

## Runner

`heartbeat/tools/bakeoff_s3_heartbeat_confirmer.py`  
Output: `heartbeat/evidence/bakeoffs/s3_heartbeat_confirmer.json`
