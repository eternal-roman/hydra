# Q & A — Operator Discussion Record

*Companion to `RETAIL_CRYPTO_EDGE_2026.md`. Captures the strategic
questions asked of the research and the evidence-backed answers, so the
reasoning survives alongside the numbers. Updated as questions recur.*

---

**Q: Does it make sense to run HYDRA at all if I can park everything in
an S&P 500 index fund (FIDSP500) and call it done?**

For core capital: park it in the index fund — HYDRA should never
compete for that money. FXAIX-class funds delivered ~+9.6% in H1 2026
alone with a century of evidence, near-zero fees, and tax efficiency.
HYDRA's validated function is narrower: it is the safest available
vehicle for *crypto exposure you were going to hold anyway* — raw
holding cost −48% this year; HYDRA held ≈0% with a systematic re-entry
trigger armed. The quantified bet: to justify itself against ~10%/yr
equities over a 4-year cycle, the sleeve must catch roughly one
+50%-net bull leg per cycle while its cash floor earns the risk-free
rate. BTC's history (+121%, +156%, +304% calendar years) makes that
plausible; it is not proven. Take it only with satellite money.

---

**Q: Was 2025–26 a weird year, or is something broader wrong with the
financial climate?**

Neither. By crypto's own history this is the *fourth scheduled bear*
(2014 −57%, 2018 −74%, 2022 −64%, 2025-26 −43% rolling-1y — the
mildest so far); 31% of all rolling 1y BTC windows in our tape are
negative. And the broader climate was *favorable*: S&P +9.6% H1 2026 at
record highs, small caps +22%, while crypto decoupled downward on
record ETF outflows and leverage unwinds. The loss was
crypto-idiosyncratic, not systemic. (Paper §4.)

---

**Q: Some people clearly make money in crypto — why didn't we?**

Because the durable profit-takers occupy seats a US retail spot account
cannot: market makers earning the spread (our 16 bps × 2 friction is
their revenue), the exchanges, delta-neutral funding/basis desks
(derivatives-gated), short-sellers in bears, issuers/insiders, and
full-cycle holders who survive −75–90% drawdowns. The class we started
in — directional intraday technical trading on public signals — is the
class that *funds* the first two, and our §3.1 study measured that
transfer directly (−54.6% vs −44.5% B&H). The only edge this seat
offers is risk-managed cyclical beta, whose correct bear-year output is
≈0%. That is what it produced. (Paper §5.)

---

**Q: Does anyone in the US actually need crypto exposure?**

Need — no one. The investable case is a small (1–5%) satellite of
cyclical growth optionality, which is exactly how TradFi has
operationalized it: spot ETFs deliver demanded beta, allocation models
size it small, and no major asset manager claims harvestable retail
alpha. If held at all, hold it risk-managed — the difference between
−48% and ≈0% this year — with core capital in broad equity index funds.

---

**Q: Can the idle cash at least earn yield on-exchange?**

Verified against the live account (2026-07-13): Kraken Earn offers this
US account USDC at 1.75% APR *and it is not allocatable*, no USD
strategy exists, BTC pays 0.02%, and only SOL pays meaningfully (2.89%
instant / 5.78% bonded — which requires holding SOL through the winter
the strategy is designed to sit out). With T-bills near 4–5%, the yield
floor must live **off-exchange**: keep only the trading float on
Kraken; the strategic reserve earns elsewhere. No code can close this
gap; it is an operational allocation decision.

---

**Q: Is there a max-drawdown setting that would have made the system
profitable?**

No. On the shipped configuration the breaker is dormant (max DD 1.05%
over 2y — every threshold from 5% to 1000% produced identical results).
On the unmanaged engine the threshold is a ~1:1 loss floor (cb10 →
−9.8%, cb20 → −20.0%): it decides how much you lose, never whether you
win. Profitability lives in the entry/exit discipline, not the stop.
(Paper §3.5.)
