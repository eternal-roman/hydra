"""Live confirmation API — the contract an external trading bot consumes.

Two transports, same JSON payload (see README "Integration contract"):

  1. STATUS FILE (`api.status_file`): atomically rewritten (tmp +
     os.replace) after every heartbeat. Poll-friendly; a reader never
     sees a torn write.
  2. TCP QUERY (`api.tcp_host`:`api.tcp_port`): line-oriented — send any
     line ("\n"), receive exactly one JSON line back with the current
     payload. Connection stays open for repeated queries.

Payload fields:
    pair, tf            configured market
    p_up                posterior P(up) at the last heartbeat (0..1)
    L                   log-odds
    ts                  exchange timestamp of the last heartbeat
    candle_progress     fraction of the forming candle elapsed (0..1)
    tainted             last heartbeat fell in a tainted range
    gap_count           feed gaps observed this session
    max_clock_skew_s    worst |local - exchange| seen
    alerts              total TapeAlerts

Consumers MUST treat `tainted: true` (or a stale `ts`) as "no opinion".
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional


def status_payload(pair: str, tf: str, pipe, monitor) -> dict:
    out = pipe.last_output
    forming = pipe.builder.forming
    return {
        "pair": pair, "tf": tf,
        "p_up": out.p_up if out else None,
        "L": out.L if out else None,
        "ts": out.ts if out else None,
        "candle_progress": forming.progress if forming else None,
        "tainted": out.tainted if out else None,
        "gap_count": monitor.gap_count,
        "max_clock_skew_s": round(monitor.max_skew_s, 3),
        "alerts": len(monitor.alerts),
    }


def write_status_file(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


class TcpStatusServer:
    """Line-oriented TCP status endpoint. `payload_fn` is called per query."""

    def __init__(self, payload_fn) -> None:
        self.payload_fn = payload_fn
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("server not started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.readline():
                writer.write((json.dumps(self.payload_fn()) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
