# heartbeat — order-flow posterior for Kraken

A recursive Bayesian posterior **P(up) / P(down)** computed per forming
candle from live Kraken order flow, with a rolling ~30-candle evidence
memory. It is **not** a standalone signal generator: it is a
**confirmation classifier** for an existing trailing-stop bottom-buy
system. When a counter-trend bounce is forming, heartbeat's job is to
discriminate real reversals (persistent aggressive taker buying) from
fakes (passive-fill bounces with evaporating pressure).

Primary asset BTC/USD; generalizes to ETH/USD, ZEC/USD, SOL/USD via
config. Python 3.11+, no GPU. Self-contained package — nothing in here
imports from or is imported by the HYDRA engine.

## Install & test

```bash
cd heartbeat
pip install -e .[dev]          # or: pip install requests websockets pyarrow PyYAML pytest
python -m pytest tests/        # 71 tests: no-lookahead, determinism, fixtures, mocks
```

## CLI

```bash
heartbeat backfill --pair BTC/USD --tf 1h --days 90     # historical tape via REST Trades
heartbeat run --pair BTC/USD --tf 1h                    # live WS stream + P(up) per heartbeat
heartbeat eval --pair BTC/USD --tf 1h                   # labeler + metrics -> report
heartbeat calibrate --pairs BTC/USD,ETH/USD --tf 1h --walk-forward
heartbeat replay --tape data/BTC_USD/1h/tape/part-....parquet   # deterministic replay
heartbeat status                                        # feed health + current P(up)
heartbeat synth --pair BTC/USD --tf 1h --days 150 --seed 7      # offline validation tape
```

(Without an editable install, use `PYTHONPATH=src python -m heartbeat.cli ...`.)

`run` prints one machine-parseable line per heartbeat:

```
ts pair tf candle_progress P_up L OFI CLV vol_z [TAINTED]
```

plus a `CLOSE ...` summary line at each candle close.

## The estimator

Per heartbeat (one per trade; micro-bucketed to 500 ms when the trailing
1-second trade rate exceeds 20/s):

```
S_i(t) = λ_hb · S_i(t−1) + z_i(t) / h        (per-feature evidence sum)
L(t)   = Σ_i w_i · S_i(t)                     P(up) = σ(L)
```

* `λ_candle = 1 − 1/N` (N = 30 candles); `λ_hb = λ_candle^(1/h)` where
  `h` = rolling median heartbeats-per-candle, frozen at candle open —
  memory is defined in candle units, not tick units.
* The `1/h` evidence scaling makes the candle-close-sampled recursion
  match the candle-level recursion `L ← λ·L + w·z` regardless of trade
  rate (asserted by `test_candle_unit_memory`).
* `z_i` = raw feature value robust-scaled to ~[−1, 1] (median/MAD over
  trailing 500 candle closes, frozen at candle open, persisted).
* Because `L = Σ w_i S_i` **exactly**, a no-intercept L2 logistic
  regression on snapshot `S` vectors fits the live posterior in its true
  functional form — calibrated weights drop into `features.weights` with
  zero approximation gap.

Features are registered in tiers (`features/tier0.py` … `tier2.py`) with
name, tier, inputs, lookback, and a falsifiable hypothesis each. Tier 0
(OFI, CLV, range/ATR, volume z, OFI-momentum) is enabled by default;
Tier 1/2 stay dark until their gates pass (`features.enabled_tiers`).

## Integration contract (live confirmation mode)

`heartbeat run` exposes the posterior two ways; both carry the same JSON
payload (field list in `src/heartbeat/api.py`):

1. **Status file** — `api.status_file` (default `data/heartbeat_status.json`)
   is resolved **per pair** to `data/heartbeat_status_BTC_USD.json` (etc.)
   via `resolve_status_path` so multi-pair `heartbeat run` processes do not
   clobber each other. Atomically rewritten after every heartbeat
   (`tmp` + `os.replace`). Hydra polls the same paths:
   - dashboard surface: `hydra_heartbeat_surface` → `quant_indicators["heartbeat"]`
   - S3 shadow confirmer: `HYDRA_S3_HEARTBEAT_STATUS_DIR` (default
     `heartbeat/data`) + `heartbeat_status_<PAIR>.json`
2. **TCP query** — connect to `api.tcp_host:api.tcp_port` (default
   `127.0.0.1:8790`), send any line, receive one JSON line. The
   connection stays open for repeated queries.

```json
{"pair": "BTC/USD", "tf": "1h", "p_up": 0.6421, "L": 0.5843,
 "ts": 1752741032.417, "candle_progress": 0.47, "tainted": false,
 "gap_count": 0, "max_clock_skew_s": 0.213, "alerts": 0,
 "features": {"clv": {"z": 0.4, "raw": 0.7}, "ofi": {"z": 0.1, "raw": 0.05}}}
```

Consumer rules:

* Treat `tainted: true` as **no opinion** — never as 0.5.
* Treat a stale `ts` (older than a few heartbeat intervals; Hydra uses 300s)
  as feed loss.
* `p_up` is calibrated per pair/timeframe only after weights load.
  `heartbeat run` **auto-loads** the first hit of
  `weights_{PAIR}_{tf}.json` from `data/reports/`,
  `evidence/real_tape/`, then store root (`weights_io.find_weights`).
  Without a file it warns and uses `default_weight` (≈ coin flip).
* **No order path** from this contract — Hydra display + S3 shadow only
  until a pre-registered `/bakeoff` promotes a gate.

## Data integrity (fail-loud rules)

* Exchange timestamps only in the math path; local clock is used solely
  for skew monitoring (alert + taint above 2 s).
* WS reconnects backfill the gap via REST `Trades` cursor; if backfill
  is incomplete the gap window is tainted and every overlapping candle
  is flagged `TAINTED` in all outputs.
* Sequence violations (backwards exchange timestamps) taint the
  inversion window.
* Tainted events are excluded from eval/calibration.

## Determinism

Given the same tape and config, output is bit-identical across runs
(`heartbeat replay` prints a SHA-256 digest of the posterior series).
No wall clock, no unseeded randomness anywhere in the math path;
`synth` tapes derive entirely from `random.Random(seed)`. Digests are
**per-platform**: `math.exp` in the sigmoid is libm-dependent, so
Linux and Windows produce different (each internally stable) digests.

## Verification gates — status

| gate | offline evidence (this repo) | needs network |
|---|---|---|
| 1 feed+tape | store round-trip, replay digest identical, reconnect+backfill under mocked transport (`tests/test_ws.py`) | real 90-day `backfill`, live socket-kill drill |
| 2 engine+posterior | `test_no_lookahead.py`, `test_determinism.py`, hand-computed fixtures per feature | — |
| 3 labeler+eval | ≥60 events/asset on synth tapes, reports + example traces in `evidence/` | re-run on real tape |
| 4 calibration | walk-forward AUC tables, printed non-overlapping train/test ranges (`evidence/gate4_walkforward.txt`) | re-run on real tape |
| 5 live mode | API contract tests (`tests/test_api.py`) | 24 h soak on BTC |

Real-tape gates were run 2026-07-19 on 90d of SOL/BTC/ETH trades
(verified against `hydra_history.sqlite`): the promote gate **passed on
BTC and ETH** and failed on SOL — results, bake-off verdict, and
recommendation in HONEST_FINDINGS. Still outstanding: the 24h live WS
soak + socket-kill drill:

```bash
heartbeat run --pair BTC/USD --tf 1h        # 24h soak; then `heartbeat status`
```

Findings, caveats, and the promote/kill recommendation live in
[HONEST_FINDINGS.md](HONEST_FINDINGS.md).
