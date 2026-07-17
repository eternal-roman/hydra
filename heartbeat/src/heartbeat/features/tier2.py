"""Tier 2 — experimental features. Interfaces + stubs.

Per spec, each tier-2 feature needs STANDALONE evidence before joining
the posterior, so none is enabled by default and several are deliberate
stubs (they return None — zero evidence — until their data source is
wired and evidenced):

  * book_imbalance / cancel_asymmetry need the L2 book channel (not
    subscribed by the default feed — heavier, optional).
  * btc_lead needs a second running posterior (exogenous input plumbing
    exists via FeatureContext.config["exogenous"]).
  * funding_basis needs perp data — interface stubbed only, per spec.
  * bocd_runlength is NOT a z-feature: BOCD modulates lambda. The hook
    (`PosteriorEngine.lambda_modulator`) exists; the Adams-MacKay
    implementation is future work gated on tier-2 evidence.
"""

from __future__ import annotations

from typing import Optional

from .registry import FeatureContext, register


@register(
    name="book_imbalance", tier=2,
    inputs="L2 top-10-level bid/ask depth imbalance (book channel required)",
    lookback=0,
    hypothesis=("Reversals build resting bid depth under price while "
                "takers buy; fakes lift into a thinning book."),
)
def book_imbalance(ctx: FeatureContext) -> Optional[float]:
    book = ctx.config.get("exogenous", {}).get("book_imbalance")
    return float(book) if book is not None else None


@register(
    name="cancel_asymmetry", tier=2,
    inputs="bid vs ask cancel rate (book churn; spoof/absorption proxy)",
    lookback=0,
    hypothesis=("Fakes show ask-side cancels evaporating above price "
                "(spoof pull); reversals show bid-side cancels replaced "
                "lower (absorption re-quote)."),
)
def cancel_asymmetry(ctx: FeatureContext) -> Optional[float]:
    val = ctx.config.get("exogenous", {}).get("cancel_asymmetry")
    return float(val) if val is not None else None


@register(
    name="btc_lead", tier=2,
    inputs="BTC posterior L as exogenous input to alt posteriors",
    lookback=0,
    hypothesis=("BTC flow leads alt reversals by 1-3 candles; an alt "
                "bounce against a falling BTC posterior is a fake."),
)
def btc_lead(ctx: FeatureContext) -> Optional[float]:
    val = ctx.config.get("exogenous", {}).get("btc_log_odds")
    return float(val) if val is not None else None


@register(
    name="funding_basis", tier=2,
    inputs="perp funding / basis drift (perp feed NOT wired; stub only)",
    lookback=0,
    hypothesis=("Reversals begin with shorts paying rising funding into "
                "spot absorption; fakes carry flat funding."),
)
def funding_basis(ctx: FeatureContext) -> Optional[float]:
    val = ctx.config.get("exogenous", {}).get("funding_basis")
    return float(val) if val is not None else None
