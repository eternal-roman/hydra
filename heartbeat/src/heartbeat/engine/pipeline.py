"""HeartbeatPipeline — the ONE code path from trades to posterior.

live `run`, `replay`, and backfill evaluation all push trades through
this exact class, so replay-vs-live equivalence (Phase 1 gate) and the
no-lookahead property (Phase 2 gate) are properties of the only path
that exists, not of a parallel reimplementation.

Responsibilities:
  * micro-bucketing: one heartbeat per trade, but when the trailing-1s
    trade rate exceeds `bucket_rate_threshold`, trades landing in the
    same `micro_bucket_ms` exchange-time bucket as the previous heartbeat
    fold into candle state without emitting a heartbeat (pure CPU relief;
    the 1/h evidence scaling makes the posterior rate-invariant);
  * candle lifecycle: freeze ATR/scalers/h at open, snapshot at close;
  * taint propagation from the TapeMonitor to heartbeats and candles.

Everything is driven by exchange timestamps — no wall clock in here.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Optional, Sequence

from ..feed.tape import TapeMonitor, Trade
from ..features.registry import FeatureContext
from ..features.tier0 import robust_atr
from .candle import CandleBuilder, ClosedCandle
from .posterior import HeartbeatOutput, PosteriorEngine

HeartbeatSink = Callable[[HeartbeatOutput, float], None]  # (out, candle_progress)
CandleSink = Callable[[dict], None]                       # posterior row


class HeartbeatPipeline:
    HISTORY_MAX = 700  # closed candles kept for feature lookbacks

    def __init__(self, config: dict, pair: str, tf: str,
                 engine: Optional[PosteriorEngine] = None,
                 monitor: Optional[TapeMonitor] = None,
                 on_heartbeat: Optional[HeartbeatSink] = None,
                 on_candle: Optional[CandleSink] = None) -> None:
        self.config = config
        self.pair = pair
        self.tf = tf
        self.engine = engine or PosteriorEngine(config)
        self.monitor = monitor or TapeMonitor(
            float(config.get("feed", {}).get("clock_skew_alert_s", 2.0)))
        self.builder = CandleBuilder(tf)
        self.history: deque[ClosedCandle] = deque(maxlen=self.HISTORY_MAX)
        self.on_heartbeat = on_heartbeat
        self.on_candle = on_candle

        hb = config.get("heartbeat", {})
        self.bucket_s = float(hb.get("micro_bucket_ms", 500)) / 1000.0
        self.rate_threshold = float(hb.get("bucket_rate_threshold", 20.0))
        self._recent_ts: deque[float] = deque()
        self._last_hb_bucket: Optional[int] = None

        acfg = config.get("atr", {})
        self._atr_period = int(acfg.get("period", 14))
        self._atr_outlier = float(acfg.get("outlier_mult", 3.0))
        self._atr_frozen: Optional[float] = None
        self._cur_open_ts: Optional[float] = None
        self.last_output: Optional[HeartbeatOutput] = None

    # -- bootstrap ---------------------------------------------------------------

    def bootstrap(self, candles: Sequence[ClosedCandle]) -> int:
        """Warm scalers + history from historical closed candles (must all
        precede any trade later fed in; enforced via builder ordering)."""
        pushed = self.engine.warm_scalers_from_candles(list(candles))
        for c in candles:
            self.history.append(c)
        if candles:
            self.builder.prev_close = candles[-1].close
        return pushed

    # -- main entry ---------------------------------------------------------------

    def feed_trade(self, trade: Trade,
                   local_ts: Optional[float] = None,
                   observe: bool = True) -> Optional[HeartbeatOutput]:
        """Push one trade. Returns the HeartbeatOutput if a heartbeat fired
        (None when the trade folded into a micro-bucket)."""
        if observe:
            self.monitor.observe(trade, local_ts=local_ts)

        for closed in self.builder.on_trade(trade):
            self._close_candle(closed)

        if self._cur_open_ts != self.builder.forming.open_ts:
            self._open_candle()

        if not self._should_heartbeat(trade.ts):
            return None
        forming = self.builder.forming
        tainted = self.monitor.taint.overlaps(forming.open_ts, trade.ts)
        ctx = FeatureContext(forming=forming, closed=tuple(self.history),
                             atr=self._atr_frozen, config=self.config)
        out = self.engine.heartbeat(ctx, ts=trade.ts, tainted=tainted)
        self.last_output = out
        if self.on_heartbeat:
            self.on_heartbeat(out, forming.progress)
        return out

    def flush(self) -> Optional[dict]:
        """Force-close the forming candle (end of tape)."""
        last = self.builder.flush()
        if last is None:
            return None
        return self._close_candle(last)

    # -- internals ---------------------------------------------------------------

    def _open_candle(self) -> None:
        self._cur_open_ts = self.builder.forming.open_ts
        self._atr_frozen = robust_atr(tuple(self.history), self._atr_period,
                                      self._atr_outlier)
        self.engine.on_candle_open()
        self._last_hb_bucket = None

    def _close_candle(self, candle: ClosedCandle) -> dict:
        tainted = self.monitor.taint.overlaps(candle.open_ts, candle.close_ts)
        if tainted:
            candle = ClosedCandle(**{**_candle_fields(candle), "tainted": True})
        snap = self.engine.on_candle_close(candle)
        self.history.append(candle)
        row = {
            "ts": candle.close_ts,
            "candle_open_ts": candle.open_ts,
            "open": candle.open, "high": candle.high,
            "low": candle.low, "close": candle.close,
            "volume": candle.volume,
            "buy_vol": candle.buy_vol, "sell_vol": candle.sell_vol,
            "trade_count": candle.trade_count,
            "vwap": candle.vwap,
            "L": snap["L"], "p_up": snap["p_up"],
            "tainted": tainted,
            "features_json": _stable_json(snap["features"]),
        }
        if self.on_candle:
            self.on_candle(row)
        return row

    def _should_heartbeat(self, ts: float) -> bool:
        self._recent_ts.append(ts)
        while self._recent_ts and self._recent_ts[0] <= ts - 1.0:
            self._recent_ts.popleft()
        rate = float(len(self._recent_ts))  # trades in trailing 1s
        bucket = int(ts / self.bucket_s)
        if rate > self.rate_threshold and bucket == self._last_hb_bucket:
            return False
        self._last_hb_bucket = bucket
        return True


def _candle_fields(c: ClosedCandle) -> dict:
    return {
        "open_ts": c.open_ts, "close_ts": c.close_ts, "open": c.open,
        "high": c.high, "low": c.low, "close": c.close, "volume": c.volume,
        "buy_vol": c.buy_vol, "sell_vol": c.sell_vol,
        "trade_count": c.trade_count, "vwap": c.vwap,
        "buy_size_sum": c.buy_size_sum, "buy_count": c.buy_count,
        "sell_size_sum": c.sell_size_sum, "sell_count": c.sell_count,
        "max_buy_streak": c.max_buy_streak,
        "max_sell_streak": c.max_sell_streak,
        "vol_bottom_third": c.vol_bottom_third, "tainted": c.tainted,
    }


def _stable_json(obj) -> str:
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def run_tape(config: dict, pair: str, tf: str, trades: Sequence[Trade],
             bootstrap_candles: Sequence[ClosedCandle] = (),
             on_heartbeat: Optional[HeartbeatSink] = None,
             engine: Optional[PosteriorEngine] = None,
             flush_final: bool = True) -> list[dict]:
    """Deterministic batch replay: same tape + config -> bit-identical rows."""
    pipe = HeartbeatPipeline(config, pair, tf, engine=engine,
                             on_heartbeat=on_heartbeat)
    rows: list[dict] = []
    pipe.on_candle = rows.append
    if bootstrap_candles:
        pipe.bootstrap(bootstrap_candles)
    for t in trades:
        pipe.feed_trade(t)
    if flush_final:
        pipe.flush()
    return rows
