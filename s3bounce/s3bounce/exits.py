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
  x4a_trail_ma9   stop on close < L0 while UNARMED; ARM when close >=
                  L0+3.3*ATR (the old target is an arming line, no exit);
                  armed: exit at close < MA9 (caller supplies ma9) ->
                  horizon [SHADOW ARM ONLY - trail gate 2026-07-19 passed
                  C1-C4 but failed C5 LOYO stability; never a live basis]
  x5_vigor_routed premium_atr = (entry-L0)/ATR > premium_cut -> x4a rule,
                  else x1 rule; routed once at entry, deterministic
                  [SHADOW ARM ONLY - failed C3 vs blind time control]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .candles import DailyBar
from .setups import HORIZON, TARGET_ATR

POLICIES = ("x0_registered", "x1_close_stop", "hold_k60_stop",
            "x4a_trail_ma9", "x5_vigor_routed")
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
    armed: bool = False       # x4a trail: past the arming line (mutated
    #                           by evaluate; caller persists it)
    premium_cut: Optional[float] = None   # x5 routing threshold (artifact)

    @property
    def premium_atr(self) -> float:
        return (self.entry_px - self.low_px) / self.atr


@dataclass(frozen=True)
class ExitDecision:
    price: float
    reason: str               # stop | stop_close | target | trail | time


def evaluate(policy: str, pos: OpenPosition, bar: DailyBar,
             bar_idx: int, ma9: Optional[float] = None) -> Optional[ExitDecision]:
    """Evaluate one completed bar (bar_idx = its index in the same series
    pos indices refer to). Returns None while the position stays open.
    `ma9` = 9-bar simple MA of daily closes INCLUDING `bar` (the trail
    arms need it; the caller owns the bar series). x4a mutates pos.armed;
    persistence of that flag across restarts is the caller's job."""
    if policy not in POLICIES:
        raise ValueError(f"unknown exit policy {policy!r}")
    if policy == "x5_vigor_routed":
        # Routed once at entry from immutable fields — deterministic per
        # position. No cut configured -> conservative x1 behaviour.
        strong = (pos.premium_cut is not None
                  and pos.premium_atr > pos.premium_cut)
        return evaluate("x4a_trail_ma9" if strong else "x1_close_stop",
                        pos, bar, bar_idx, ma9)
    tgt = pos.low_px + TARGET_ATR * pos.atr
    if policy == "x4a_trail_ma9":
        # Priority mirrors tools/bakeoff_s3_trail_exit.py trail_exit():
        # arm first, then unarmed-stop, then trail, then horizon.
        if not pos.armed and bar.close >= tgt:
            pos.armed = True
        if not pos.armed and bar.close < pos.low_px:
            return ExitDecision(bar.close, "stop_close")
        if pos.armed and ma9 is not None and bar.close < ma9:
            return ExitDecision(bar.close, "trail")
        if bar_idx - pos.low_idx > HORIZON:
            return ExitDecision(bar.close, "time")
        return None
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
