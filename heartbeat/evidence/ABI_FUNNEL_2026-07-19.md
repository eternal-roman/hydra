# ABI Funnel 2026-07-19 — Hydra trading + heartbeat

> Anomaly → Bore → Ideate on **v2.30.1** (cores BTC/USD, ETH/USD, ZEC/USD).
> Evidence-only. Survivors need pre-registered `/bakeoff` — never promote here.
> Absorbs: `ABI_FUNNEL_2026-07-18.md`, `ABI_FUNNEL_STOPS_2026-07-18.md`,
> `ABI_FUNNEL_ROUND3_2026-07-18.md`, `abi/s3_trail_funnel_2026-07-19.md`.

## One-line synthesis

Retail spot HYDRA already solves **capital preservation** (rails + overlay);
the only after-fee selection edge in evidence is **daily S3 bounce quality on
BTC/ETH with X1 exits**; heartbeat is a **calibrated, liquidity-limited
confirmer** for that book — not a fix for the fee-dead 1h engine.

## Anomalies (quoted)

| id | claim | numbers | source |
|---|---|---|---|
| P1 | 1y edge is abstention | HYDRA **+0.035%**; B&H SOL −52% / BTC −44%; 11/12 mo = 0 | `.hydra-flywheel/monthly_roi_1y.json` |
| P2 | 90d rails → zero BUYs | `fills: 0` over 2160 candles | `evidence/hydra_bakeoff.json` |
| P3 | raw tech underperforms B&H | SOL −54.6% vs BH −44.5%; BTC −28.5% vs −25.5% | `validation_results.json` |
| P4 | overlay is the product | ON vs OFF 1y +0.01 vs −0.39; 6/6 windows | `trend_overlay_gate.json` |
| P5 | daily entry + 1h exit whipsaws | entry ON 2y **−5.37%** vs OFF **+0.09%** | `trend_entry_gate.json` |
| P7 | bridge dead | 0 trades 1y; 2y Sharpe −0.99 vs no-bridge −0.63 | `bridge_isolation.json` |
| H1 | flow AUC liquidity-stratified | BTC mean **0.76 PASS**; ETH **0.73 PASS**; SOL **0.56 FAIL**; ZEC **0.61 FAIL** | `HONEST_FINDINGS.md` |
| H2 | "flow" often candle-shape | ETH clv **+0.885** vs ofi **+0.033** | `real_tape/weights_*` |
| H3 | uncalibrated ≈ coin | BTC AUC **0.55**; high bins miscalibrated | `eval_BTC_USD_1h.md` |
| S1 | S3 gated BTC/ETH positive | BTC +1.03%/trade n=23; ETH +2.25% n=30; ZEC FAIL | `s3_daily_classifier.json` |
| S2 | X1 adopted; flip killed | X1 +2.36 C2 8/13; flip +13.2 but C2 5/13, hold 1 bar | `s3_exit_policy.json` |
| S3 | stops = insurance | **23/23** stop exits are losses | S3 paper / trail funnel |
| S7 | daily ensemble ≠ S3 watermark | ens&lt;0.6 stop 0.31 fwd60 +14.9% vs ≥0.6 stop 0.38 / +9.4% | trail funnel |
| S8 | trails fail stability | X4a LOYO fail; X5 C3 fail → SHADOW_ARM_ONLY | `s3_trail_exit.json` |
| S10 | engine+HB bakeoff inconclusive | zero BUYs under v2.28 rails | `hydra_bakeoff.json` |

## Mechanisms

1. **M1 Venue arithmetic** — 1h fee/ATR 0.4–1.26 → coin; daily 0.02–0.22 → residual is regime beta, not microstructure.
2. **M2 Production edge is exit system** — hold-through + daily overlay + conviction; placebo entries ≈ bounce under same exit.
3. **M3 Bounce label compounds** — oracle−fake spread monotone in k; X1 banks short-horizon quality; trails re-import beta.
4. **M4 Heartbeat = second-stage confirmer on deep majors only** — calibrate mandatory; SOL/ZEC FAIL; cascade weeks break all assets.

## Hybrid frames (survivors / process)

| # | domain | implication | status |
|---|---|---|---|
| F1 | ground delay | BUY only when daily ensemble clears | **SHIPPED** overlay |
| F2 | two-stage assay | S3 screen + HB confirmer BTC/ETH | **bakeoff stub** |
| F3 | pin / cascade | HB AUC collapses in multi-asset washouts → confirmer `no_opinion` | measurement |
| F12 | bandwidth separation | never couple daily entry to 1h defensive exit; S3 ≠ overlay gate | **invariant** |
| F14 | suppress→miss | do not put trend overlay on S3 entries (S7) | **invariant** |
| F7 | cash hurdle | +0.035% is preservation not growth alpha | narrative kill |
| F10 | cold chain | ZEC excluded (loss geometry + classifier FAIL) | **enforced** |

**Killed (do not reopen without new anomaly):** 1h bounce family @ taker fees,
flip/hybrid exits, blind K-holds, freshness, envelope-1h, close-confirm-as-info,
trail basis flip, SOL weight transfer, regime-priced stops, time-stops.

## Decision-path friction

```
candle → engine.tick(generate_only) → hold-through → daily overlay
  → friction (BUY) → exit_only → 15% CB (BUY only)
  → coord (no-op default) → book/session conf mods
  → brain (non-HOLD) → R1–R10 → QFE → execute → limit post-only
  → S3 shadow (parallel, no orders) + heartbeat surface (display)
```

