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
    features            optional {name: {z, raw}} from last heartbeat

Consumers MUST treat `tainted: true` (or a stale `ts`) as "no opinion".

Multi-pair path contract (Hydra S3 confirmer + dashboard surface):
    resolve_status_path("data/heartbeat_status.json", "BTC/USD")
        → data/heartbeat_status_BTC_USD.json
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional


def resolve_status_path(status_file_or_dir: str | Path, pair: str) -> Path:
    """Map api.status_file (+ pair) → multi-pair status path Hydra polls.

    Generic ``heartbeat_status.json`` becomes ``heartbeat_status_BTC_USD.json``
    so concurrent `heartbeat run --pair …` processes do not clobber each
    other and match ``hydra_s3`` / ``hydra_heartbeat_surface`` filenames.
    """
    raw = Path(status_file_or_dir)
    token = pair.replace("/", "_")
    pair_name = f"heartbeat_status_{token}.json"
    if token in raw.name and raw.suffix == ".json":
        return raw
    if raw.suffix != ".json":
        return raw / pair_name
    return raw.parent / pair_name


def status_payload(pair: str, tf: str, pipe, monitor) -> dict:
    out = pipe.last_output
    forming = pipe.builder.forming
    features = None
    if out is not None:
        z = getattr(out, "z", None) or {}
        raw = getattr(out, "raw", None) or {}
        if z or raw:
            keys = set(z) | set(raw)
            features = {
                k: {"z": z.get(k), "raw": raw.get(k)} for k in sorted(keys)
            }
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
        "features": features,
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
