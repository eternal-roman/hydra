# Pre-registration — post-only fill / adverse-selection measurement (ABI frame F8)

Registered 2026-10-01 before running `tools/abi_trend_killtests.py --k2`.

Frame (foreign domain: auctions — the winner's curse). A resting post-only
order is a free option written to the market: it fills when price moves
THROUGH it (against you) and is left behind when price moves your way.
Hydra places BUY at the bid and SELL at the ask, books the fill
optimistically, and never re-prices or expires a resting order.

Data: real Kraken 1h OHLC in `s3bounce/tests/fixtures/parity_{BTC,ETH,ZEC}_USD.json`
(`hourly_sample`, 714 bars each, 2026-06-20..2026-07-19).

Fill rule (conservative, queue-aware): an order resting at the decision
bar's close c_t fills in a later bar only if price trades THROUGH it
(BUY: low < c_t; SELL: high > c_t). Context: downtrend = c_t < mean of the
prior 24 closes; uptrend = c_t > that mean.

F8 SURVIVES (=> the execution layer needs a re-price/TTL policy) iff BOTH:
1. P(SELL fills within the next 1 bar | downtrend) < 0.80, AND
2. mean close-to-close return over the 24 bars after a SELL that did NOT
   fill within 1 bar is negative (the exits left resting are the ones that
   needed to happen).
Reported, not gated: the mirror statistics for BUY in uptrends, and fill
probability within 4 and 24 bars.
