"""Tier 1 — secondary features.

Registered but NOT in the default posterior (config enables tier 1 only
after the Tier 0 gate passes; see config features.enabled_tiers).
"""

from __future__ import annotations

import math
from typing import Optional

from .registry import FeatureContext, register


@register(
    name="size_skew", tier=1,
    inputs="mean buy trade size / mean sell trade size (forming candle)",
    lookback=0,
    hypothesis=("Large-trader footprint: reversals show larger average "
                "aggressive-buy prints than sells; fakes are retail-sized "
                "buys into large sells."),
)
def size_skew(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    if f.buy_count == 0 or f.sell_count == 0:
        return None
    mean_buy = f.buy_size_sum / f.buy_count
    mean_sell = f.sell_size_sum / f.sell_count
    if mean_buy <= 0 or mean_sell <= 0:
        return None
    return math.log(mean_buy / mean_sell)


@register(
    name="aggressor_run", tier=1,
    inputs="longest same-side aggressor streak in forming candle, signed",
    lookback=0,
    hypothesis=("Reversals print long consecutive buy-aggressor runs "
                "(program buying); fakes alternate sides."),
)
def aggressor_run(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    if f.trade_count == 0:
        return None
    if f.max_buy_streak >= f.max_sell_streak:
        return float(f.max_buy_streak)
    return -float(f.max_sell_streak)


@register(
    name="vwap_dev", tier=1,
    inputs="(close - forming-candle VWAP) / ATR",
    lookback=15,
    hypothesis=("Closing above own VWAP means selling was absorbed; fakes "
                "close below VWAP as sellers keep control."),
)
def vwap_dev(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    if f.trade_count == 0 or ctx.atr is None or ctx.atr <= 0:
        return None
    return (f.close - f.vwap) / ctx.atr


@register(
    name="wick_absorption", tier=1,
    inputs="lower-wick fraction x volume filled in bottom third of range",
    lookback=0,
    hypothesis=("Stop-run + reclaim: reversals print a long lower wick "
                "with heavy volume transacted in the bottom third "
                "(undercut-and-reclaim); fakes bounce without absorption."),
)
def wick_absorption(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    if f.trade_count == 0:
        return None
    rng = f.high - f.low
    if rng <= 0:
        return 0.0
    lower_wick = (min(f.open, f.close) - f.low) / rng
    return lower_wick * f.vol_bottom_third


@register(
    name="flow_persistence", tier=1,
    inputs="lag-1 autocorrelation of signed flow, last 6 closed candles",
    lookback=6,
    hypothesis=("Reversal flow is persistent (positive autocorrelation of "
                "signed flow); fake flow mean-reverts tick to tick."),
)
def flow_persistence(ctx: FeatureContext) -> Optional[float]:
    if len(ctx.closed) < 6:
        return None
    flows = [c.signed_flow for c in ctx.closed[-6:]]
    mean = sum(flows) / len(flows)
    dev = [x - mean for x in flows]
    denom = sum(d * d for d in dev)
    if denom <= 0:
        return None
    num = sum(dev[i] * dev[i + 1] for i in range(len(dev) - 1))
    return num / denom
