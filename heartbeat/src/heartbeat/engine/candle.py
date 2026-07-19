"""Incremental candle builder for any timeframe.

The forming candle is computed strictly incrementally from trades as they
arrive (never by peeking at a completed candle) — this is what the
no-lookahead test exercises. All accumulators are pure functions of the
trade prefix, so an incremental run and a from-scratch replay of the same
tape are bit-identical.

Time base: exchange timestamps only. Candle boundaries are
ts // tf_s * tf_s (UTC-aligned epoch buckets, matching Kraken OHLC).
Gaps in trading produce explicit empty candles (O=H=L=C=prev close,
volume 0) so downstream indicators keep a regular time index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..feed.tape import Side, Trade

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}


def tf_seconds(tf: str) -> int:
    try:
        return TF_SECONDS[tf]
    except KeyError:
        raise ValueError(f"unsupported timeframe {tf!r}; one of {sorted(TF_SECONDS)}")


@dataclass(slots=True)
class FormingCandle:
    """Mutable forming-candle state, updated one trade at a time."""

    open_ts: float
    tf_s: int
    open: float = 0.0
    high: float = float("-inf")
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    trade_count: int = 0
    vwap_num: float = 0.0
    # tier-1 accumulators
    buy_size_sum: float = 0.0
    buy_count: int = 0
    sell_size_sum: float = 0.0
    sell_count: int = 0
    _streak_side: Optional[Side] = None
    _streak_len: int = 0
    max_buy_streak: int = 0
    max_sell_streak: int = 0
    # volume filled while price sat in the bottom third of the range AS OF
    # that trade (causal approximation of "volume in bottom third").
    vol_bottom_third: float = 0.0

    @property
    def close_ts(self) -> float:
        return self.open_ts + self.tf_s

    @property
    def vwap(self) -> float:
        return self.vwap_num / self.volume if self.volume > 0 else self.close

    @property
    def range(self) -> float:
        return (self.high - self.low) if self.trade_count else 0.0

    @property
    def progress(self) -> float:
        """Fraction of the candle elapsed at the LAST trade seen (0..1)."""
        if not self.trade_count:
            return 0.0
        return min(1.0, max(0.0, (self._last_ts - self.open_ts) / self.tf_s))

    _last_ts: float = 0.0

    def apply(self, t: Trade) -> None:
        if self.trade_count == 0:
            self.open = t.price
            self.high = t.price
            self.low = t.price
        self.high = max(self.high, t.price)
        self.low = min(self.low, t.price)
        self.close = t.price
        self.volume += t.qty
        self.vwap_num += t.price * t.qty
        self.trade_count += 1
        self._last_ts = t.ts
        if t.side is Side.BUY:
            self.buy_vol += t.qty
            self.buy_size_sum += t.qty
            self.buy_count += 1
        else:
            self.sell_vol += t.qty
            self.sell_size_sum += t.qty
            self.sell_count += 1
        # aggressor streaks
        if t.side is self._streak_side:
            self._streak_len += 1
        else:
            self._streak_side = t.side
            self._streak_len = 1
        if t.side is Side.BUY:
            self.max_buy_streak = max(self.max_buy_streak, self._streak_len)
        else:
            self.max_sell_streak = max(self.max_sell_streak, self._streak_len)
        # bottom-third volume, causal (uses the range as of this trade)
        rng = self.high - self.low
        if rng > 0 and t.price <= self.low + rng / 3.0:
            self.vol_bottom_third += t.qty


@dataclass(frozen=True, slots=True)
class ClosedCandle:
    open_ts: float
    close_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_vol: float
    sell_vol: float
    trade_count: int
    vwap: float
    buy_size_sum: float = 0.0
    buy_count: int = 0
    sell_size_sum: float = 0.0
    sell_count: int = 0
    max_buy_streak: int = 0
    max_sell_streak: int = 0
    vol_bottom_third: float = 0.0
    tainted: bool = False

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def signed_flow(self) -> float:
        return self.buy_vol - self.sell_vol

    @property
    def ofi(self) -> float:
        tot = self.buy_vol + self.sell_vol
        return (self.buy_vol - self.sell_vol) / tot if tot > 0 else 0.0


def _freeze(f: FormingCandle, prev_close: Optional[float]) -> ClosedCandle:
    if f.trade_count == 0:
        px = prev_close if prev_close is not None else 0.0
        return ClosedCandle(f.open_ts, f.close_ts, px, px, px, px,
                            0.0, 0.0, 0.0, 0, px)
    return ClosedCandle(
        f.open_ts, f.close_ts, f.open, f.high, f.low, f.close, f.volume,
        f.buy_vol, f.sell_vol, f.trade_count, f.vwap,
        f.buy_size_sum, f.buy_count, f.sell_size_sum, f.sell_count,
        f.max_buy_streak, f.max_sell_streak, f.vol_bottom_third)


class CandleBuilder:
    """Feeds trades in, emits closed candles + maintains the forming one.

    `on_trade` returns the list of candles CLOSED by this trade's arrival
    (possibly several empty ones if trading gapped across boundaries),
    strictly before the trade is applied to the new forming candle.
    """

    def __init__(self, tf: str) -> None:
        self.tf_s = tf_seconds(tf)
        self.forming: Optional[FormingCandle] = None
        self.prev_close: Optional[float] = None

    def bucket_open(self, ts: float) -> float:
        return (int(ts) // self.tf_s) * self.tf_s

    def on_trade(self, t: Trade) -> list[ClosedCandle]:
        closed: list[ClosedCandle] = []
        open_ts = self.bucket_open(t.ts)
        if self.forming is None:
            self.forming = FormingCandle(open_ts=open_ts, tf_s=self.tf_s)
        elif open_ts > self.forming.open_ts:
            # close current, emit empties for skipped buckets, open new
            c = _freeze(self.forming, self.prev_close)
            self.prev_close = c.close if c.trade_count else self.prev_close
            closed.append(c)
            nxt = self.forming.open_ts + self.tf_s
            while nxt < open_ts:
                empty = _freeze(FormingCandle(open_ts=nxt, tf_s=self.tf_s),
                                self.prev_close)
                closed.append(empty)
                nxt += self.tf_s
            self.forming = FormingCandle(open_ts=open_ts, tf_s=self.tf_s)
        elif open_ts < self.forming.open_ts:
            raise ValueError(
                f"trade ts {t.ts} belongs to an already-closed candle "
                f"(bucket {open_ts} < forming {self.forming.open_ts}); "
                "tape must be time-ordered")
        self.forming.apply(t)
        return closed

    def flush(self) -> Optional[ClosedCandle]:
        """Force-close the forming candle (end of tape / shutdown)."""
        if self.forming is None or self.forming.trade_count == 0:
            return None
        c = _freeze(self.forming, self.prev_close)
        self.prev_close = c.close
        self.forming = None
        return c


def candles_from_trades(trades: list[Trade], tf: str,
                        include_final: bool = True) -> list[ClosedCandle]:
    """Convenience batch path (backfill): same builder, same semantics."""
    b = CandleBuilder(tf)
    out: list[ClosedCandle] = []
    for t in trades:
        out.extend(b.on_trade(t))
    if include_final:
        last = b.flush()
        if last is not None:
            out.append(last)
    return out
