"""Daily bar construction for the S3 bounce algorithm.

UTC-day OHLCV bars from two sources: bulk daily seeds (e.g. Kraken 1440m
OHLC at boot, or a research sqlite export) and incremental 1h candle
closes. A day built from 1h data is authoritative: seeds only fill days
the 1h feed has not touched. The still-forming UTC day is never exposed
— every consumer sees completed bars only, matching the research
convention (features at the bounce-confirm bar's CLOSE).
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_S = 86400


@dataclass(frozen=True)
class DailyBar:
    open_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def close_ts(self) -> float:
        return self.open_ts + DAY_S

    @property
    def day(self) -> int:
        return int(self.open_ts) // DAY_S


class _DayAgg:
    __slots__ = ("open", "high", "low", "close", "volume",
                 "first_1h", "last_1h")

    def __init__(self, o, h, low, c, v, first_1h=None, last_1h=None):
        self.open, self.high, self.low, self.close = o, h, low, c
        self.volume = v
        self.first_1h = first_1h      # None => seed-built day
        self.last_1h = last_1h


class DailyBarSeries:
    def __init__(self) -> None:
        self._days: dict[int, _DayAgg] = {}

    def update_1h(self, ts: float, o: float, h: float, low: float,
                  c: float, v: float) -> None:
        """Fold one completed 1h candle (ts = its OPEN, seconds) into its
        UTC day. Handles out-of-order delivery: open/close follow the
        earliest/latest 1h timestamp seen, extremes and volume always
        accumulate."""
        day = int(ts) // DAY_S
        cur = self._days.get(day)
        if cur is None or cur.first_1h is None:   # new day or seed-built
            self._days[day] = _DayAgg(o, h, low, c, v,
                                      first_1h=float(ts), last_1h=float(ts))
            return
        cur.high = max(cur.high, h)
        cur.low = min(cur.low, low)
        cur.volume += v
        if ts < cur.first_1h:
            cur.open, cur.first_1h = o, float(ts)
        if ts >= cur.last_1h:
            cur.close, cur.last_1h = c, float(ts)

    def seed(self, rows: list[dict]) -> None:
        """Bulk daily rows ({ts,open,high,low,close,volume}, ts = day open
        seconds). Never overwrites a day already fed by 1h data; re-seeding
        the same rows is idempotent (seed days are simply rewritten)."""
        for r in rows:
            day = int(r["ts"]) // DAY_S
            cur = self._days.get(day)
            if cur is not None and cur.first_1h is not None:
                continue
            self._days[day] = _DayAgg(float(r["open"]), float(r["high"]),
                                      float(r["low"]), float(r["close"]),
                                      float(r["volume"]))

    def completed_bars(self, now_ts: float) -> list[DailyBar]:
        """All bars whose UTC day has fully elapsed at now_ts, ascending.
        A day D is complete iff (D+1)*86400 <= now_ts."""
        last_complete = int((now_ts - DAY_S) // DAY_S)
        return [DailyBar(open_ts=float(day * DAY_S), open=a.open, high=a.high,
                         low=a.low, close=a.close, volume=a.volume)
                for day, a in sorted(self._days.items())
                if day <= last_complete]
