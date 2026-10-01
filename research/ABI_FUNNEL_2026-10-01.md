# ABI funnel — 2026-10-01 (decision-path audit cycle)

Previous cycle: `heartbeat/evidence/ABI_FUNNEL_2026-07-19.md` (frames F1–F14).

This cycle asks one question. **Where could Hydra's money come from, and
does any candidate survive a pre-registered kill-test on real or
independent data?**

Every number below is quoted from a committed file, which is named next
to it.

## 1. Anomalies (quoted)

| id | anomaly | numbers | source |
|---|---|---|---|
| A1 | The production engine is cash | final config: +1.0% over 3y, 11 round trips; 1y +0.04% on 1 trade | `research/data/trend_overlay_gate.json` (`_final_after_donchian_fix`) |
| A2 | Its best year was abstention | +0.035% while BTC fell 44.55% | `research/data/monthly_roi_1y.json` |
| A3 | The "validated" trend edge has an asymmetric cash credit | the trend arms earn 4% APY on idle cash; buy-and-hold holds none | `tools/trend_backtest.py` (`CASH_APY_PCT = 4.0`) |
| A4 | The trend edge has no vol-targeting control | VT trend Sharpe 1.36–1.50 vs B&H 1.13, never vs **vol-targeted** B&H; the VT arms rebalance 50–79×/yr | `research/data/trend_results.json` |
| A5 | "Won 3/3" is about return, not risk | conviction sizing 3y: return 1.0% vs 0.3%, **Sharpe 0.308 vs 0.366, DD 1.87 vs 0.52** | `research/data/conviction_sizing_gate.json` |
| A6 | The gate windows are tiny | ≤ 11 round trips in 3y, ≤ 1 in 1y | `trend_overlay_gate.json`, `trend_entry_gate.json` |
| A7 | The evidence bootstrap passed coin flips | 95% CI above 0 for **26%** of zero-edge series at n=25 (nominal 2.5%) | `research/data/abi/mc_bootstrap_calibration.json` |
| A8 | S3 X1 is positive but not established | BTC +1.17%/trade t=0.74 CI [−1.99, +4.10]; ETH +3.28% t=1.59 CI [−0.70, +7.23] | recomputed from `research/data/s3/s3_trade_ledger_x1.json` (n=69; 5000-resample bootstrap, seed 7) |
| A9 | The heartbeat labeler scored resolved events | null tapes reach AUC ~0.60 with the leak | `heartbeat/tests/test_labeler.py` (pinned) |
| A10 | The 6-qubit ZZ kernel concentrates | mean off-diagonal 0.019 on uniform inputs | `research/data/abi/quantum_kernel_REGISTRATION.md` (A1) |

## 2. Mechanisms (bored, each with its verifying computation)

- **M1 — abstention, not alpha (A1, A2, A6).**
  - **Claim:** the 1h entry stack (TREND_UP regime, BUY confidence ≥ 0.65,
    daily overlay long, friction) almost never fires. Engine P&L is
    therefore near zero whatever the market does.
  - **Verified:** 11 round trips in 3y (A1); 0 BUY fills on BTC/ETH over
    365 days with the rails on (2 BTC and 1 ETH BUY signals,
    `heartbeat/evidence/bakeoffs/engine_buy_cooccurrence.json`).
- **M2 — the trend evidence conflates three things (A3, A4).**
  - **Claim:** `trend_results.json` mixes timing, vol targeting and a cash
    yield only the trend arms receive.
  - **Verified:** an independent source with no cash credit and no vol
    targeting (K3 below) finds trend timing ≈ B&H on Sharpe, with lower
    drawdown.
- **M3 — 1h post-only fills are not the bottleneck.**
  - **Claim:** resting orders at the touch fill within one bar ~94–97% of
    the time on real Kraken 1h bars (K2).
  - **Implication:** the execution layer does not lose the edge, because
    there is no edge for it to lose (M1).
- **M4 — next-bar direction is ≈ unpredictable from price features (A10).**
  - **Claim:** this holds on these samples at daily and hourly horizons.
  - **Verified:** every model is at AUC ≈ 0.50, classical and quantum
    alike (F-Q below).

## 3. Frames tested this cycle

