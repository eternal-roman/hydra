# The S3 Bounce Edge: Classifying Continuation Legs on Daily Crypto Tape — Gates, Trade Ledger, and the Limits of Holding Winners

**HYDRA Research · July 2026 · v1.0**

*Companion paper to `RETAIL_CRYPTO_EDGE_2026.md`. All results generated on
Kraken 1h OHLC tape (`hydra_history.sqlite`, refreshed 2026-07-19)
resampled to UTC daily bars; walk-forward folds train strictly before
test; 26 bps/side taker fees throughout. Gate evidence (registrations
committed before runners) in `heartbeat/evidence/bakeoffs/`; study data
and the per-trade ledger in `research/data/s3/`; the shipped algorithm is
the `s3bounce/` package (v2.30.0). Not financial advice.*

---

## Abstract

We document the full evidence arc of S3, a daily-bar bounce-leg
continuation classifier now shipped (shadow-only) in a production spot
system: (1) a six-feature walk-forward logistic gate over swing-low
bounce setups clears its pre-registered promotion gate on BTC and ETH
(win rates 0.64–0.70 under a 3.3·ATR target / bounce-low stop) and
fails on ZEC; (2) a pre-registered exit-policy gate replaces the
touch-stop with a close-fill stop (BTC +1.17, ETH +3.34 %/trade net)
and **kills** trend-flip exits on fold consistency despite +13%/trade
pooled — the gain was regime beta, not exit information; (3) a
hold-horizon study **refutes** "the classifier is right every time at
K=20/50": per-entry hit rates at K≥20 are statistically coin-flip on
every asset (all Wilson 95% CIs include 0.5), and long-hold averages
are lottery-shaped (ETH K=50 mean +13.1%, median −4.1%); (4) an
anomaly-driven discovery cycle locates the real residual structure —
the fixed target truncates winners (+9.8%/+13.0% BTC/ETH mean
continuation in the 40 bars after target exits) and bounce vigor at
entry watermarks losses (weak-bounce entries stop 48% vs 20%) — and
two trailing-exit constructions built on it clear expectancy and
exposure controls yet **fail leave-one-year-out stability**, so the
pre-committed decision rule denies the basis flip and relegates both
to measurement-only shadow arms. The through-line: every construction
that extends exposure looks brilliant on pooled averages and survives
only as far as its concentration allows; the durable edge is
short-horizon bounce quality, and the honest system trades exactly
that while shadow arms accumulate live evidence on everything else.

---

## 1. Algorithm under test

**Setup detection (causal):** swing low (SW=2) with ≥2 lower swing lows
below the 9-bar MA in a 30-bar lookback, crash exclusion (any bar range
> 3·ATR in the last 4), bounce trigger high ≥ low + 1.0·ATR, entry at
the close of the first bar after the bounce whose low holds (b1). The
2-bar swing confirmation lag coincides with the entry decision bar, so
no look-ahead exists at entry (`s3bounce/setups.py`, verbatim port of
the research pipeline, parity-pinned at 1e-9).

**Classifier:** logistic regression (L2=1.0) on six frozen features —
`clv`, `range_atr` (dominant), `vol_z`, `shock_recency`, `breadth`,
`retest` — trained per expanding yearly fold on setups *resolved*
before the fold cut (label leakage includes resolution time). Gate =
train-p75 of predicted probability. Artifact: 2026 fold weights,
BTC threshold 0.5677, ETH 0.5575 (`s3bounce/model_artifact.json`).

**Exit basis (X1, gate-adopted):** stop on daily close < bounce low
(fill at that close), target at low + 3.3·ATR, 200-bar horizon.

## 2. Gates run (chronological, all pre-registered)

