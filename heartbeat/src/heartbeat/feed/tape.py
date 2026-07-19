"""Normalized trade records, gap detection, and taint bookkeeping.

The tape is the single source of truth for the math path. Every trade
carries the EXCHANGE timestamp (never local receipt time); local receipt
time is kept only for clock-skew monitoring and never enters the math.

Fail-loud contract: sequence violations (non-monotonic exchange
timestamps beyond Kraken's same-ms reordering), feed gaps, and clock skew
above the configured threshold raise/record `TapeAlert`s and mark the
affected time ranges tainted. Nothing is silently interpolated.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator, Optional


class Side(str, Enum):
    BUY = "buy"    # aggressor bought (taker lifted the ask)
    SELL = "sell"  # aggressor sold (taker hit the bid)


@dataclass(frozen=True, slots=True)
class Trade:
    """One normalized trade.

    ts        exchange timestamp, epoch seconds (float, Kraken gives RFC3339
              in WS v2 and float seconds in REST; both normalized here).
    price     trade price.
    qty       base-asset quantity.
    side      aggressor side.
    ord_type  "market" | "limit" (as reported by Kraken).
    trade_id  Kraken monotonically increasing trade id when available
              (REST provides `last` cursor; WS v2 provides trade_id).
              0 when unknown. Used only for stable sort + dedup, never math.
    """

    ts: float
    price: float
    qty: float
    side: Side
    ord_type: str = "limit"
    trade_id: int = 0

    def sort_key(self) -> tuple[float, int]:
        return (self.ts, self.trade_id)

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side is Side.BUY else -self.qty


class AlertKind(str, Enum):
    GAP = "gap"                    # feed discontinuity not fully backfilled
    SEQUENCE = "sequence"          # exchange timestamps went backwards
    CLOCK_SKEW = "clock_skew"      # |local - exchange| > threshold
    BACKFILL_PARTIAL = "backfill_partial"


@dataclass(frozen=True, slots=True)
class TapeAlert:
    kind: AlertKind
    ts_start: float
    ts_end: float
    detail: str


class TaintRegistry:
    """Tracks tainted [start, end] exchange-time ranges.

    Candles overlapping any tainted range must be flagged TAINTED in all
    output. Ranges are kept sorted and merged.
    """

    def __init__(self) -> None:
        self._ranges: list[tuple[float, float]] = []

    def add(self, ts_start: float, ts_end: float) -> None:
        if ts_end < ts_start:
            raise ValueError(f"tainted range end < start: {ts_start}..{ts_end}")
        i = bisect.bisect_left(self._ranges, (ts_start, ts_end))
        self._ranges.insert(i, (ts_start, ts_end))
        self._merge()

    def _merge(self) -> None:
        merged: list[tuple[float, float]] = []
        for s, e in self._ranges:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self._ranges = merged

    def overlaps(self, ts_start: float, ts_end: float) -> bool:
        i = bisect.bisect_left(self._ranges, (ts_start, float("-inf")))
        for s, e in self._ranges[max(0, i - 1):]:
            if s > ts_end:
                break
            if e >= ts_start and s <= ts_end:
                return True
        return False

    def ranges(self) -> list[tuple[float, float]]:
        return list(self._ranges)


class TapeMonitor:
    """Streaming integrity monitor for a live or replayed tape.

    Feed every trade through `observe`. It:
      * rejects out-of-order exchange timestamps (tolerating equal ts,
        which Kraken emits for trades matched in the same batch) by
        raising a SEQUENCE alert and tainting the inversion window;
      * checks clock skew when local receipt time is supplied;
      * exposes `mark_gap` for the WS client to report reconnect gaps.
    Alerts accumulate in `.alerts`; taint ranges in `.taint`.
    """

    # Kraken occasionally reorders trades within the same millisecond;
    # anything beyond this backwards jump is a real sequence violation.
    SEQUENCE_TOLERANCE_S = 0.0

    def __init__(self, clock_skew_alert_s: float = 2.0) -> None:
        self.clock_skew_alert_s = clock_skew_alert_s
        self.taint = TaintRegistry()
        self.alerts: list[TapeAlert] = []
        self.last_ts: Optional[float] = None
        self.last_trade_id: int = 0
        self.gap_count: int = 0
        self.max_skew_s: float = 0.0

    def observe(self, trade: Trade, local_ts: Optional[float] = None) -> None:
        if self.last_ts is not None and trade.ts < self.last_ts - self.SEQUENCE_TOLERANCE_S:
            alert = TapeAlert(
                AlertKind.SEQUENCE, trade.ts, self.last_ts,
                f"exchange ts went backwards: {self.last_ts} -> {trade.ts}",
            )
            self.alerts.append(alert)
            self.taint.add(trade.ts, self.last_ts)
        self.last_ts = max(trade.ts, self.last_ts or trade.ts)
        if trade.trade_id:
            self.last_trade_id = max(self.last_trade_id, trade.trade_id)
        if local_ts is not None:
            skew = abs(local_ts - trade.ts)
            self.max_skew_s = max(self.max_skew_s, skew)
            if skew > self.clock_skew_alert_s:
                self.alerts.append(TapeAlert(
                    AlertKind.CLOCK_SKEW, trade.ts, trade.ts,
                    f"clock skew {skew:.3f}s > {self.clock_skew_alert_s}s",
                ))
                self.taint.add(trade.ts, trade.ts)

    def mark_gap(self, ts_start: float, ts_end: float, detail: str,
                 backfilled: bool) -> None:
        """Record a feed gap. Fully backfilled gaps count but do not taint."""
        self.gap_count += 1
        if not backfilled:
            self.taint.add(ts_start, ts_end)
            self.alerts.append(TapeAlert(AlertKind.GAP, ts_start, ts_end, detail))
        else:
            self.alerts.append(TapeAlert(
                AlertKind.BACKFILL_PARTIAL, ts_start, ts_end,
                f"gap fully backfilled: {detail}",
            ))


def normalize_trades(trades: Iterable[Trade]) -> Iterator[Trade]:
    """Stable-sort by (ts, trade_id) and drop exact duplicates.

    Used when merging REST backfill with live WS trades around a gap.
    """
    seen: set[tuple[float, int, float, float, str]] = set()
    for t in sorted(trades, key=Trade.sort_key):
        key = (t.ts, t.trade_id, t.price, t.qty, t.side.value)
        if key in seen:
            continue
        seen.add(key)
        yield t
