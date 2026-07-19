"""Frozen S3 feature computation — verbatim behavioral port of
heartbeat/tools/bakeoff_s3_daily_classifier.py:87-155 (provenance; do
not drift without regenerating the golden parity fixtures).

Six features, all computed at the bounce-confirm bar (causal — nothing
after the entry-decision bar):

  clv            ((close-low)-(high-close))/(high-low) of the bounce bar
  range_atr      (high-low)/ATR of the bounce bar (ATR = the setup's
                 robust ATR14)
  vol_z          bounce-bar volume z-score vs the previous 20 bars
  shock_recency  bars since last |daily return| > 2*stdev(prev 20
                 returns), evaluated at the bounce bar, capped at 10
  breadth        how many universe assets made a fresh 20d low within
                 the last 3 daily bars at the SETUP date
  retest         1 if a prior low within the previous 30 bars came
                 within 0.25*ATR of the setup low
"""

from __future__ import annotations

import statistics
from typing import Sequence

from .candles import DailyBar
from .setups import HORIZON, TARGET_ATR, Setup

FEATURES = ("clv", "range_atr", "vol_z", "shock_recency", "breadth", "retest")


def shock_flags(bars: Sequence[DailyBar]) -> list[bool]:
    """Per-bar flag: |daily return| > 2*stdev of the previous 20 returns.
    Causal: the sigma window ends at the prior bar."""
    n = len(bars)
    rets = [0.0] * n
    for i in range(1, n):
        prev = bars[i - 1].close
        rets[i] = bars[i].close / prev - 1.0 if prev else 0.0
    flags = [False] * n
    for i in range(21, n):
        window = rets[i - 20:i]
        sd = statistics.pstdev(window)
        flags[i] = sd > 0 and abs(rets[i]) > 2.0 * sd
    return flags


def recency_at(flags: Sequence[bool], idx: int, cap: int = 10) -> int:
    """Bars since the last shock at-or-before idx, capped (cap = none)."""
    for back in range(cap):
        j = idx - back
        if j < 0:
            break
        if flags[j]:
            return back
    return cap


def fresh_low_days(bars: Sequence[DailyBar]) -> set[int]:
    """UTC day buckets where the bar's low undercut the prior 20 bars'."""
    out = set()
    lows = [b.low for b in bars]
    for i in range(20, len(bars)):
        if lows[i] < min(lows[i - 20:i]):
            out.add(int(bars[i].open_ts) // 86400)
    return out


def compute_features(bars: Sequence[DailyBar], setups: Sequence[Setup],
                     low_days_by_asset: dict[str, set[int]]) -> None:
    """Attach x (feature dict) and resolve_ts to each setup in place."""
    flags = shock_flags(bars)
    vols = [b.volume for b in bars]
    for s in setups:
        b, i = s.bounce_idx, s.low_idx
        c = bars[b]
        rng = c.high - c.low
        clv = ((c.close - c.low) - (c.high - c.close)) / rng if rng > 0 else 0.0
        range_atr = rng / s.atr
        if b >= 20:
            w = vols[b - 20:b]
            mu, sd = statistics.mean(w), statistics.pstdev(w)
            vol_z = (vols[b] - mu) / sd if sd > 0 else 0.0
        else:
            vol_z = 0.0
        day = int(bars[i].open_ts) // 86400
        breadth = sum(1 for days in low_days_by_asset.values()
                      if any(d in days for d in (day - 2, day - 1, day)))
        retest = int(any(abs(bars[j].low - s.low_px) <= 0.25 * s.atr
                         for j in range(max(0, i - 30), i)))
        s.x = {"clv": clv, "range_atr": range_atr, "vol_z": vol_z,
               "shock_recency": float(recency_at(flags, b)),
               "breadth": float(breadth), "retest": float(retest)}
        s.resolve_ts = None
        tgt = s.low_px + TARGET_ATR * s.atr
        for j in range(b, min(len(bars), i + 1 + HORIZON)):
            if bars[j].low < s.low_px or bars[j].high >= tgt:
                s.resolve_ts = bars[j].close_ts
                break
