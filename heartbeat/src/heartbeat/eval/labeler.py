"""Bounce-event extraction and fake/reversal labeling.

Definitions (all thresholds config-driven, defaults per spec):

  * robust ATR: median-based ATR(14) with >3x-median outliers dropped
    (same function the features use), computed at the candle BEFORE the
    low so the yardstick itself is not contaminated by the event.
  * down-leg: within `down_leg_lookback` candles before the low there are
    >= 2 successive lower swing lows, each printed below the 9MA of closes.
  * bounce event: a swing low L0 in a down-leg, followed by price reaching
    L0 + 1.0*ATR (high of some later candle) BEFORE any candle prints a
    low below L0.
  * label REVERSAL: price subsequently advances >= 3.3*ATR above L0
    without a new low below L0 first.
  * label FAKE: a low below L0 prints before the 3.3*ATR target.
  * excluded: crash regime (any candle in the 4 candles ending at the low
    with range > 3*ATR) and chop (down-leg requirement not met); events
    unresolved within `horizon_candles` are discarded as ambiguous.

Labels use FUTURE data by construction (that is what a label is); the
POSTERIOR values recorded at checkpoints are strictly past-only because
they come from the causal pipeline output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..features.tier0 import robust_atr
from ..engine.candle import ClosedCandle


@dataclass(frozen=True)
class BounceEvent:
    pair: str
    tf: str
    low_idx: int          # candle index of the event low
    low_ts: float
    low_price: float
    atr: float
    bounce_idx: int       # first candle whose high >= low + 1.0*ATR
    label: str            # "reversal" | "fake"
    resolve_idx: int      # candle where label resolved
    # posterior P(up) at candle CLOSES of bounce+1..+3, and at first close
    # after price progressed 2.0*ATR off the low. None = out of range.
    p_at: dict[str, Optional[float]]
    tainted: bool


def _ma(vals: Sequence[float], period: int, idx: int) -> Optional[float]:
    if idx + 1 < period:
        return None
    window = vals[idx + 1 - period: idx + 1]
    return sum(window) / period


def _swing_lows(candles: Sequence[ClosedCandle], w: int) -> list[int]:
    out = []
    for i in range(w, len(candles) - w):
        lo = candles[i].low
        if all(lo <= candles[j].low for j in range(i - w, i + w + 1)) and \
           any(lo < candles[j].low for j in range(i - w, i + w + 1) if j != i):
            out.append(i)
    return out


def extract_events(pair: str, tf: str,
                   candles: Sequence[ClosedCandle],
                   p_up: Sequence[float],
                   config: dict) -> list[BounceEvent]:
    """candles[i] and p_up[i] must be aligned (posterior AT that close)."""
    if len(candles) != len(p_up):
        raise ValueError(f"candles({len(candles)}) and p_up({len(p_up)}) misaligned")
    lcfg = config.get("labeler", {})
    ma_period = int(lcfg.get("ma_period", 9))
    sw = int(lcfg.get("swing_window", 2))
    lookback = int(lcfg.get("down_leg_lookback", 30))
    bounce_atr = float(lcfg.get("bounce_atr", 1.0))
    reversal_atr = float(lcfg.get("reversal_atr", 3.3))
    crash_mult = float(lcfg.get("crash_range_atr", 3.0))
    horizon = int(lcfg.get("horizon_candles", 200))
    acfg = config.get("atr", {})
    atr_period = int(acfg.get("period", 14))
    atr_outlier = float(acfg.get("outlier_mult", 3.0))

    closes = [c.close for c in candles]
    swings = _swing_lows(candles, sw)
    events: list[BounceEvent] = []
    claimed_until = -1  # avoid overlapping events sharing the same leg

    for i in swings:
        if i <= claimed_until:
            continue
        atr = robust_atr(candles[:i], atr_period, atr_outlier)
        if atr is None or atr <= 0:
            continue
        # -- down-leg requirement: >=2 successive lower swing lows below MA9
        prior = [j for j in swings if i - lookback <= j < i]
        idx_seq = prior + [i]
        lower_count = 0
        for a, b in zip(idx_seq[:-1], idx_seq[1:]):
            ma_a = _ma(closes, ma_period, a)
            if ma_a is None:
                continue
            if candles[b].low < candles[a].low and candles[a].low < ma_a:
                lower_count += 1
        ma_i = _ma(closes, ma_period, i)
        if lower_count < 2 or ma_i is None or candles[i].close >= ma_i:
            continue
        # -- crash-regime exclusion
        recent = candles[max(0, i - 3): i + 1]
        if any(c.range > crash_mult * atr for c in recent):
            continue
        # -- find bounce: +1.0 ATR before a lower low
        low_px = candles[i].low
        bounce_idx: Optional[int] = None
        for j in range(i + 1, min(len(candles), i + 1 + horizon)):
            if candles[j].low < low_px:
                break
            if candles[j].high >= low_px + bounce_atr * atr:
                bounce_idx = j
                break
        if bounce_idx is None:
            continue
        # -- resolve label: 3.3 ATR advance vs new low
        label: Optional[str] = None
        resolve_idx: Optional[int] = None
        for j in range(bounce_idx, min(len(candles), i + 1 + horizon)):
            if candles[j].low < low_px:
                label, resolve_idx = "fake", j
                break
            if candles[j].high >= low_px + reversal_atr * atr:
                label, resolve_idx = "reversal", j
                break
        if label is None:
            continue  # unresolved within horizon: ambiguous, discard
        # -- posterior checkpoints (candle closes; strictly causal series)
        p_at: dict[str, Optional[float]] = {}
        for k in (1, 2, 3):
            idx = bounce_idx + k
            p_at[f"bounce+{k}"] = p_up[idx] if idx < len(candles) else None
        prog_idx = next((j for j in range(bounce_idx, min(len(candles),
                                                          i + 1 + horizon))
                         if candles[j].high >= low_px + 2.0 * atr), None)
        p_at["progress_2atr"] = (p_up[prog_idx]
                                 if prog_idx is not None and prog_idx < len(p_up)
                                 else None)
        tainted = any(c.tainted for c in candles[i:min(len(candles),
                                                       bounce_idx + 4)])
        events.append(BounceEvent(
            pair=pair, tf=tf, low_idx=i, low_ts=candles[i].open_ts,
            low_price=low_px, atr=atr, bounce_idx=bounce_idx, label=label,
            resolve_idx=resolve_idx, p_at=p_at, tainted=tainted))
        claimed_until = resolve_idx
    return events
