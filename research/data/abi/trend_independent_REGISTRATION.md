# Pre-registration — independent trend replication (ABI frames F14/F1)

Registered 2026-10-01 before running `tools/abi_trend_killtests.py --k3`.

Question: does the daily-trend edge claimed in `research/data/trend_results.json`
(Kraken sqlite, BTC 11y) reproduce on an INDEPENDENT price source? Source:
monthly BTCUSD 2012-01..2024-12 bundled with the `backtesting` PyPI package
(v0.6.6, `backtesting/test/BTCUSD.csv`, 156 bars) — different vendor, different
resolution, overlapping only partly with the Kraken window.

Arms (decide at month-end close t, hold month t+1; 26 bps/side per switch;
cash earns 0%):
- B&H
- SMA10: long iff close > 10-month SMA (monthly analogue of SMA200d)
- TSMOM12: long iff 12-month return > 0
- INVERSE control: long iff close <= 10-month SMA

Frame survives iff ALL: (1) SMA10 and TSMOM12 Sharpe > B&H Sharpe;
(2) both max drawdowns < B&H max drawdown; (3) INVERSE Sharpe < B&H Sharpe.
Convexity readout (F14, reported not gated): OLS of SMA10 monthly return on
[r, r^2] of the underlying; trend-as-long-straddle predicts beta2 > 0.
