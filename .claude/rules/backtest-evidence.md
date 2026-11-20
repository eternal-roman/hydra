---
paths:
  - "hydra_backtest*.py"
  - "hydra_walk_forward.py"
  - "hydra_flywheel.py"
  - "hydra_experiments.py"
  - "hydra_s3.py"
  - "tools/**"
  - "research/**"
  - "heartbeat/**"
  - "s3bounce/**"
---

# Backtest / evidence invariants (HIGH severity if violated)

Moved verbatim from `CLAUDE.md` §Cross-cutting invariants. Loads when the
files above are touched.

- **Backtest candles align on TIMESTAMP, never on index (v2.32.1)** — `BacktestRunner._loop` advances only the pairs whose next bar sits at the earliest timestamp across pairs; a pair with no bar there is absent for that tick. Index-zipping silently accumulated cross-pair skew on any gapped series (which the real sqlite store has — `HistoryStore.coverage` tracks `gap_count`), corrupting coordinator decisions and the summed equity curves that `tools/flywheel_validation.py` writes as the evidence gate. Never reintroduce a bare `next(it)` per pair per tick. The replay surface is engine rails, the portfolio buy halt, the UTC session weight, and the coordinator (including Rule 4 prices and swap legs). It does not invent derivatives indicators, an order book, or a brain. `BacktestResult.decision_surface` says so.
- **Evidence tooling is calibrated against a null (audit 2026-10)** — the Monte Carlo trade bootstrap is circular with a block of at most round(n^(1/3)) trades; the old non-circular L=20 draw put the 95% CI above zero for 26% of zero-edge series at n=25 (nominal 2.5%), so `mc_ci_lower_positive` passed coin flips ~10x too often (`tools/mc_bootstrap_calibration.py`, `research/data/abi/mc_bootstrap_calibration.json`). `walk_forward` slices every pair on one union-timestamp clock and seeds each slice's daily overlay from the slice start. Synthetic bars start at `SYNTHETIC_EPOCH_S`, never `time.time()` (I12: the UTC-hour session weight made results depend on run time). The realistic fill model does not fill a doji that only wicked the limit, and no model fills a flat zero-volume (no-trade) bar. Heartbeat evaluation scores a checkpoint only while the label is unresolved at its close (`idx < resolve_idx`); scoring resolved events leaked the outcome (null tapes reached AUC ~0.60). The 2026-10-02 re-run of that fix withdraws the BTC/ETH PASS (`research/data/abi/heartbeat_leakfix_2026-10-02.json`).
- **Trade-level Sharpe annualizes by sqrt(TRADES/yr), not sqrt(bars/yr) (v2.32.1)** — `monte_carlo_resample` operates on one return per TRADE. Scaling those by `annualization_factor(candle_interval_min)` inflated Sharpe ~93x on 60m bars and made the `mc_ci_lower_positive` rigor gate unable to bind. Pass `trades_per_year`, or accept the honest unannualized per-trade figure.
