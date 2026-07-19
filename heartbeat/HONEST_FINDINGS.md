# HONEST_FINDINGS — heartbeat + S3 verdict ledger (compacted 2026-07-19)

Every claim here is backed by a committed evidence file; every gate was
pre-registered before its runner executed. The full research narrative
(methods, per-trade ledger, lessons) now lives in
`research/S3_BOUNCE_EDGE_2026.md` with study data promoted to
`research/data/s3/`. This file is the compact ledger of what was
decided, on what evidence, and what may not be revisited without new
anomalies.

## The core question — ANSWERED (real tape, 2026-07-19)

> Does the heartbeat separate fakes from reversals at bounce+3?

90d of real Kraken trades per asset, REST-backfilled at the 2s floor,
cross-verified against `hydra_history.sqlite` (`evidence/tape_verify_*`)
and mirrored into its `trades` table. Walk-forward, train strictly
before test (`evidence/real_tape/`):

| asset | events | fold AUCs @ bounce+3 (calibrated) | verdict |
|---|---|---|---|
| BTC/USD | 69 | 0.90, 0.55, 0.84 (mean 0.76) | **PASS** |
| ETH/USD | 77 | 0.73, 0.77, 0.69 (mean 0.73) | **PASS** |
| SOL/USD | 80 | 0.62, 0.65, 0.40 (mean 0.56) | FAIL |
| ZEC/USD | 75 | 0.61, 0.76, 0.46 (mean 0.61) | FAIL |

Liquidity-consistent: flow signal exists on deep majors only.
Calibration is mandatory (uncalibrated AUC ≈ 0.55 everywhere). ZEC's
paper-sim "edge" was regime beta (inverse control +24.6% — everything
long ZEC won that window; `evidence/paper_bounce_sim_ZEC.json`).

## Engineering guarantees (each regression-tested, evidence committed)

- No lookahead: incremental state bit-identical to prefix replay.
- Determinism: identical SHA-256 across replays (`gate2_*.txt`).
- Exact calibration transfer: `L = Σ wᵢSᵢ` — fitted weights ARE the
  live posterior's weights.
- Feed integrity: reconnect/backfill order-preserving; incomplete
  backfill, clock skew >2s, sequence violations ⇒ taint; tainted
  events excluded from eval.
- Canonical-store audit (fixed): ~50% missing 1h rows + frozen
  forming candles found and healed from trade-level truth
  (2159/2159 hours exact post-heal); refresh now skips the forming
  candle on both paths.

## S3 verdict ledger (all gates pre-registered; chronological)

| gate / study | verdict | evidence |
|---|---|---|
| Classifier promotion (daily 6-feature logistic, train-p75 gate) | PASS BTC/ETH; FAIL ZEC | `evidence/bakeoffs/s3_daily_classifier.json` |
| HYDRA entry-gating bakeoff | structurally inconclusive — v2.28 config took ZERO BUYs in the window; a confirmation gate cannot be tested against no entries. Not "safe", not "useless": untested | `evidence/hydra_bakeoff*.json` |
| Exit-policy gate (X0/X1/X2/X3/T_K) | **ADOPT X1 close-fill stop** (+2.36 vs +1.72 %/trade pooled, C2 8/13, tails strictly better). **KILL flip/hybrid** (C2 5/13; +13.2%/trade was fold-concentrated regime beta; flip median hold = 1 bar — degenerate entry/exit-state interaction) | `evidence/bakeoffs/s3_exit_policy.json` + REGISTRATION |
| Hold-horizon study (k=1..60, Wilson CIs, bootstrap LB, LOYO) | **"right every time at K=20/50" REFUTED** — per-entry hit rates coin-flip on ALL assets (every CI includes 0.5); large-K averages lottery-shaped (ETH K=50 mean +13.1%, median −4.1%); optimal holds bimodal (1–5 vs 46–60 bars). Only ETH K=60 has positive boot-LB (+4.6%) and LOYO stability → shadow arm ONLY | `research/data/s3/s3_hold_horizon.json` |
| ABI funnel (loss watermarks + winner truncation) | mechanisms: post-target +40d continuation +9.8/+13.0% (BTC/ETH); `premium_atr` vigor watermark (weak: 48% stop / +3.8% fwd60; strong: 20% / +20.7%); ensemble<0.6 does NOT watermark losses (counter-trend entries continue harder) | `evidence/abi/s3_trail_funnel_2026-07-19.md` |
| Trail-exit gate (X4a ride-MA9, X5 vigor-routed, T_K) | **NO basis flip.** X4a passed C1–C4 (+5.79 vs +2.31; beat T_10 control) but failed C5 LOYO (winner flips on drop-2019 — ETH_2019 fold +98.9pp carries it); X5 failed C3 (+3.68 vs T_10 +3.66 with train-derived cuts). Pre-committed rule → both **SHADOW_ARM_ONLY** | `evidence/bakeoffs/s3_trail_exit.json` + REGISTRATION |

## Registered per-coin basis (what real money may use)

| asset | basis | status |
|---|---|---|
| BTC/USD | X1 exit (close-fill stop L0 / tgt 3.3·ATR / 200-bar horizon), +1.17%/trade, win 0.696 | tradable basis, provisional pending shadow |
| ETH/USD | X1 exit, +3.34%/trade, win 0.643 | tradable basis, provisional pending shadow |
| shadow arms (both assets) | x0_registered, x4a_trail_ma9, x5_vigor_routed (+ hold_k60_stop ETH only) | measurement ONLY — exporter hard-fails if promoted past gate decision |
| ZEC/USD | none — classifier FAIL, worst loss-geometry (−13.9% avg loss), trail FAILS there | excluded; bars feed breadth only |

Structural facts: all 23/23 stop exits are losses (stop = insurance
premium, never alpha); ~40% of stops are whipsaws whose save-value no
tested entry-time state predicts; the CI-supported edge is
short-horizon bounce quality (win 0.64–0.70, bounded tails), NOT
continuation holding.

## Killed lines — do not revisit without new anomalies

Freshness gate, close-confirm exits, envelope-1h (evidence:
`research/data/s3/killed/`); flip/hybrid exits; blind K-holds; 1h
bounce family; regime-priced stops; winner's-curse margin; shock
annealing; MA200-front gating; time-stops (funnel kill-table has the
numbers). Monitored, underpowered: post-stop re-entry (n=7, fwd60
+33.0% vs +8.9%); breadth-horizon inversion (low-breadth fwd60 +23.3%
vs +1.3%) — both registered as shadow-window secondaries.

## Shipped state + outstanding gates

Shipped v2.30.0+: `s3bounce/` package (parity-pinned 1e-9, yearly refit
`tools/export_s3_model.py`) + `hydra_s3.py` signal surface and
`HYDRA_S3_STRATEGY` shadow phase (all arms logged in parallel to
`.hydra-s3/`; NO order path exists).

1. **Shadow window** is the final authority on every provisional
   verdict above — start it by setting `HYDRA_S3_STRATEGY=1`.
2. **Heartbeat network gates still unrun**: 24h live WS soak +
   socket-kill drill (mock-tested only); Tier 1 features enabled +
   re-gate on BTC/ETH is the next classifier step.
3. **Live wiring** (`HYDRA_S3_LIVE`) requires the shadow gate + its own
   `/bakeoff` — dedicated bounce-entry surface with heartbeat as
   confirmation layer; the trend overlay must NOT gate S3 entries
   (evidence above). No live wiring before that passes.
