"""Exit policies for S3 positions — the gate-adopted basis and the
shadow-tracked arms. Semantics mirror the exit-policy bakeoff runner
(heartbeat/tools/bakeoff_s3_exit_policy.py, unified simulator): priority
within a bar is stop, then target, then horizon/K; close-decided exits
fill at the deciding bar's close.

  x0_registered   touch-stop L0 (fill min(close, L0)) -> target 3.3*ATR
                  -> 200-bar horizon from low_idx      [promotion baseline]
  x1_close_stop   stop on close < L0 (fill that close) -> target -> horizon
                  [ADOPTED by the pre-registered exit gate, 2026-07-19]
  hold_k60_stop   stop on close < L0 -> exit at close of entry+60 bars
                  [ETH shadow arm ONLY - lottery-profile disclosure in
                   the hold-horizon study; never a live basis]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .candles import DailyBar
from .setups import HORIZON, TARGET_ATR

POLICIES = ("x0_registered", "x1_close_stop", "hold_k60_stop")
HOLD_K = 60


@dataclass
class OpenPosition:
    asset: str
    arm: str                  # one of POLICIES
    entry_ts: float           # open_ts of the entry (b1) bar
    entry_px: float           # b1 close
    low_px: float             # L0
    atr: float
    low_idx: int              # indices in the bar series at entry time
    entry_idx: int


@dataclass(frozen=True)
class ExitDecision:
    price: float
    reason: str               # stop | stop_close | target | time


def evaluate(policy: str, pos: OpenPosition, bar: DailyBar,
             bar_idx: int) -> Optional[ExitDecision]:
    """Evaluate one completed bar (bar_idx = its index in the same series
    pos indices refer to). Returns None while the position stays open."""
    if policy not in POLICIES:
        raise ValueError(f"unknown exit policy {policy!r}")
    tgt = pos.low_px + TARGET_ATR * pos.atr
    if policy == "x0_registered":
        if bar.low < pos.low_px:
            return ExitDecision(min(bar.close, pos.low_px), "stop")
        if bar.high >= tgt:
            return ExitDecision(tgt, "target")
        if bar_idx - pos.low_idx > HORIZON:
            return ExitDecision(bar.close, "time")
        return None
    if policy == "x1_close_stop":
        if bar.close < pos.low_px:
            return ExitDecision(bar.close, "stop_close")
        if bar.high >= tgt:
            return ExitDecision(tgt, "target")
        if bar_idx - pos.low_idx > HORIZON:
            return ExitDecision(bar.close, "time")
        return None
    # hold_k60_stop
    if bar.close < pos.low_px:
        return ExitDecision(bar.close, "stop_close")
    if bar_idx - pos.entry_idx >= HOLD_K:
        return ExitDecision(bar.close, "time")
    return None
