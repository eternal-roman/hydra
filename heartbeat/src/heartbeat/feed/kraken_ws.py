"""Kraken WebSocket v2 trade-channel client.

Responsibilities:
  * subscribe to `trade` for one pair on wss://ws.kraken.com/v2;
  * normalize each trade message to `tape.Trade` (exchange timestamps);
  * respond to Kraken heartbeat/ping per docs (websockets lib answers
    protocol-level pings automatically; Kraken v2 additionally sends
    `heartbeat` channel messages which we use for liveness);
  * auto-reconnect with exponential backoff;
  * on reconnect, backfill the gap via REST Trades `since` cursor and
    emit the backfilled trades BEFORE new live trades; if backfill is
    incomplete, report the gap so affected candles are tainted.

The client is transport-only: it never computes features. All trades are
delivered via an async callback in exchange-timestamp order.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import WebSocketException

from .kraken_rest import KrakenRest
from .tape import Side, TapeMonitor, Trade, normalize_trades

log = logging.getLogger("heartbeat.ws")

# WS v2 uses ISO-ish symbols: BTC/USD stays BTC/USD.
TradeCallback = Callable[[Trade, float], Awaitable[None]]  # (trade, local_recv_ts)


def parse_ws_trade(item: dict, pair: str) -> Trade:
    """Normalize one element of a WS v2 trade `data` array."""
    ts_str = item["timestamp"]  # RFC3339, e.g. 2026-07-17T12:34:56.789012Z
    ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    return Trade(
        ts=ts,
        price=float(item["price"]),
        qty=float(item["qty"]),
        side=Side.BUY if item["side"] == "buy" else Side.SELL,
        ord_type=item.get("ord_type", "limit"),
        trade_id=int(item.get("trade_id", 0)),
    )


class KrakenWsClient:
    LIVENESS_TIMEOUT_S = 20.0  # Kraken heartbeats every ~1s; 20s silence = dead

    def __init__(self, pair: str, on_trade: TradeCallback,
                 monitor: TapeMonitor,
                 rest: Optional[KrakenRest] = None,
                 ws_url: str = "wss://ws.kraken.com/v2",
                 reconnect_base_s: float = 1.0,
                 reconnect_max_s: float = 60.0,
                 clock: Callable[[], float] = None) -> None:
        import time as _time
        self.pair = pair
        self.on_trade = on_trade
        self.monitor = monitor
        self.rest = rest
        self.ws_url = ws_url
        self.reconnect_base_s = reconnect_base_s
        self.reconnect_max_s = reconnect_max_s
        self._clock = clock or _time.time
        self._stop = asyncio.Event()
        self.last_trade_ts: Optional[float] = None
        self.connected: bool = False

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect/reconnect loop. Returns only after stop()."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0  # clean disconnect -> reset backoff
            except (WebSocketException, OSError, asyncio.TimeoutError) as e:
                self.connected = False
                backoff = min(self.reconnect_max_s, self.reconnect_base_s * (2 ** attempt))
                attempt += 1
                log.warning("WS dropped (%s); reconnect in %.1fs (attempt %d)",
                            e, backoff, attempt)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

    async def _session(self) -> None:
        gap_start = self.last_trade_ts  # None on the very first connect
        async with websockets.connect(self.ws_url, ping_interval=20,
                                      ping_timeout=20) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {"channel": "trade", "symbol": [self.pair]},
            }))
            self.connected = True
            log.info("WS connected, subscribed trade %s", self.pair)
            if gap_start is not None:
                await self._backfill_gap(gap_start)
            while not self._stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=self.LIVENESS_TIMEOUT_S)
                await self._handle(raw)
        self.connected = False

    async def _handle(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        channel = msg.get("channel")
        if channel == "heartbeat":
            return
        if channel == "status" or msg.get("method") in ("subscribe", "pong"):
            if msg.get("success") is False:
                raise WebSocketException(f"subscribe failed: {msg}")
            return
        if channel == "trade":
            local_ts = self._clock()
            for item in msg.get("data", []):
                trade = parse_ws_trade(item, self.pair)
                self.monitor.observe(trade, local_ts=local_ts)
                self.last_trade_ts = trade.ts
                await self.on_trade(trade, local_ts)

    async def _backfill_gap(self, gap_start: float) -> None:
        """Close a reconnect gap [last seen trade, now] via REST Trades."""
        gap_end = self._clock()
        if self.rest is None:
            self.monitor.mark_gap(gap_start, gap_end, "no REST client for backfill",
                                  backfilled=False)
            return
        try:
            loop = asyncio.get_running_loop()
            trades, complete = await loop.run_in_executor(
                None, lambda: self.rest.trades_range(self.pair, gap_start, gap_end))
        except Exception as e:  # noqa: BLE001 - any backfill failure taints
            self.monitor.mark_gap(gap_start, gap_end, f"backfill failed: {e}",
                                  backfilled=False)
            return
        emitted = 0
        for t in normalize_trades(trades):
            if self.last_trade_ts is not None and t.ts <= self.last_trade_ts \
                    and t.trade_id and t.trade_id <= self.monitor.last_trade_id:
                continue  # already seen before the drop
            self.monitor.observe(t)
            self.last_trade_ts = t.ts
            await self.on_trade(t, self._clock())
            emitted += 1
        self.monitor.mark_gap(gap_start, gap_end,
                              f"reconnect backfill emitted {emitted} trades",
                              backfilled=complete)
        if not complete:
            log.error("gap backfill INCOMPLETE %s..%s — candles tainted",
                      gap_start, gap_end)
