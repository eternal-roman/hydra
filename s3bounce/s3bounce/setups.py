"""Bounce-setup geometry — verbatim behavioral port from the HYDRA
heartbeat research pipeline (provenance, do not drift without
regenerating the golden parity fixtures):

  * ma, swing_lows        <- heartbeat/src/heartbeat/eval/labeler.py:51-65
  * robust_atr            <- heartbeat/src/heartbeat/features/tier0.py:17-47
  * causal_setups,
    entry_index           <- heartbeat/tools/paper_bounce_sim.py:64-129

Everything a live trader sees is causal: swing low L0 confirmed SW bars
later, an established down-leg (>=2 lower swing lows below MA9,
past-only), crash exclusion, then the bounce trigger high >= L0 +
BOUNCE_ATR*ATR. The oracle `label` uses FUTURE bars (labeler resolution
semantics) and exists for research/refit parity only — no causal
decision may read it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

from .candles import DailyBar

SW = 2
MA_P = 9
LOOKBACK = 30
BOUNCE_ATR = 1.0
TARGET_ATR = 3.3
CRASH_ATR = 3.0
HORIZON = 200


@dataclass
class Setup:
    low_idx: int
    low_px: float
    atr: float
    bounce_idx: int
    label: Optional[str]          # "reversal" | "fake" | None — RESEARCH ONLY
    x: Optional[dict] = None      # features, attached by compute_features
    resolve_ts: Optional[float] = None

    @property
    def setup_id(self) -> str:
        return f"{int(self.low_idx)}@{self.low_px:.10g}"


def ma(vals: Sequence[float], period: int, idx: int) -> Optional[float]:
    if idx + 1 < period:
        return None
    window = vals[idx + 1 - period: idx + 1]
    return sum(window) / period


def swing_lows(bars: Sequence[DailyBar], w: int) -> list[int]:
    out = []
    for i in range(w, len(bars) - w):
        lo = bars[i].low
        if all(lo <= bars[j].low for j in range(i - w, i + w + 1)) and \
           any(lo < bars[j].low for j in range(i - w, i + w + 1) if j != i):
            out.append(i)
    return out


def robust_atr(bars: Sequence[DailyBar], period: int = 14,
               outlier_mult: float = 3.0) -> Optional[float]:
    """Median-based ATR over the last `period` closed bars; true ranges
    above outlier_mult x the window median are dropped first."""
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    trs = []
    for prev, cur in zip(window[:-1], window[1:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    med = statistics.median(trs)
    if med <= 0:
        nonzero = [t for t in trs if t > 0]
        if not nonzero:
            return None
        med = statistics.median(nonzero)
    kept = [t for t in trs if t <= outlier_mult * med]
    if not kept:
        return None
    return statistics.median(kept)


def causal_setups(bars: Sequence[DailyBar]) -> list[Setup]:
    closes = [b.close for b in bars]
    swings = swing_lows(bars, SW)
    out: list[Setup] = []
    for i in swings:
        atr = robust_atr(bars[:i], 14, 3.0)
        if atr is None or atr <= 0:
            continue
        prior = [j for j in swings if i - LOOKBACK <= j < i]
        idx_seq = prior + [i]
        lower = 0
        for a, b in zip(idx_seq[:-1], idx_seq[1:]):
            ma_a = ma(closes, MA_P, a)
            if ma_a is None:
                continue
            if bars[b].low < bars[a].low and bars[a].low < ma_a:
                lower += 1
        ma_i = ma(closes, MA_P, i)
        if lower < 2 or ma_i is None or bars[i].close >= ma_i:
            continue
        if any(c.range > CRASH_ATR * atr for c in bars[max(0, i - 3): i + 1]):
            continue
        low_px = bars[i].low
        bounce_idx = None
        for j in range(i + 1, min(len(bars), i + 1 + HORIZON)):
            if bars[j].low < low_px:
                break
            if bars[j].high >= low_px + BOUNCE_ATR * atr:
                bounce_idx = j
                break
        if bounce_idx is None:
            continue
        label = None
        tgt = low_px + TARGET_ATR * atr
        for j in range(bounce_idx, min(len(bars), i + 1 + HORIZON)):
            if bars[j].low < low_px:
                label = "fake"
                break
            if bars[j].high >= tgt:
                label = "reversal"
                break
        out.append(Setup(low_idx=i, low_px=low_px, atr=atr,
                         bounce_idx=bounce_idx, label=label))
    return out


def entry_index(bars: Sequence[DailyBar], s: Setup,
                offset: int = 1) -> Optional[int]:
    """Entry checkpoint bounce+offset, or None if the setup is already
    RESOLVED by then (low undercut, or target reached)."""
    e = s.bounce_idx + offset
    if e >= len(bars):
        return None
    tgt = s.low_px + TARGET_ATR * s.atr
    for k in range(s.bounce_idx, e + 1):
        if bars[k].low < s.low_px or bars[k].high >= tgt:
            return None
    return e
