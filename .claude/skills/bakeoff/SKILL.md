---
name: bakeoff
description: Use when the user says /bakeoff, or asks to evaluate whether a new signal, data stream, gate, or strategy variant improves HYDRA's trade outcomes — "does X give us edge", "test X against the current system", "should we integrate X". Also use before wiring any research subsystem's output into engine/agent decisions.
---

# Bake-off — candidate signal vs the current system, real data decides

Decide with evidence whether a candidate (signal stream, entry gate,
sizing modifier, strategy variant) improves trade outcomes when combined
with the CURRENT default HYDRA config. Never argue from theory or
synthetic fixtures — synthetic results validate machinery, not the
market hypothesis (precedent: `heartbeat/HONEST_FINDINGS.md`).

**Reuse before rebuilding:** `heartbeat/tools/hydra_bakeoff.py` (arm
runner: baseline/gated/inverse/OOS, percentile thresholds, leak-free
weight fitting) and `heartbeat/tools/verify_tape_vs_sqlite.py`
(cross-source data verification) are working templates. Adapt; don't
reinvent.

## Where to look

| what | where |
|---|---|
| canonical 1h OHLC (backtest source of truth) | `hydra_history.sqlite` (`ohlc`: pair, grain_sec, ts=open, source) |
| refresh sqlite to now (do this BEFORE windowing) | `python -m tools.refresh_history` |
| backtest runner (reuses live engine verbatim) | `hydra_backtest.BacktestRunner`, `SqliteSource`, mode=competition |
| gate injection point (no fork needed) | wrap `runner.engines[pair].execute_signal`; current candle = `engine.candles[-1].timestamp` (open ts) |
| sizing-candidate seam | `size_multiplier` arg of `execute_signal` (applies before the `max_position_pct` cap — PR-B) |
| stats (bootstrap CI, walk-forward, MC, regime P&L) | `hydra_backtest_metrics.py` |
| trade-level tape store + posterior | `heartbeat/` (`Store`, parquet under `heartbeat/data/`) |
| evidence JSONs (commit the verdict) | `.hydra-flywheel/<name>_gate.json` or `heartbeat/evidence/` |
| prior verdicts to mirror (incl. negative) | `trend_overlay_gate.json` (pass), `trend_entry_gate.json` (REJECTED), `bridge_isolation.json` (bridge off) |

## Protocol (order matters)

1. **Acquire real data.** Kraken REST at ≥2s intervals (project floor);
   backfills must stream to store (never accumulate in memory) and
   resume from last stored ts — `heartbeat backfill` does both. Size the
   job first (one Trades page ≈ 1000 trades; measure trades/day, print
   ETA). Multi-hour: run in background with a Monitor.
2. **Verify the data before trusting any result.** Aggregate the
   candidate's raw data into candles with its own builder and diff
   O/H/L/C/volume against `hydra_history.sqlite` for identical UTC
   buckets (`verify_tape_vs_sqlite.py` pattern). Material divergence =
   stop; fix data first. Also verify claimed evidence reproduces
   (digests are per-platform — `math.exp` is libm-dependent).
3. **Candidate's own gate first.** Walk-forward AUC / hit-rate on ≥60
   events per asset, train strictly before test. If the signal can't
   classify its own target out-of-sample, skip the backtest bake-off.
4. **Causality audit.** Gate value keyed by candle open_ts must be
   computable at that candle's CLOSE (same information time as the
   engine's decision tick; fills happen next candle). Anything derived
   "as-of-now" instead of point-in-time invalidates the arm.
5. **Arms** (same window, same config, only the candidate differs):
   baseline; candidate at P20/P35/P50/P65 thresholds of its own
   TRAIN-segment distribution (absolute levels are regime-dominated —
   never hand-pick); **inverse control** (flip the veto: if both
   directions "help", the effect is trade-count noise, not
   information); OOS arms on the held-out tail with weights AND
   thresholds fit only on train-resolved events.
6. **Verdict.** Pre-register criteria before running arms. Promote only
   if: OOS improvement consistent across thresholds (flat neighborhood),
   inverse control hurts or is flat, candidate's own OOS gate passed,
   and trade count stays meaningful. Few trades in the window = report
   "inconclusive — insufficient power", not a win. Commit the evidence
   JSON either way; negative results are recorded, not discarded.

## Integration rules (HIGH-severity invariants)

- Gates touch **BUY entries only**, SKIP semantics; SELL paths untouched
  (PR-A exit guarantees). Missing/stale/tainted signal **fails open**.
- New flag default OFF, kill-switch listed in CLAUDE.md env table, same
  change.
- Data streams are SIGNAL INPUT ONLY (spot-only execution invariant).
- Live promotion additionally needs: paper shadow run, mock harness
  (`tests/live_harness/harness.py --mode mock`), CI green, `/release`.

## Common failure modes

| trap | counter |
|---|---|
| thresholds tuned on the full window | derive from train segment only; report the whole sweep, not the best point |
| calibrated weights fit on the eval window | fit on events RESOLVED before the split (label leakage includes resolution time) |
| "improvement" from trading less in a drawdown window | inverse control + trade-count floor |
| synthetic/backtest-only evidence promoted to live | real-tape gates are mandatory; synthetic validates machinery only |
| gate silently blocks exits or dust drains | grep the wrapper: only `action == "BUY"` may be vetoed |
| sqlite stale vs tape end | refresh first; window = tape ∩ sqlite coverage |
| new backfill re-downloads or OOMs | resume from `Store.last_tape_ts`; stream pages via `on_page`, `collect=False` |