| frame | foreign domain → mapping | testable implication | kill-test | result |
|---|---|---|---|---|
| F-Q | quantum information: ZZ feature map + fidelity kernel (exact statevector emulation) | quantum-kernel SVM out-of-sample AUC beats the best classical kernel by ≥ 0.03, CI excluding 0 | `tools/abi_quantum_kernel_killtest.py` (registration + amendment A1, bandwidth-steelmanned) | **KILLED** on all 3 datasets: next-day AUC 0.502–0.507; next-hour 0.504–0.512; S3 second stage quantum 0.457/0.490 vs poly2 0.704 |
| F8 | auctions / winner's curse: a resting post-only order is a free option written to the market | SELL fill rate < 0.80 within 1 bar in downtrends, and unfilled SELLs precede losses | `tools/abi_trend_killtests.py --k2` | **KILLED**: SELL p_fill(1 bar) 0.94, BUY 0.966; unfilled SELLs fwd24 +0.093% |
| F14/F1 | options: trend following as a synthetic long straddle | trend timing beats B&H Sharpe on an independent source; the inverse loses | `tools/abi_trend_killtests.py --k3` (monthly BTCUSD 2013–2024, `backtesting` package) | **KILLED on Sharpe** (SMA10 0.832 vs B&H 0.823; TSMOM12 0.781). **Drawdown control holds** (69.5% vs 79.2%; 40.6% vs 72.7% in 2019–24); the inverse arm loses (−49.7%) |
| K3b | the same independent series, now against the control K3 lacked: vol-targeted buy-and-hold | SMA10 timing × vol target beats vol-targeted B&H on all five sleeve-gate criteria | `tools/abi_trend_killtests.py --k3b` (registered in git before the runner existed) | **SURVIVES, not significant**: Sharpe 1.052 vs 0.979 (B&H 0.822), maxDD 33.0% vs 50.1%, 3/3 thirds, inverse 0.018. Paired bootstrap range [−0.13, +0.37]. Vol targeting is the larger effect (+0.16 Sharpe); timing adds +0.07 and the drawdown cut |

Each verdict reproduces byte-for-byte from the committed runners. The
quantum JSON matches except for its timestamp.

The vendor series K3/K3b use was checked against well-known month-end
BTC closes. For example: 13,808 (Dec 2017), 3,751 (Dec 2018), 46,649
(Dec 2021), 16,567 (Dec 2022) and 93,381 (Dec 2024).

## 4. Survivor → pre-registered gate

**F15 — portfolio insurance (CPPI / synthetic put) × bandwidth separation (F12).**

- **Frame.** K3 kills trend timing as *alpha*, but keeps it as a hedge:
  it holds crypto beta and gives up the deep drawdowns.
- **Mapping.** Run the daily ensemble as a long-or-flat sleeve that
  decides on completed daily closes only. That is the bandwidth
  separation the 2026-07-19 funnel made an invariant (F12).
- **Testable implication.** The sleeve has a higher Sharpe than
  vol-targeted B&H, the control the original study skipped. Its drawdown
  is ≤ 0.75 × that control's. The inverse arm loses.

What exists:

- **Implementation:** `HydraEngine` `trend_sleeve`, behind
  `HYDRA_TREND_SLEEVE`, default **OFF**. Tests: `tests/test_trend_sleeve.py`.
- **Registration:** `research/data/trend_sleeve_REGISTRATION.md`, frozen
  before any real-data run.
- **Gate:** `tools/trend_sleeve_gate.py`. Its machinery tests are
  `tests/test_trend_sleeve_gate.py`.
- **Null calibration:** `research/data/trend_sleeve_gate_calibration.json`.

| synthetic family (40 runs each) | per-asset pass rate | global rate (2 of 3) |
|---|---|---|
| random walk, bull drift (null) | 1/40 | ~0.2% |
| random walk, zero drift (null) | 1/40 | ~0.2% |
| 180-day trend regimes (alternative) | 26/40 | ~72% |

**Status: UNTESTED on real data.** The audit container cannot reach any
exchange. The 11-year store (`hydra_history.sqlite`) is on the operator's
machine. Run:

```
python tools/trend_sleeve_gate.py --engine
```

A smoke run on the only real daily bars in the repo (400 bars per asset,
`s3bounce/tests/fixtures/parity_*`) is **not evidence**. It covers 0.52
years after warmup, and the gate correctly reports INSUFFICIENT_DATA.

- BTC fell 28.6% over the evaluated window.
- The sleeve lost 1.37%; vol-targeted B&H lost 13.28%.
- The sleeve was in the market 11% of the time.

**Evidence changed the candidate (registration amendment A1).** K3b's
post-hoc readout at Hydra's 0.40 cap ran on monthly data the gate never
uses:

