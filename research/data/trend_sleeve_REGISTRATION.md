# Pre-registration — daily trend sleeve gate (`HYDRA_TREND_SLEEVE`)

Registered 2026-10-01, before any real-data run of `tools/trend_sleeve_gate.py`.
Nothing below may change after the first real-data run. A changed criterion
is a new registration with a new date, and the old result stays on record.

## Question

The engine cites the daily trend ensemble as its validated edge
(`trend_results.json`), but trades it only as a filter on rare 1h entries
and exits on 1h noise. The final engine made **+1.0% in 3 years on 11
trades**. `trend_overlay_gate.json` (`_final_after_donchian_fix`) records that.

The sleeve trades the ensemble directly. Does holding the ensemble earn
more risk-adjusted return than the controls below, after costs? The
controls include the one the original study skipped: **vol-targeted
buy-and-hold**.

## Why the existing evidence is not enough

1. `tools/trend_backtest.py` credits **4% APY on idle cash to the trend
   arms only**. Buy-and-hold holds no cash, so it never earns it. This
   biases every trend-vs-B&H comparison in `trend_results.json`.
2. It has **no vol-targeted buy-and-hold control**. The "VT" trend variants
   (BTC Sharpe 1.36–1.50 vs B&H 1.13) can't be split into how much comes
   from timing and how much from vol targeting.
3. Its VT variants **rebalance daily** (50–79 switches/yr). The engine
   sleeve sizes once at entry. The study does not test the engine's
   construction.
4. An independent monthly replication (K3,
   `research/data/abi/trend_independent_killtest.json`) found **SMA10
   Sharpe 0.832 vs B&H 0.823**. That is a lower drawdown (69.5% vs 79.2%)
   but no material Sharpe gain. The frame was KILLED on Sharpe.

**Prior (registered):** timing is more likely to show up as drawdown
control than as higher Sharpe. The most likely failure is criterion C1 or
C2.

## Candidate (D) — exactly the engine code

The gate does not re-implement the sleeve. It drives a real `HydraEngine`
with two bars per UTC day:

- an opening print at 00:00, which completes the previous day;
- a closing print at 23:00.

So these all come from `hydra_engine.py` itself:

- the ensemble score;
- the Donchian state machine;
- the 420-day close window;
- the 21-day vol multiplier.

| Rule | Value |
|---|---|
| Score | `0.4·(close > SMA200) + 0.4·(EMA20 > EMA100) + 0.2·Donchian(55 in / 20 out)`, on **completed** closes |
| Long | while the score is ≥ 0.6 |
| Flat | while the score is < 0.6; warming (< 210 closes) stays flat |
| Entry size | `equity × cap × vm / price`, units fixed until exit, no rebalancing |
| `cap` | `max_position_pct` = 0.40 (competition preset; production runs `--mode competition`) |
| `vm` | `clamp(30 / realized_vol_21d_annualized, 0.2, 1.0)` |
| Execution | decide on the close of day *t−1*; trade at that close plus costs; hold over day *t* |

## Arms (same window, same costs, same cap frame)

| Arm | Rule |
|---|---|
| A `bh_cap` | always long at `cap`, re-sized every 30 days |
| B `bh_voltarget` | always long at `cap × vm`, re-sized every 30 days (vol-managed B&H) |
| C `sleeve_novt` | D without vol targeting (`vm = 1`) |
| **D `sleeve`** | **the candidate** |
| E `inverse` | long while the score is < 0.6 (warm), same entry sizing as D |
| F `engine_off` | (`--engine`) `BacktestRunner` on 1h tape, current engine |
| G `engine_sleeve` | (`--engine`) `BacktestRunner`, `HYDRA_TREND_SLEEVE=1` |

Idle cash earns 0% in every arm. A 4% sensitivity is reported, applied to
**every** arm's idle cash, and not gated.

## Data

- `hydra_history.sqlite`: grain 86400 if present, else 3600 resampled to
  the last close per UTC day. Alternatively `--csv PAIR=path` (date or
  timestamp, close).
- Pairs: BTC/USD, ETH/USD, ZEC/USD (the default cores).
- Evaluation starts on the first day the sleeve is warm. The same window
  is used for every arm of an asset.
- **An asset needs ≥ 5 years evaluated.** Below that it is
  `INSUFFICIENT_DATA` and does not count.

## Costs

- **Base:** 25 bps fee + 10 bps slippage per side, on traded notional.
  *Erratum (2026-10-01):* this line first said "25 bps is Kraken's
  entry maker tier". That was written from memory, not evidence. The
  repo's captured `kraken volume` responses (`tests/test_kraken_cli.py`)
  and its paper-fill model both show 16 bps maker / 26 bps taker for
  this account, and the dashboard's `Fee M/T` pill shows the live tier.
  The cost is unchanged. 25 bps is a conservative base, not a quote of
  the account's tier.
- **Stress:** 40 bps + 10 bps.
- **Engine arms:** `maker_fee_bps=25` with the realistic post-only fill
  model.

## Statistics

- **Sharpe:** daily equity returns, mean/sd (ddof=1) × √365.
- **maxDD:** on the daily equity curve.
- **Sub-periods:** three equal contiguous thirds of the evaluation window.
- **Bootstrap (reported, not gated):** paired circular block bootstrap of
  the daily return pairs (D, B). Block = round(n^⅓), 2000 resamples,
  seed 7. Reports the 5th–95th percentile of Sharpe(D) − Sharpe(B).

