# Trade plan and trade cycle

This is the plan Hydra trades, and the routine an operator follows each
session. Each rule says where its evidence comes from: a test in this
repository, or written trading lore that has **not** been tested here. Treat
the second kind as a hypothesis, not a fact.

## What the evidence says wins on a retail spot account

People who made lasting money trading crypto spot mostly did one of four
things:

1. They held the asset through its uptrends.
2. They provided liquidity (market making).
3. They ran infrastructure or carry trades.
4. They had an information edge.

Only the first is open to a retail spot account on Kraken behind a 2-second
REST floor. The question is how to hold crypto's upside without its 75–90%
drawdowns. The record of the best-known traders and the tests in this repo
agree on the answer: **follow the trend on a slow clock, size by volatility,
and cut the position when the trend breaks.**

| Principle (who) | Tested here? | Result | In Hydra |
|---|---|---|---|
| Trade with the major trend; the 200-day line (Paul Tudor Jones, Faber) | yes: K3, K3b on independent monthly BTC 2013–2024 | Timing alone ≈ buy-and-hold Sharpe (0.83 vs 0.82), but max drawdown 70% vs 79% | Sleeve term `close > SMA200` |
| Breakouts and channel exits (Donchian; Dennis and the Turtles) | in the ensemble only | Not tested on its own here | Sleeve term: Donchian 55-day in / 20-day out |
| Moving-average trend (Seykota; EMA crossovers) | in the ensemble only | Not tested on its own here | Sleeve term `EMA20 > EMA100` |
| Size by volatility, not conviction (Turtle "N", Van Tharp) | yes: K3b | **Largest single effect**: Sharpe 0.82 → 0.98, drawdown 79% → 50% | `vm = clamp(30% / realised vol, 0.2, 1)` |
| Trend + volatility sizing together | yes: K3b, registered before it ran | Sharpe 1.05 vs 0.98, drawdown 33% vs 50%. Wins 3 of 3 sub-periods. **Not significant** (range −0.13…+0.37) | The trend sleeve |
| Cut losses; exit when the trend breaks (Livermore, Seykota) | yes (as above) | The drawdown cut is the main benefit | Sleeve exits when the score < 0.6 |
| Let winners run, but keep the bet the same size | yes: K3b post-hoc at the 0.40 cap | Never trimming: drawdown 58%, the 15% breaker trips in year one. Monthly re-sizing: 14% drawdown, never trips | Re-size every 30 days (trim / top-up) |
| Pyramid into winners (Livermore, Turtles) | indirectly (as above) | Growing exposure into a rally is what blew the drawdown up | **Not used** — deliberate deviation |
| Few, patient trades (Livermore: "the big money is in the sitting") | yes: engine history | The 1h engine traded 11 times in 3 years and earned +1%. Its patience was not the problem; it never held the trend | Daily decisions only, ~2–4 round trips/yr per asset |
| Never risk ruin (Jones: "defense first") | n/a (risk rule) | — | Spot only, no leverage, 40% cap, sticky 15% breaker |
| Systematic, no discretionary overrides (the Turtle experiment; Dunn, Henry) | n/a | Overrides make live trading differ from what was tested | The sleeve's exits and trims are protected from the LLM, rules and coordinator; LLM vetoes are not part of the tested system |
| Predict the next candle (quant / HFT) | yes: quantum kernel + classical models on real Kraken bars | AUC 0.50–0.51: no information | **Killed** |
| Buy the dip / mean reversion (S3) | yes: 69-trade ledger | Positive but not significant (BTC t = 0.74, ETH t = 1.59) | Shadow only, no orders |
| Concentrate when conviction is high (Druckenmiller, Soros) | no | No measure of conviction survived testing here | Not used |
| Trade less, lose less (Barber & Odean) | lore | — | Daily clock; companion proposals are paper by default |

**Honest limits.**

- The independent tests are monthly and BTC-only.
- The daily sleeve has a pre-registered gate, but that gate has **not yet
  run** on the 11-year store. It lives on the operator's machine (see
  *Evidence* below).
- Nothing here predicts prices. The plan earns when crypto trends up and
  holds cash otherwise. In a long bear market its correct output is ≈ 0%.

## The trade plan (exact rules)