| gate | registration | verdict |
|---|---|---|
| classifier promotion | ABI funnel §6 (2026-07-18) | PASS BTC/ETH; FAIL ZEC — `heartbeat/evidence/bakeoffs/s3_daily_classifier.json` |
| exit policy (X0/X1/X2/X3/T_K) | `s3_exit_policy_REGISTRATION.md` | ADOPT X1 close-stop; flip/hybrid KILLED (C2 5/13) — `s3_exit_policy.json` |
| hold horizon (K=1..60) | user-directed study | NO blind K adopted; ETH K=60 shadow arm only — `research/data/s3/s3_hold_horizon.json` |
| trail exits (X4a/X5/T_K) | `s3_trail_exit_REGISTRATION.md` | NO basis flip; both SHADOW_ARM_ONLY (X4a failed C5 LOYO, X5 failed C3) — `s3_trail_exit.json` |

Killed research lines with evidence retained for revisit
(`research/data/s3/killed/`): freshness gate, close-confirm exits,
envelope-1h. Do not revisit without new anomalies.

## 3. The trade ledger (what real money would have seen)

Walk-forward out-of-sample X1 trades, 26 bps/side deducted. Full
per-trade rows (dates, prices, features, forward returns):
`research/data/s3/s3_trade_ledger_x1.json`.

| asset | n | right (target) | wrong (stop) | hit rate (Wilson 95%) | avg/trade | avg win | avg loss | avg hold |
|---|---|---|---|---|---|---|---|---|
| BTC/USD | 23 | 16 | 7 | 69.6% (49–84%) | **+1.17%** | +5.3% | −8.4% | 10.4 d |
| ETH/USD | 28 | 18 | 10 | 64.3% (46–79%) | **+3.34%** | +10.0% | −8.6% | 12.8 d |
| ZEC/USD | 19 | 13 | 6 | 68.4% (46–85%) | +1.22% | +8.2% | **−13.9%** | 8.5 d |

Structural facts the summary hides:

- **Every wrong flag exits at a loss — 23/23.** The stop only fires on
  a close below the bounce low, which is always below entry. There is
  no "wrong but lucky" exit; the stop is pure insurance premium.
- **Roughly 40% of stops are whipsaws** (thesis-correct): 6–7 of 17
  BTC+ETH stops saw fwd60 ≫ 0 after stopping (extreme: ETH 2022-06-17
  stopped −8.9%, +72.5% sixty days later). True saves cluster at
  cascade onsets (2021 top → 2022), where the stop avoided −44 to
  −60 pp. Regime-pricing the stop was tested and killed — save-value
  is symmetric around MA200 (4/8 vs 4/9 saves).
- **ZEC's exclusion is loss-geometry, not hit rate:** equal win
  frequency but −13.9% average loss (worst −26.7%) on an illiquid
  book, plus a failed classifier AUC gate.

## 4. The hold-horizon refutation

Per-entry forward returns k=1..60 with Wilson CIs and 10k-bootstrap
lower bounds (`research/data/s3/s3_hold_horizon.json`):

- Hit rates at K≥20 include 0.5 in every CI on every asset (BTC K=20:
  14/24; ETH K=50: 15/32; ZEC K=50: 5/21). The "always right if held"
  impression came from one-position sequencing dropping clustered
  entries and means carried by a few huge winners.
- Optimal-hold distributions are **bimodal** everywhere (BTC: 8 trades
  peak at 1–5 d, zero at 6–10, 8 at 46–60): a bounce leg either fails
  fast or rides a regime for months. "Optimal K" is a category error —
  the population is a mixture of two species.
- The single defensible long-hold is ETH K=60 (bootstrap LB
  +4.6%/trade, LOYO-stable) — shipped as a shadow arm only, because
  its profile is lottery-shaped (median −4.9%, top-3 trades ≈ half of
  P&L).

## 5. The discovery cycle and the trail gate

An anomaly→bore→ideate cycle (`heartbeat/evidence/abi/`
`s3_trail_funnel_2026-07-19.md`; 15 frames, 8 killed by cheap tests)
established two mechanisms:

**M1 — winner truncation.** Post-target continuation averages +9.8%
(BTC) and +13.0% (ETH) over the next 40 bars, ~66% positive. The
system's asymmetry was inverted: winners truncated at 3.3·ATR, losers
held to a slow stop.

