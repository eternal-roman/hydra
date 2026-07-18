"""Tier 0 — core features (the minimum viable heartbeat).

Each returns the RAW value; robust scaling to ~[-1, 1] happens in the
posterior engine. Hand-computed fixtures for every function live in
tests/test_features_tier0.py.
"""

from __future__ import annotations

import statistics
from typing import Optional, Sequence

from ..engine.candle import ClosedCandle
from .registry import FeatureContext, register


def robust_atr(closed: Sequence[ClosedCandle], period: int = 14,
               outlier_mult: float = 3.0) -> Optional[float]:
    """Median-based ATR over the last `period` closed candles.

    True range uses the standard prev-close definition. Ranges greater
    than `outlier_mult` x the median TR of the window are dropped before
    taking the median (crash spikes must not inflate the yardstick).
    Returns None with fewer than `period`+1 candles or an all-zero window.
    """
    if len(closed) < period + 1:
        return None
    window = closed[-(period + 1):]
    trs = []
    for prev, cur in zip(window[:-1], window[1:]):
        tr = max(cur.high - cur.low,
                 abs(cur.high - prev.close),
                 abs(cur.low - prev.close))
        trs.append(tr)
    med = statistics.median(trs)
    if med <= 0:
        nonzero = [t for t in trs if t > 0]
        if not nonzero:
            return None
        med = statistics.median(nonzero)
    kept = [t for t in trs if t <= outlier_mult * med]
    if not kept:
        return None
    atr = statistics.median(kept)
    return atr if atr > 0 else None


@register(
    name="ofi", tier=0,
    inputs="forming candle buy_vol/sell_vol (aggressor-side)",
    lookback=0,
    hypothesis=("Reversals show positive OFI persisting >=3 candles; "
                "fakes decay by candle 2."),
)
def ofi(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    tot = f.buy_vol + f.sell_vol
    if tot <= 0:
        return None
    return (f.buy_vol - f.sell_vol) / tot


@register(
    name="clv", tier=0,
    inputs="forming candle O/H/L/C geometry",
    lookback=0,
    hypothesis=("Reversal candles close upper-third on rising volume; "
                "fake pops close mid-range."),
)
def clv(ctx: FeatureContext) -> Optional[float]:
    f = ctx.forming
    if f.trade_count == 0:
        return None
    rng = f.high - f.low
    if rng <= 0:
        return 0.0
    return ((f.close - f.low) - (f.high - f.close)) / rng


@register(
    name="range_atr", tier=0,
    inputs="forming candle range / robust ATR(14, median, 3x-outlier-drop)",
    lookback=15,
    hypothesis=("Reversal thrusts expand range; fakes bounce on "
                "contracting range."),
)
def range_atr(ctx: FeatureContext) -> Optional[float]:
    if ctx.atr is None or ctx.atr <= 0 or ctx.forming.trade_count == 0:
        return None
    return ctx.forming.range / ctx.atr


# Per-candle memo for vol_z: (mean, sd) depend only on (closed, window),
# and the pipeline reuses ONE closed-tuple object for every heartbeat of a
# candle, so identity equality is exact (tuples are immutable — same object
# implies same stats). Keyed by identity, holding a strong ref so the id
# can never be recycled while cached. Single entry: replays are sequential.
_VOLZ_MEMO: dict = {"closed": None, "window": None, "stats": None}


@register(
    name="vol_z", tier=0,
    inputs="forming candle volume vs trailing 96 closed candles",
    lookback=96,
    hypothesis="Reversals carry volume; fakes are quiet.",
)
def vol_z(ctx: FeatureContext) -> Optional[float]:
    window = int(ctx.config.get("vol_z", {}).get("window", 96))
    closed = ctx.closed
    if len(closed) < window or ctx.forming.trade_count == 0:
        return None
    if _VOLZ_MEMO["closed"] is closed and _VOLZ_MEMO["window"] == window:
        mean, sd = _VOLZ_MEMO["stats"]
    else:
        vols = [c.volume for c in closed[-window:]]
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        sd = var ** 0.5
        _VOLZ_MEMO.update(closed=closed, window=window, stats=(mean, sd))
    if sd <= 0:
        return None
    # Pro-rate the forming candle's volume to a full-candle equivalent so a
    # half-formed candle isn't structurally "quiet". Floor at 25% elapsed to
    # avoid explosive extrapolation off the first trades.
    prog = max(ctx.forming.progress, 0.25)
    projected = ctx.forming.volume / prog
    return (projected - mean) / sd


@register(
    name="ofi_momentum", tier=0,
    inputs="OFI of [closed t-2, closed t-1, forming t] — LSQ slope",
    lookback=2,
    hypothesis=("Sign and slope of flow, not level, separates fake from "
                "reversal."),
)
def ofi_momentum(ctx: FeatureContext) -> Optional[float]:
    if len(ctx.closed) < 2:
        return None
    cur = ofi(ctx)
    if cur is None:
        return None
    series = [ctx.closed[-2].ofi, ctx.closed[-1].ofi, cur]
    # least-squares slope for x = 0,1,2 reduces to (y2 - y0) / 2
    return (series[2] - series[0]) / 2.0