| Rule | Value |
|---|---|
| Universe | BTC/USD, ETH/USD, ZEC/USD (`--pairs auto` adds held assets). Each pair is independent. |
| Clock | One decision per UTC day, on the **completed** daily close. The forming day is never read. |
| Signal | `0.4·[close > SMA200] + 0.4·[EMA20 > EMA100] + 0.2·[Donchian 55-in / 20-out long]` |
| Long | Score ≥ 0.6, i.e. any two of the three terms |
| Entry | Post-only limit at the touch; re-priced if the market leaves it (`HYDRA_ORDER_REPRICE_*`) |
| Size | `equity × max_position_pct (0.40 in competition) × vm`, capped by the gross-inventory limit |
| Re-size | Every 30 days back to the target, unless within 10%. The trim is a partial SELL. |
| Exit | Full close when the score drops below 0.6 |
| Catastrophe stop | 15% drawdown on an engine, or on the portfolio: flatten, no new BUYs until the operator resets |
| Not part of the plan | Intraday signals, LLM opinions, dip buying, pyramiding, leverage, discretionary entries |

The engine publishes the plan for every sleeve pair in its state,
`trend_sleeve.plan`, so the dashboard and logs show it:

- **The trigger:** `enter_above` / `exit_below`, the daily close that would
  flip the decision.
- **Each term's own level:** `levels.sma200`, `levels.ema20_over_ema100`
  and the Donchian bounds.
- **When it decides:** `decides_at_utc`.
- **The breaker:** `breaker_price`, the price where this engine's breaker
  would fire, with `breaker_reachable: false` when cash alone keeps equity
  above 85% of peak.

These are exact. Tests feed the engine a close just past each level and
confirm the decision flips.

## Who decides, per pair

The variable is `HYDRA_TREND_SLEEVE`:

| Value | Effect |
|---|---|
| unset / `auto` (default) | A pair runs the sleeve only if `research/data/trend_sleeve_gate.json` (or `HYDRA_TREND_SLEEVE_GATE`) shows the conditions below. Otherwise it stays on the 1h rails engine, which in practice holds cash. |
| `1` | The operator's call, every pair (e.g. after a PASS that was not significant). |
| `0` | Off everywhere. |

The conditions for `auto`:

- an overall verdict **PASS**;
- **significant**;
- built for the current rules;
- less than 180 days old (`HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS`);
- a PASS for that pair's base asset, with the engine check passing.

Boot prints one `[STRATEGY]` line per pair saying which strategy runs and
why. The dashboard receives the same decision as `strategy_gate`.

## Evidence: run the gate on your own data

```bash
python -m tools.refresh_history                 # bring hydra_history.sqlite up to date
python tools/trend_sleeve_gate.py --engine      # pre-registered gate, ~minutes
python -m hydra_strategy_gate                   # what Hydra will trade, per pair
```

The criteria are fixed in `research/data/trend_sleeve_REGISTRATION.md`, so a
result cannot be argued with after the fact. Re-run the gate every quarter.
Evidence older than 180 days switches `auto` back off.

## The trade cycle

**Before the session (when it starts, or each morning)**

1. The dashboard is green: candle, ticker, balance and execution streams are
   healthy, and the dashboard is authenticated.
2. Check the breaker on every engine and on the portfolio. If one is halted,
   read why before setting `HYDRA_RESET_CIRCUIT_BREAKER=1` for one restart.
3. Check for resting orders: no `PLACED` row older than the reprice window
   without a reason.
4. Read the plan for each pair: state, the trigger level, and the distance
   to the breaker price.

**At the decision (first tick after 00:00 UTC)**

5. The sleeve scores the completed day.
   - If the state changes, it places one post-only order.
   - If a re-size is due and outside the band, it places a trim or top-up.
   - Otherwise it does nothing until tomorrow.

**During the day**

6. Sleeve pairs act only on breaker flattens and order hygiene (fills,
   reprices). Intraday moves are not a reason to act. That is the plan.

**After the session (weekly)**

7. Review the journal. For every fill:
   - the reason (`TREND_SLEEVE:enter|exit|trim|topup`);
   - the fill against the plan level;
   - fees paid.
8. Compare the drawdown with the plan's breaker distance, and slippage with
   the 10 bps the gate assumed.

**Every quarter**

9. Refresh the history and re-run the gate. Hydra follows the new verdict on
   the next restart.

## What this plan will not do

- Make money when crypto falls; spot-only, long-or-flat.
- Make money every month. Trend following has long flat and whipsaw
  periods.
- Turn a small account into millions. Returns scale with capital and with
  the trend; the edge is risk control, not prediction.