**M2 — bounce vigor.** `premium_atr` = (b1 close − low)/ATR. Weak half:
stop rate 0.48, fwd60 +3.8%. Strong half: 0.20 and +20.7%. Real
information, not target-proximity geometry (the fwd60 differential is
5×). Secondary structure, registered and reproduced: breadth *inverts*
across horizons (low-breadth entries carry +23.3% fwd60 vs +1.3% —
idiosyncratic washouts run, systemic bottoms chop); trend-ensemble
state does NOT watermark losses (counter-trend entries continue
HARDER, +14.9% vs +9.4% fwd60 — which is why flip exits died and why
the S3 path must bypass the trend overlay).

Two constructions were registered and run once
(`s3_trail_exit_REGISTRATION.md`, criteria frozen before the runner):

| arm | pooled avg/trade | vs exposure control | fold consistency | LOYO | decision |
|---|---|---|---|---|---|
| X1 incumbent | +2.31% | — | — | — | remains basis |
| X4a ride-MA9 | +5.79% | beat T_10 (+3.66) | 9/13 | **FAIL** (flips on drop-2019) | shadow arm |
| X5 vigor-routed | +3.68% | +0.02 pp vs T_10 — **FAIL** | 10/13 | — | shadow arm |

X4a's pooled dominance rests on one fold (ETH 2019: +98.9 pp vs X1's
+37.1); remove it and the verdict flips. X5's exploratory appeal
(+228.8% pooled sum) evaporated to statistical equality with a blind
10-day timer once its routing cut was train-derived. The pre-committed
decision rule held: **no basis flip; both arms shadow-tracked** with
persisted trail state (`x4a_trail_ma9`, `x5_vigor_routed` in the
artifact; the exporter hard-fails if either is ever promoted past its
gate decision).

## 6. Lessons learned (distilled)

1. **Pooled expectancy is the most seductive liar in small-n trading
   research.** Three separate constructions (flip exits, blind K-holds,
   trails) beat the incumbent by 2–6× on pooled averages and every one
   failed a consistency or stability criterion. At n≈50, one regime
   fold buys the whole verdict.
2. **Exposure controls are mandatory for any exit that holds longer.**
   T_K blind timers matched at median hold repeatedly explained most of
   a "signal's" gain (killed envelope-1h, X5, half of X4a's margin).
3. **Pre-commit the decision rule, not just the criteria.** The trail
   gate's PASS/shadow/drop mapping was written before the runner;
   when the seductive number failed stability there was no
   negotiation left to have.
4. **Stops on mean-reversion entries are insurance, not alpha** — 100%
   of stop exits are losses, ~40% are whipsaws, and their value is
   concentrated in cascade onsets that no tested entry-time state
   variable predicts. Price the premium; don't pretend it's a signal.
5. **The overlay boundary is real:** the daily trend ensemble that
   dominates the core system (companion paper §3.3) must NOT gate S3 —
   capitulation entries are counter-trend by construction and their
   continuation is *stronger* below the trend line.
6. **Shadow arms are the honest resolution of "promising but
   unstable"** — structurally unable to trade, unable to be forgotten,
   accumulating exactly the live evidence the next gate needs.

## 7. Reproduction

- Ledger + watermarks: `heartbeat/tools/abi_s3_watermarks.py`
- Trail gate: `heartbeat/tools/bakeoff_s3_trail_exit.py`
- Exit-policy gate: `heartbeat/tools/bakeoff_s3_exit_policy.py`
- Hold horizon: `heartbeat/tools/s3_hold_horizon_study.py`
- Yearly refit + artifact drift check: `heartbeat/tools/export_s3_model.py`
- Package parity: `cd s3bounce && python -m pytest tests/` (golden
  fixtures at 1e-9 on real windows)

All runners consume `hydra_history.sqlite` (refresh first:
`python -m tools.refresh_history`). Narrative ledger of every verdict:
`heartbeat/HONEST_FINDINGS.md`.
