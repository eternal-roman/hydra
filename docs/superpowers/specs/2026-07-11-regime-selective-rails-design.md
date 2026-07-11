# Regime-Selective Rails Design

**Date:** 2026-07-11  
**Status:** Approved from causal AI-control study (prior session)  
**Evidence:** `.hydra-research/ai_control_sol_90d.json`, `ai_control_sol_365d.json`, `ai_control_btc_365d.json`

## Problem

Causal next-bar experiments on `hydra_history.sqlite` showed:

1. **Deregulation fails** — lowering `min_confidence` to 0.50, disabling friction, or aggressive size-up does **not** produce positive absolute P&L and often **worsens** 90d SOL returns vs baseline.
2. **Re-regulation helps (relative)** — an AI-shaped **selective** proxy lost less by trading less and cutting inventory faster, but still did **not** print absolute green on the studied windows.

| Window | Baseline | Selective | vs base | Absolute +? |
|--------|----------|-----------|---------|-------------|
| SOL 90d 1h | ≈ −13.2% | ≈ **−1.3%** | +~12 pp | No |
| SOL 365d 1h | ≈ −14.3% | ≈ **−6.8%** | +~7.5 pp | No |
| BTC 365d 1h ($10k) | ≈ −12.4% | ≈ **−0.1%** | +~12 pp | No |

## Goal

Implement the **selective re-regulation stack** as a first-class, kill-switchable engine policy so:

- Live/paper and backtest share the **same deterministic rules**.
- Counterfactual retests land in the **same ranges** as the study (within tolerance).
- We **do not** ship “more AI control” as weaker conf/friction rails.
- Absolute +P&L is **not** claimed; success is fidelity + relative loss control.

## Non-goals

- Wiring live Claude/Grok discretionary text as authority (shadow log is future work).
- Disabling friction gate or dropping min_conf to 0.50 by default.
- Adding new tickers or market orders.
- Flywheel capital reallocation (separate sleeve work).

## Design decision (approach chosen)

**Approach A (recommended): Deterministic engine policy layer** after signal generation, before execute.

| Approach | Pros | Cons |
|----------|------|------|
| **A. Engine `apply_regime_selective()`** | Same path as live tick/execute; backtestable; no LLM cost | Not “real LLM” |
| B. Agent-only filter | Easy to toggle | Backtest Phase-1 skips it → drift |
| C. Brain prompt-only | Soft | Non-deterministic; not in causal study |

**Choose A.** The study’s winning policy was a **rule stack**, not freer model text.

## Policy rules (exact)

When `regime_selective` is enabled (see kill switch):

1. **Force flatten:** if `position.size > 0` and `regime == TREND_DOWN` → replace signal with `SELL`, conf `max(engine_conf, 0.70)`, reason tagged `REGIME_SELECTIVE:force_flatten`.
2. **Entry allowlist:** if action is `BUY` and regime is not `TREND_UP` → `HOLD` (reason `REGIME_SELECTIVE:block_buy_<regime>`). Covers RANGING, VOLATILE, TREND_DOWN.
3. **TREND_UP conf floor:** if action is `BUY` and regime is `TREND_UP` and `confidence < 0.55` → `HOLD` (`REGIME_SELECTIVE:low_conf`).
4. **SELL path:** if engine says `SELL` and `position.size > 0`, allow at conf ≥ 0.50 for display; do **not** re-impose entry min_conf on exits (existing PR-A: exits ignore min_conf at execute).
5. **Unchanged:** friction gate, 15% CB (BUY halt), limit post-only, spot-only, Kelly sizing defaults.

## Kill switch

| Flag | Default | Effect |
|------|---------|--------|
| `HYDRA_REGIME_SELECTIVE` | **off** (`0` / unset) | Policy inactive; engine identical to pre-change |
| `HYDRA_REGIME_SELECTIVE=1` | opt-in | Apply rules 1–4 |

Constructor override: `HydraEngine(..., regime_selective: Optional[bool] = None)` — if set, wins over env (for tests/backtests).

## Integration points

1. `HydraEngine.tick()` — after `SignalGenerator.generate`, before execute / generate_only return: `signal = self._apply_regime_selective(regime, signal)`.
2. `HydraEngine.execute_signal()` — re-apply on the *current* regime from last tick state **or** re-detect regime from candles so generate_only + external execute cannot bypass (prefer re-detect for safety).
3. `tools/ai_control_counterfactual.py` — `ai_proxy_selective` may call engine with `regime_selective=True` for fidelity check, or keep pure policy_fn that mirrors engine (prefer engine path for single source of truth once implemented).
4. `CLAUDE.md` env table + CHANGELOG note (PATCH when released; this branch implements feature behind flag).

## Testing strategy

1. **Unit tests** — pure state transitions (force flatten, block RANGING BUY, low conf TREND_UP, friction still on).
2. **Causal retest** — re-run `tools/ai_control_counterfactual.py` SOL 90d / 365d; assert selective returns within **tolerance bands** of study:
   - SOL 90d selective: **−1.3% ± 3.0 pp** (band −4.3% … +1.7%)
   - SOL 365d selective: **−6.8% ± 4.0 pp** (band −10.8% … −2.8%)
   - Selective beats baseline by **≥ +5 pp** on both windows
   - No loose policy required to beat selective
3. **Negative control:** with flag off, behavior matches prior baseline class (no new HOLD reasons with `REGIME_SELECTIVE:`).

## Success criteria

| ID | Criterion |
|----|-----------|
| S1 | Flag default off; no behavior change when unset |
| S2 | Flag on implements rules 1–4 exactly |
| S3 | Retest ranges within bands above |
| S4 | Friction / min_conf 0.65 competition defaults preserved when selective off |
| S5 | Docs list what selective is **not** (absolute alpha unproven) |

## Risks

- Selection bias: rules chosen because they won on this tape → treat as hypothesis; holdout later.
- TREND_UP-only may under-participate in recoveries if regime lag is high.
- Force-flatten may exit before short bounces (accepted: study showed net benefit on this history).
