# Pre-registration — Engine BUY × S3 co-occurrence (diagnostic)

ABI funnel: `ABI_FUNNEL_2026-07-19.md` stub 2 / N1. **Not a strategy gate.**

## Hypothesis

Under rails ON (hold-through + trend overlay defaults), engine BUY fills
and S3 gated `entryable_b1` days co-occur ≈ 0 on recent history → any
claim that “heartbeat/S3 improves **engine** entries” is untestable
until n_BUY ≥ 20.

## Protocol

1. Backtest default cores (BTC/USD, ETH/USD) competition mode, rails ON,
   sqlite 1h, last 365d (or full archive if shorter).
2. Count engine BUY fills / BUY signals.
3. Count S3 gated entryable days overlapping the window (frozen artifact).
4. Count calendar-day co-occurrence (BUY fill day ∩ S3 gated day).

## Decision rule (diagnostic only)

| result | consequence |
|---|---|
| n_BUY < 20 | **Forbid** live claims that HB/S3 improves engine path; engine+HB bakeoff stays inconclusive |
| co-occurrence = 0 and n_BUY ≥ 20 | Orthogonal books — keep surfaces separate |
| co-occurrence > 0 | Eligible for a **future** pre-registered engine confirmer bakeoff (not this one) |

## Runner

`heartbeat/tools/engine_buy_cooccurrence.py`  
Output: `heartbeat/evidence/bakeoffs/engine_buy_cooccurrence.json`
