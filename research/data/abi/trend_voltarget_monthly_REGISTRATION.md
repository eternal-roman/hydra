# Pre-registration — K3b: timing vs vol-targeted buy-and-hold, independent monthly data

Registered 2026-10-01 before `tools/abi_trend_killtests.py --k3b` existed
or ran. It is committed on its own, before the result.

## Question

K3 found trend timing ≈ buy-and-hold on Sharpe but with lower drawdown.
The daily sleeve's registered gate
(`research/data/trend_sleeve_REGISTRATION.md`) compares the sleeve
against **vol-targeted** buy-and-hold, a control K3 lacked. This test
asks the same question on independent data before the operator's
real-data run. Does SMA10 timing add Sharpe on top of vol targeting?

## Data

- Source: the same vendor monthly BTCUSD as K3 (`backtesting==0.6.6`,
  `test/BTCUSD.csv`).
- Validated 2026-10-01 against well-known month-end closes (rounded):

| month | vendor | widely reported |
|---|---|---|
| 2017-12 | 13,808 | ~13.9k |
| 2018-12 | 3,751 | ~3.7k |
| 2020-12 | 28,921 | ~29.0k |
| 2021-12 | 46,649 | ~46.3k |
| 2022-12 | 16,567 | ~16.5k |
| 2024-12 | 93,381 | ~93.4k |

- Window: K3's window. Decide at month-end *t* (*t* = 12 … n−2), hold
  month *t*+1. That gives 143 monthly returns, 2013-02 to 2024-12.

## Construction

| element | rule |
|---|---|
| Vol at *t* | stdev (ddof=1) of the 6 monthly simple returns ending at *t*, × √12 |
| Vol multiplier | `vm_t = clamp(0.30 / vol_t, 0.2, 1.0)` (the engine's target, floor and cap) |
| Exposure | a fraction of equity, re-set at every month-end. Exposure drifts with the month's return. Turnover = abs(target − drifted exposure), charged at the per-side cost |
| Base cost | 25 bps fee + 10 bps slippage per side |
| Stress cost | 40 + 10 bps |
| Idle cash | earns 0% |

## Arms

| arm | exposure for month *t*+1 |
|---|---|
| A `bh` | 1.0 |
| B `bh_vt` | `vm_t` |
| C `sma10` | 1.0 if close > SMA10, else 0 |
| **D `sma10_vt`** | `vm_t` if close > SMA10, else 0 (the candidate analogue) |
| D2 `tsmom12_vt` | `vm_t` if the 12-month return > 0, else 0 (reported) |
| E `inverse_vt` | `vm_t` if close ≤ SMA10, else 0 |

## Statistics

- **Sharpe:** monthly mean/sd × √12.
- **maxDD:** on monthly equity.
- **Thirds:** three contiguous thirds of the 143 months.
- **Bootstrap:** paired circular block (block = round(143^⅓) = 5), 2000
  resamples, seed 7. Reports the 5th–95th percentile of
  Sharpe(D) − Sharpe(B).

## Criteria (identical to the sleeve gate; D survives only if all hold)

| id | criterion |
|---|---|
| C1 | Sharpe(D) > Sharpe(B) |
| C2 | Sharpe(D) > Sharpe(B) in ≥ 2 of 3 thirds |
| C3 | maxDD(D) ≤ 0.75 × maxDD(B) |
| C4 | Sharpe(E) < Sharpe(B) |
| C5 | C1 at stress cost |

Verdict: `SURVIVES` or `KILLED`. `significant` additionally requires a
bootstrap 5th percentile > 0.

## Registered prior and what each outcome means

- **Prior:** B beats A on Sharpe (vol targeting helps in a
  vol-clustering asset). D vs B is close to a coin flip on C1. C3 is
  likely to hold.
- **What this test is not:** monthly decisions lag the daily sleeve by up
  to a month, and 6 monthly returns are a noisier vol estimate than 21
  daily ones. It is one asset over 12 years.
- **KILLED:** lowers the prior that the daily sleeve clears its C1. The
  vol-managed buy-and-hold (B) becomes the stronger product candidate.
- **SURVIVES:** raises that prior. It never replaces the daily gate on
  the operator's data.