| construction | Sharpe | maxDD | sticky 15% breaker |
|---|---|---|---|
| sized once at entry, never trimmed (as first built) | 0.58 | 58.0% | trips June 2013, CAGR 3.4% |
| re-set to cap × vm each month | 1.05 | 14.3% | never trips, CAGR 20.1% |

The engine sleeve now re-sizes every 30 days (10% band), with a
protected partial-sell trim. Inside the breaker the cap is the
risk-budget dial. At monthly marking, caps of 0.2 / 0.3 / 0.4 give
CAGR 10 / 15 / 20% with maxDD 7 / 11 / 14% and never trip. From 0.5
up the breaker trips in 2015 or earlier, because Sharpe (~1.05) does
not change with the cap. Daily marking will be harsher. The engine arm
of the real-data gate measures that.

**The remaining design risk, reported by the gate.** Re-sizing caps the
drift, but the sticky 15% breaker still ends the sleeve until an operator
reset if a daily-marked drawdown reaches it (`engine_breaker_diagnostic`
covers D, D0 and B). The first-built entry-only sleeve showed this on
ZEC's +900% fixture window: a 35.9% drawdown. If the engine arm shows
the breaker ending the re-sized sleeve, the cap or the breaker policy
for the sleeve is the operator's decision. It is not a parameter to
sweep.

## 5. Proposed, untested (next cycle; each needs its own registration)

| frame | foreign domain → mapping | falsifiable implication |
|---|---|---|
| F16 | pharmacology (dose–response): exposure as dose, drawdown as toxicity | trimming the sleeve back to `cap × vm` when exposure exceeds 1.5× the entry dose lowers maxDD without lowering Sharpe vs the untrimmed sleeve |
| F17 | actuarial reserving: idle cash is a reserve earning the risk-free rate | with the real Kraken USD/USDC reward rate applied to idle cash in **all** arms, the sleeve-vs-B&H ranking does not change (if it does, the edge is the yield) |
| F18 | epidemiology (susceptible pool): S3 bounce entries only after a cascade has burnt out | S3 X1 expectancy conditioned on a falling 7-day liquidation proxy beats the unconditioned X1 on the shadow window |
| F19 | queueing (balking): post-only BUYs balk when the queue ahead is long | at 5m granularity, unfilled BUYs in uptrends have higher forward returns than filled ones (K2 hints at this: +1.098% vs +0.351% fwd24 on n=37) |

## 6. Killed lines (do not reopen without a new anomaly)

- Quantum-kernel (or any feature-map kernel) next-bar direction
  classifiers on price features (F-Q).
- Re-price / TTL tuning as an *edge*. It stays a correctness fix: an
  engine-blind parked exit (CLAUDE.md, audit 2026-10).
- Trend timing claimed as Sharpe alpha (K3). Only the drawdown-control
  property is supported.
- Earlier kills stand (2026-07-19 funnel).


## 7. Claims audit (2026-10-01, after "don't take the written record as gospel")

| claim | status | evidence |
|---|---|---|
| BTC fell ~44.5% over the engine's last year | **verified** | real Kraken daily bars: 119,855 (2025-07-14) → 62,254–64,709 (mid-July 2026), −46% to −48% by endpoint |
| S3 ledger returns are real | **verified where data overlaps** | 6 trades overlap the real daily fixtures. Every forward return equals the real price change minus exactly 0.52% (26 bps/side round trip) |
| K3/K3b vendor data is sound | **verified** | month-end closes match widely reported values (above) |
| "Kraken's entry maker tier is 25 bps" | **corrected** | the repo's captured fee responses and paper model use 16 bps maker / 26 bps taker; the gate's 25 bps is conservative |
| "the LLM layer costs ~$125–1,200/yr" | **retracted** | no derivation exists. Since the no-inventory fix, trade deliberations in the September ledger fall from 187 to 5 in 20 days. The recurring cost is a Grok portfolio review every 3 hourly candles. The brain reports real spend as `cost_today_usd` |
| "the engine is cash" | **consistent across three records, older code** | 11 round trips in 3y; 0 BUY fills in 365d; 0 trades in the 20-day September 2026 ledger. Not re-run on the current code here |
| "funding settles hourly" | **unverified here** | consistent with the repo's carry tooling and its hourly funding history; confirm on the first live run |
| "the trend sleeve makes ~20%/yr" | **not claimed** | that is a monthly BTC 2013–2024 analogue at the 0.40 cap. The sleeve has no real-data result yet |