| money lost if wrong | missed edge |
|---|---|
| 1h entries without rails (P3) | rails winter = 0 BUYs (P1/P2) — correct preservation |
| daily-entry/1h-exit (P5) | S3 X1 book shadow-only |
| bridge trading (P7) | HB confirmer logs both arms, no live gate |
| uncalibrated HB hard gate (H3/H4) | QFE useless when flat all winter |

## Heartbeat fit-polish (this cycle)

| phase | action | order path? |
|---|---|---|
| **0 shipped** | pair-named status files; shared reader with S3 confirmer | no |
| **1 shipped** | `hydra_heartbeat_surface` → `quant_indicators["heartbeat"]` + dashboard P(up) sparkline; kill `HYDRA_HEARTBEAT_SURFACE=0` | no |
| 2 next | `HYDRA_S3_STRATEGY=1` shadow window; Stub 1 confirmer bakeoff on **replayed** posteriors | no |
| 3 later | optional soft gate / S3 live only after bakeoff + n≥15 | opt-in only |

### Bakeoff stubs (pre-register before run)

1. **s3_heartbeat_confirmer** — arms: S3-only X1 / S3+HB / inverse / random 50%; promote iff stop-rate ↓ ≥10pp and expectancy ≥ baseline−0.3pp and inverse worse.
2. **engine_buy_cooccurrence** — diagnostic only; if n_BUY≈0, forbid "HB improves engine entries" claims.
3. **cascade_week_blackout** — stratified AUC; if cascade ≤0.55 and quiet ≥0.70, confirmer emits no_opinion in cascade weeks.

## Non-goals

No 1h bounce resurrection · no SOL/ZEC weight transfer · no overlay on S3 ·
no trail/flip promotion · no stripping CB/friction/R1–R11 · no growth-alpha
claim for +0.035%.

## Thesis review — recommendation dispositions (2026-07-19 action pass)

Thesis under test: **preservation product + S3 daily X1 edge (shadow) +
heartbeat as BTC/ETH confirmer only**. Actions only when the recommendation
strengthens that thesis without reopening killed frames.

| recommendation | holds? | action |
|---|---|---|
| Status path + dashboard P(up) surface | **YES** — measurement/display, no orders | **DONE** (`hydra_heartbeat_surface`, App.jsx) |
| Docs densify / CB wording / default pairs | **YES** — reduces operator false models | **DONE** |
| Auto-load calibrated weights on `heartbeat run` | **YES** — H3 uncalibrated ≈ coin | **DONE** (`weights_io` + cli) |
| Brain soft advisory for HB/S3 in QI block | **YES** — tax-friction pattern; explicit never force_hold | **DONE** (`_format_heartbeat_s3_advisory`) |
| Pre-register s3×HB confirmer bakeoff | **YES** — F2 measurement, shadow-only promote | **DONE** registration + runner |
| engine_buy_cooccurrence diagnostic | **YES** — forbids false engine claims when n_BUY≈0 | **DONE** registration + runner |
| cascade-week AUC measurement | **YES** — F3; display policy only | **DONE** registration + runner |
| Enable `HYDRA_S3_STRATEGY=1` by default | **NO** — operator opt-in; shadow is final authority | **REJECT** (document only) |
| Live SKIP-BUY from heartbeat on engine | **NO** — P2/S10 zero-BUY + fee/ATR 1h dead | **REJECT** |
| `HYDRA_S3_LIVE` / order path from S3 | **NO** until shadow n≥15 + own bakeoff | **REJECT** |
| Trail X4a/X5 basis flip | **NO** — LOYO/C3 already failed | **REJECT** |
| Daily-entry + 1h exit re-add | **NO** — trend_entry_gate −5.37% | **REJECT** |
| Strip CB / friction / R1–R11 | **NO** — safety ≠ false edge | **REJECT** |

Runners (execute after registration; commit JSON either way):

```text
python heartbeat/tools/engine_buy_cooccurrence.py
python heartbeat/tools/bakeoff_s3_heartbeat_confirmer.py --days 90
python heartbeat/tools/cascade_week_heartbeat_auc.py --days 90
```

### Executed results (2026-07-19, post-breadth fix)

| runner | verdict | numbers | thesis implication |
|---|---|---|---|
| `engine_buy_cooccurrence` | **FORBID_ENGINE_HB_CLAIMS** | buy_fills=0; BTC s3_days=2; cooc=0 (365d rails ON) | Confirms P2: cannot claim HB improves **engine** path |
| `s3_heartbeat_confirmer` | **INCONCLUSIVE C6** | n(A)=1 in 90d (BTC one gated X1 +7.4%; ETH 0) | Thin flow (~2–4/yr); need multi-year tape×posterior for power — **no shadow filter promote** |
| `cascade_week_heartbeat` | **NO_BLACKOUT** | pooled AUC cascade **0.811** vs quiet **0.703** (n=66/156) | F3 hypothesized cascade AUC≤0.55 **falsified** on this 90d window — do **not** force confirmer no_opinion on cascade weeks |

**Thesis holds after action pass:** display/calibrate/advisory surfaces ship; live gates stay off; engine path claims forbidden; confirmer filter waits for powered bakeoff; cascade blackout killed by measurement.
