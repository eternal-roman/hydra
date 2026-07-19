# s3bounce

Standalone, dependency-free packaging of **S3** — a daily-bar bounce-leg
continuation classifier with a gate-adopted exit policy, produced by the
HYDRA heartbeat research program through pre-registered bakeoffs on
full-history Kraken data (2016–2026).

## What it does

On daily OHLCV bars it detects **bounce setups** (a confirmed swing low
after an established down-leg, followed by a 1.0·ATR bounce trigger),
scores each setup with a **frozen walk-forward logistic model** over six
candle-only features (`clv, range_atr, vol_z, shock_recency, breadth,
retest` — `range_atr` is the dominant weight on every asset), and gates
entries at the model's train-p75 threshold. Exits follow the
pre-registered exit-gate winner: **stop on close < setup low (filled at
that close), target 3.3·ATR, 200-bar horizon**.

## Evidence basis (read this before trading it)

| asset | basis | walk-forward result (net of 26 bps/side) |
|---|---|---|
| BTC/USD | X1 exit | +1.17 %/trade, win 0.696, n=23 (7–11y) |
| ETH/USD | X1 exit | +3.34 %/trade, win 0.643, n=28 |
| ETH/USD `hold_k60_stop` | **shadow arm only** | boot-LB +4.6 %/trade but hit 44%, median −4.9% — lottery profile |
| ZEC/USD | **excluded** | classifier gate FAIL; bars feed the breadth feature only |

Honest limits, measured (see `heartbeat/evidence/` in the parent repo):

- **Thin flow:** ~2–4 gated setups per asset per year. The promotion is
  *provisional pending a paper-shadow window* — that is what
  `ShadowLedger` exists for.
- **Continuation is NOT "always right":** per-entry hit rates at K≥20
  holding days are statistically coin-flip on every asset (all Wilson
  95% CIs include 0.5). Long-hold P&L is carried by a minority of large
  winners. No blind hold period is part of the basis.
- **The classifier's real, CI-supported edge** is short-horizon bounce
  quality under the target/stop construction — not regime prediction.
- Swing confirmation lags two bars: adjacent bounces first become
  computable at the entry-decision bar (b1 close). All signals here are
  causal at their stated decision time.

## Usage

```python
from s3bounce import S3Strategy, ShadowLedger, load_artifact

strat = S3Strategy(load_artifact())
for asset in strat.universe:                 # BTC/USD, ETH/USD, ZEC/USD
    strat.seed(asset, daily_rows[asset])     # [{ts,open,high,low,close,volume}]

# per completed 1h candle:
strat.on_1h("BTC/USD", ts, o, h, l, c, v)

sig = strat.evaluate("BTC/USD")              # S3Signal
if sig.stage == "entryable_b1" and sig.gated:
    ledger = ShadowLedger(".s3bounce-shadow")
    ledger.propose(asset="BTC/USD", low_ts=..., low_px=sig.setup.low_px,
                   atr=sig.setup.atr, low_idx=sig.setup.low_idx,
                   entry_idx=sig.entry_idx, entry_ts=..., entry_px=...,
                   score=sig.score, arms=list(...))
```

`evaluate()` returns a degraded, un-gated signal (with reasons) when the
model artifact is stale (>400 days), the asset is warming up, or a
breadth universe member's bars are missing — consumers must treat
degraded as "do not act" (SKIP), never as an error.

## Retraining

This package never fits anything. The yearly walk-forward refit is an
operator action in the parent repo:
`heartbeat/tools/export_s3_model.py` regenerates `model_artifact.json`
(hard-failing on drift against the promoted evidence) and the golden
parity fixtures under `tests/fixtures/` that pin this package's port to
the research pipeline at 1e-9 tolerance.

## Provenance

- Promotion: `heartbeat/evidence/bakeoffs/s3_daily_classifier.json`
- Exit gate: `heartbeat/evidence/bakeoffs/s3_exit_policy.json` (+ registration)
- Hold-horizon study: `research/data/s3/s3_hold_horizon.json`
- Narrative: `heartbeat/HONEST_FINDINGS.md`, `heartbeat/evidence/ABI_FUNNEL_ROUND3_2026-07-18.md`
