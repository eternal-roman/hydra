"""Kraken public REST client: OHLC bootstrap + Trades backfill.

Rate-limit aware (token bucket, public tier ~1 req/s sustained). Public
data only; optional KRAKEN_KEY/KRAKEN_SECRET are read from env for future
private endpoints and are NEVER logged.

Fail-loud: HTTP errors and Kraken `error` payloads raise KrakenRestError
after bounded retries; callers decide whether the failure taints candles.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from .tape import Side, Trade

log = logging.getLogger("heartbeat.rest")

# Kraken REST pair aliases (public endpoints accept these).
_REST_PAIR = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "ZEC/USD": "ZECUSD",
}

_INTERVAL_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def rest_pair(pair: str) -> str:
    return _REST_PAIR.get(pair, pair.replace("/", ""))


def interval_minutes(tf: str) -> int:
    try:
        return _INTERVAL_MIN[tf]
    except KeyError:
        raise ValueError(f"unsupported timeframe {tf!r}; one of {sorted(_INTERVAL_MIN)}")


class KrakenRestError(RuntimeError):
    pass


class TokenBucket:
    """Deterministic-enough limiter for I/O (not part of the math path)."""

    def __init__(self, rate_per_s: float, burst: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = rate_per_s
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                self._sleep((1.0 - self.tokens) / self.rate)


@dataclass(frozen=True)
class Ohlc:
    ts: float      # candle OPEN time, epoch seconds
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


class KrakenRest:
    def __init__(self, base_url: str = "https://api.kraken.com",
                 rate_per_s: float = 0.9, burst: int = 3,
                 session: Optional[requests.Session] = None,
                 max_retries: int = 4) -> None:
        self.base_url = base_url.rstrip("/")
        self.bucket = TokenBucket(rate_per_s, burst)
        self.session = session or requests.Session()
        self.max_retries = max_retries
        # Optional keys for future private endpoints. Never logged.
        self._key = os.environ.get("KRAKEN_KEY")
        self._secret = os.environ.get("KRAKEN_SECRET")

    # -- plumbing -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self.bucket.acquire()
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code in (429, 502, 503, 504):
                    raise KrakenRestError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("error"):
                    err = ",".join(payload["error"])
                    if "EGeneral:Too many requests" in err or "Rate limit" in err:
                        raise KrakenRestError(err)
                    raise KrakenRestError(f"kraken error: {err}")
                return payload["result"]
            except (requests.RequestException, KrakenRestError) as e:
                last_err = e
                if attempt < self.max_retries:
                    backoff = 2.0 ** attempt
                    log.warning("REST %s failed (%s); retry in %.0fs", path, e, backoff)
                    time.sleep(backoff)
        raise KrakenRestError(f"REST {path} failed after {self.max_retries + 1} attempts: {last_err}")

    # -- endpoints ----------------------------------------------------------

    def ohlc(self, pair: str, tf: str, since: Optional[float] = None) -> list[Ohlc]:
        """Up to 720 most recent candles (Kraken hard cap). Excludes the
        still-forming candle (last array entry, per Kraken docs)."""
        params: dict[str, Any] = {"pair": rest_pair(pair), "interval": interval_minutes(tf)}
        if since is not None:
            params["since"] = int(since)
        result = self._get("/0/public/OHLC", params)
        rows_key = next(k for k in result if k != "last")
        rows = result[rows_key]
        out = [Ohlc(float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                    float(r[4]), float(r[5]), float(r[6]), int(r[7]))
               for r in rows[:-1]]  # drop forming candle
        return out

    def trades_page(self, pair: str, since: int = 0) -> tuple[list[Trade], int]:
        """One page (<=1000 trades) from the given `since` cursor.

        Returns (trades, next_cursor). next_cursor == since means no progress
        (caller should stop). Cursor is Kraken's `last` (ns precision id).
        """
        result = self._get("/0/public/Trades", {"pair": rest_pair(pair), "since": since})
        rows_key = next(k for k in result if k != "last")
        rows = result[rows_key]
        next_cursor = int(result["last"])
        trades = []
        for r in rows:
            # [price, volume, time, buy/sell, market/limit, misc, trade_id]
            side = Side.BUY if r[3] == "b" else Side.SELL
            ord_type = "market" if r[4] == "m" else "limit"
            tid = int(r[6]) if len(r) > 6 else 0
            trades.append(Trade(ts=float(r[2]), price=float(r[0]), qty=float(r[1]),
                                side=side, ord_type=ord_type, trade_id=tid))
        return trades, next_cursor

    def trades_range(self, pair: str, ts_start: float, ts_end: float,
                     on_page: Optional[Callable[[list[Trade]], None]] = None,
                     max_pages: int = 1_000_000,
                     collect: bool = True) -> tuple[list[Trade], bool]:
        """All trades in [ts_start, ts_end]. Returns (trades, complete).

        complete=False when pagination stalled or max_pages hit before
        reaching ts_end — the caller MUST taint the uncovered range.

        collect=False streams pages to `on_page` only and returns an empty
        list — a multi-month backfill is millions of trades and must not
        accumulate in memory when every page is already persisted.
        """
        cursor = int(ts_start * 1_000_000_000)
        collected: list[Trade] = []
        for _ in range(max_pages):
            page, next_cursor = self.trades_page(pair, since=cursor)
            in_range = [t for t in page if ts_start <= t.ts <= ts_end]
            if collect:
                collected.extend(in_range)
            if on_page and in_range:
                on_page(in_range)
            if page and page[-1].ts > ts_end:
                return collected, True
            if next_cursor == cursor:  # no progress: caught up to now
                return collected, True
            if not page and next_cursor != cursor:
                cursor = next_cursor
                continue
            cursor = next_cursor
        return collected, False