## Reported, not gated

- **The engine breaker.** D and B are rerun with the engine's sticky 15%
  circuit breaker (sell at that close, never buy again). The report gives
  each arm's stats and the trip date, plus the sleeve's maximum exposure.
- **Why it matters.** The sleeve sizes once and never trims, so a rally
  grows its exposure. An ordinary pullback can then cost 15% of equity and
  halt it until `HYDRA_RESET_CIRCUIT_BREAKER=1`.
- **How the engine arm sees it.** G carries the real breaker, so a trip
  shows up there as a fidelity gap against D.

## Calibration (measured on synthetic data before any real-data run)

Command: `python tools/trend_sleeve_gate.py --calibrate 40`. Output:
`research/data/trend_sleeve_gate_calibration.json`. Each run is 2800 days.

| family | per-asset PASS (original D) | per-asset PASS (D after A1) |
|---|---|---|
| random walk with bull drift (no timing edge by construction) | 1/40 | 4/40 |
| random walk with zero drift | 1/40 | 3/40 |
| 180-day ±0.4%/day trend regimes | 26/40 | 35/40 |

Under independence, the 2-of-3 rule gives this for the amended D:

- **Global false-positive rate:** about 1.6–2.8%.
- **Power on the trending alternative:** about 96%.

Re-sizing made D a cleaner vol-targeted strategy. A pure random walk
therefore lands closer to a coin flip against B than the drifting
entry-only D did. The criteria were not tightened to win back the old
rate.

## Criteria — per asset, all must hold

| id | criterion |
|---|---|
| C1 | Sharpe(D) > Sharpe(B), full window, base costs |
| C2 | Sharpe(D) > Sharpe(B) in ≥ 2 of 3 thirds |
| C3 | maxDD(D) ≤ 0.75 × maxDD(B) |
| C4 | Sharpe(E) < Sharpe(B) (inverse control: the timing must point the right way) |
| C5 | Sharpe(D) > Sharpe(B) at stress costs |

## Verdict

- **Daily PASS:** ≥ 2 assets pass C1–C5.
- **Engine check** (required for a full PASS): on each passing asset,
  total return G > F over the same window. G must also track D in log
  growth: |ln(1+G) − ln(1+D)| ≤ max(0.5 × |ln(1+D)|, 0.05). A larger
  gap means the daily simulation does not represent the engine, and the
  verdict is `FIDELITY_FAIL`.
- **Labels:**
  - `PASS` = daily PASS and the engine check passes.
  - `PASS_DAILY_ONLY` = daily PASS, but `--engine` was not run (not
    sufficient to enable).
  - `FAIL` = otherwise.
- **Significance:** `significant` is added only when the bootstrap 5th
  percentile is > 0 on every passing asset. Without it, a PASS is a point
  estimate. Treat it as a reason to run the sleeve small, not as an
  established edge.

## Decision rule (registered)

- `FAIL` → leave `HYDRA_TREND_SLEEVE` off. If B beats D, the better
  product is vol-managed buy-and-hold in the cap frame. Hydra does not
  implement that, and adding it would be a new registration.
- `PASS` without `significant` → operator's call, at reduced capital.
- `PASS significant` → enabling is supported by the evidence.

**What a PASS is not:** it is not alpha. The sleeve is long crypto beta
with drawdown control, so it makes money when crypto trends up and holds
cash otherwise.

## Amendment A1 (2026-10-01, before any real-data run of the gate)

**What changed.** D is now the **re-sized** sleeve.

- Every `SLEEVE_RESIZE_DAYS` (30) after its last sizing, a held sleeve is
  re-set to `equity × cap × vm`. No trade happens if it is already within
  `SLEEVE_RESIZE_TOL` (10%) of that target.
- A trim is a partial SELL and a top-up is a BUY. The trim is protected
  like the exit, so the rules, the LLM and the coordinator cannot veto it.
- The original construction (sized once, never re-sized) is kept as the
  reported arm `sleeve_entry_only` (D0).
- C (`sleeve_novt`) and E (`inverse`) use the same re-size rule as D.
- The criteria are unchanged.

**Why: evidence from data the gate does not use.** On the independent
monthly BTC series (K3b,
`research/data/abi/trend_voltarget_monthly_killtest.json`, post-hoc), at
the 0.40 cap:

| construction | Sharpe | maxDD | max exposure | sticky 15% breaker |
|---|---|---|---|---|
| entry-only | 0.58 | 58.0% | 0.92 | trips June 2013, CAGR 3.4% |
| re-sized monthly | 1.05 | 14.3% | 0.46 | never trips, CAGR 20.1% |

- **Mechanism:** without trims, a rally drifts the position toward all of
  equity. The sleeve becomes unhedged buy-and-hold just before the
  pullback.
- **No tuning:** the 30-day period matches the B control's re-size
  cadence and K3b's monthly reset. The 10% band is an a-priori
  churn/dust guard. Neither was swept on any data.
- **Not data-snooping:** the operator's Kraken store, the gate's data,
  has not been touched by this amendment.
- **Calibration:** the null calibration was re-run for the amended D
  (`research/data/trend_sleeve_gate_calibration.json`).
