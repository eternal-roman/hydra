# ABI Round 3 — Synthesis funnel + executed bakeoffs: what materialized (2026-07-18)

> Round 3 of `/abi-discovery`. Rounds 1–2 (`ABI_FUNNEL_2026-07-18.md`,
> `ABI_FUNNEL_STOPS_2026-07-18.md`) produced four survivors with frozen
> pre-registered designs; this round's falsification funnel EXECUTED all
> four via `/bakeoff` (two dedicated agents; runners committed as
> `tools/bakeoff_*.py`, evidence in `evidence/bakeoffs/*.json`) and adds
> one new verified mechanism that reframes the whole program.

## The round-3 mechanism (new, verified in-session)

**M-R3: the bounce label's payoff COMPOUNDS with holding horizon — it never
decays.** Forward-return spread (oracle reversal − fake) from the close of
bounce+1, computed on every labeled setup:

| Dataset | k=1 | k=5 | k=20 | k=50 bars |
|---|---|---|---|---|
| BTC 1h (n=4851) | +0.38% | +1.27% | +2.33% | +2.65% |
| BTC 1d (n=182) | +1.00% | +6.96% | +14.40% | +16.09% |
| ETH 1d (n=178) | +1.78% | +9.66% | +23.63% | +33.05% |
| ZEC 1d (n=169) | +4.06% | +14.06% | +27.09% | **+38.81%** |

The label is a regime-identification call, not a microstructure call. This
explains rounds 1–2 in one line: the 3.3·ATR target (~10–15% at daily)
amputates a +16–39% payoff — which is why target/stop arms lose everywhere,
long-hold exits win wherever drift exists, and fees only bind when the
construction forces short holds. It also means the S3 P&L design below
(frozen with the tgt3.3 exit before this measurement existed) understates
the classifier's value — confirmed by its exploratory exit variants.

## Bakeoff verdicts (all four pre-registered designs executed)

| # | Candidate (origin) | Verdict | Decisive numbers |
|---|---|---|---|
| 1 | **S3 daily candle-only classifier** (round 1) | **PROMOTE (provisional)** | Pooled WF AUC **0.617** (≥0.60 ✓); gated expectancy net of 26 bps **BTC +1.03, ETH +2.25** %/trade (ZEC −0.88; 2/3 ✓); inverse worse than ungated pooled ✓ (BTC per-asset fails → `PROMOTE_under_strict_c3_reading: false` recorded). Gated totals: BTC +19.8% (ungated −83.3%), ETH +56.4% (ungated −66.5%) |
| 2 | Capitulation-freshness gate (round 1) | **FAIL** | Direction real on 3/3 (+1.6…+2.6%/trade vs ungated, inverse ordering correct) but fresh-gated positive in only 30–50% of folds and pooled totals still negative — improves a loser without making a winner |
| 3 | Close-confirmed exits (round 2) | **FAIL** | (iii)−(i) +0.19%/trade < +0.3 bar; the mechanical control (touch trigger, close FILL) captures ~95% of the effect and late confirmation costs nothing — it was fill mechanics, not information; all arms net-negative |
| 4 | Envelope-1h layer (round 2) | **FAIL** | Loses to its exposure-matched beta control (Σlog-eq 12.28 vs 14.50; fold wins 8/24); maxDD bar cleared on 1/3 assets. Inverse loses ✓ and ZEC fails as predicted ✓ — coherent regime beta, but not better than its own beta |

Three honest kills and one provisional promotion. The controls did the
work: the beta-matched control killed #4, the mechanical-fill control
killed #3's story, the fold-consistency bar killed #2.

## What materialized — the profit path

**The only construction that survived its gate:** daily-grain bounce
setups on BTC/ETH, entered only when the candle-only classifier (trained
walk-forward on the full archive; `range_atr` is the dominant feature on
all three assets, not `clv` as round 1 guessed) scores above the train-p75
threshold. Registered-exit economics: +1.0–2.2%/trade net of taker fees,
win rates 0.63–0.65. The exploratory exit variants (labeled, not part of
the verdict) point where M-R3 predicts: close-fill stops add +0.1…+2.1
%/trade (flips ZEC positive), and long-hold flip exits add +6…+18 %/trade
on BTC/ETH (regime beta on top of selection — legitimate to hold, per
M-R3, but needs its own gate before being claimed as edge).

**Known weaknesses, stated plainly:** ~19–30 gated trades/asset over 7–11
years (~2–4/yr — thin); BTC pooled AUC 0.598 sits at the bar; the BTC
inverse arm outperformed ungated (interpretation fragility recorded in the
JSON); ZEC stays excluded (negative gated expectancy, consistent with its
classifier FAIL). Heartbeat's 1h flow posterior remains bakeoff-passed on
BTC/ETH as a *prediction* layer and is the designated second-stage
confirmer — its merit is real but it is not itself the P&L engine.

## Exit state (for resumption)

1. **S3 status: PROMOTE (provisional)** — next steps in order:
   (a) paper-shadow window (the JSON's POWER caveat: thin trade counts);
   (b) a follow-up pre-registered gate for the exit question (registered
   tgt3.3 vs close-fill stop vs flip exit — M-R3 says the registered exit
   truncates the payoff; do NOT silently swap exits without the gate);
   (c) integration planning agent → wire as signal-input + strategy
   surface behind an env flag, heartbeat confirmer on BTC/ETH only.
2. Freshness/close-confirm/envelope: recorded kills; do not revisit
   without new anomalies. Close-FILL (not confirm) is a legitimate
   mechanical improvement to any level-triggered research exit.
3. The 1h bounce family stays dead (fee/range 0.54–0.84); daily is the
   monetization grain (0.07–0.13), 4h transitional (0.21–0.38)
   (`bounce_geometry_4h.json`).

Evidence: `evidence/bakeoffs/{s3_daily_classifier,freshness_gate,close_confirm_exits,envelope_1h}.json`;
runners `tools/bakeoff_*.py`; horizon measurement reproducible via the
snippet in this doc's session (setups from `paper_bounce_sim.causal_setups`
on `candles_from_sqlite(db, pair, 24)`).
