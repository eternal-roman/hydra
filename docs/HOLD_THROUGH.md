# Hold-Through Rails

**Default ON** for every pair. Kill: `HYDRA_HOLD_THROUGH=0`.  
**Code:** `HydraEngine._apply_hold_through` · **Tests:** `tests/test_hold_through.py`

Product default after multi-pair causal bakeoffs (fees-on, next-bar fills).
Replaces opt-in `HYDRA_REGIME_SELECTIVE` (weaker floor, no ride-through).

## What this hardens

| Rule | Sanded danger | Kept capability |
|------|---------------|-----------------|
| BUY only in `TREND_UP` | Chop / ranging / volatile entries | Momentum path when trend is real |
| BUY conf ≥ **0.65** | Low-conf churn (0.55 re-opened losses) | Matches competition sizer floor |
| Long + `TREND_DOWN` → force SELL | Bag-holding dumps | Defensive flatten (session CB still separate) |
| Mid-`TREND_UP` SELL → HOLD unless extreme overbought | Noise fade-outs that cut winners | Extreme-RSI / reason-tagged exits still fire |

Friction gate, 15% drawdown breaker, limit post-only, Kelly sizing, AI brain,
and R1–R11 are **unchanged**. Rails re-apply on `execute_signal` so brain /
coordinator cannot bypass.

## Decision tree

```
signal + regime + position
  │
  ├─ long + TREND_DOWN     → force SELL (flatten)
  ├─ BUY + not TREND_UP    → HOLD
  ├─ BUY + TREND_UP
  │     conf < 0.65        → HOLD
  │     conf ≥ 0.65        → BUY
  ├─ long + TREND_UP + SELL
  │     extreme overbought → SELL
  │     else               → HOLD (ride)
  └─ else                  → pass through
```

## Cutoffs (evidence)

| | Value | Why |
|--|------:|-----|
| BUY min conf | **0.65** | ≈ p90 of TREND_UP BUY conf on 1h tape; multi-pair h12 mean ≥ 0; matches sizer |
| Flatten floor | **0.65** | Display only — always flatten TREND_DOWN |
| Mid-trend exit | **reason only** | Conf path anti-predictive / unreachable at 0.85 |

## What this is *not*

- **Not a profit claim.** Absolute alpha unproven; rails are relative defense +
  capture discipline on historical windows.
- **Not a pair delist.** SOL/USD · SOL/BTC · BTC/USD remain the default
  triangle. Isolation (BH vs cash vs rails) shows **strategy + window**
  limits, not “bad instruments.”
- **Not extra TA.** ADX / HTF SMA / ATR trail / swing HH / squeeze failed the
  multi-pair bar (e.g. ADX helped SOL/bridge, hurt BTC). Do not ship as
  global defaults.
- **Not deregulation.** Turning rails off (`=0`) is for research / base-path
  unit tests — not a live “unlock alpha” switch.

## Ops

```bash
python hydra_agent.py --mode competition --resume   # rails on (default)
set HYDRA_HOLD_THROUGH=0                            # raw engine (research)
python -m pytest tests/test_hold_through.py -q
```
