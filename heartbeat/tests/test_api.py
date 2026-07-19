"""Live confirmation API contract tests (Phase 5)."""

import asyncio
import json

from heartbeat.api import TcpStatusServer, status_payload, write_status_file
from heartbeat.engine.pipeline import HeartbeatPipeline
from helpers import base_config, mk_trade


def _pipe_with_data():
    cfg = base_config()
    pipe = HeartbeatPipeline(cfg, "BTC/USD", "1h")
    for i in range(10):
        pipe.feed_trade(mk_trade(3600.0 + i * 10, 100.0 + i,
                                 side="buy" if i % 2 else "sell", tid=i + 1))
    return pipe


def test_status_payload_fields():
    pipe = _pipe_with_data()
    p = status_payload("BTC/USD", "1h", pipe, pipe.monitor)
    assert p["pair"] == "BTC/USD" and p["tf"] == "1h"
    assert 0.0 <= p["p_up"] <= 1.0
    assert p["ts"] == 3690.0
    assert 0.0 <= p["candle_progress"] <= 1.0
    assert p["tainted"] is False
    assert p["gap_count"] == 0 and p["alerts"] == 0


def test_status_file_atomic_write(tmp_path):
    pipe = _pipe_with_data()
    path = tmp_path / "sub" / "status.json"
    payload = status_payload("BTC/USD", "1h", pipe, pipe.monitor)
    write_status_file(path, payload)
    assert json.loads(path.read_text()) == payload
    write_status_file(path, dict(payload, p_up=0.9))  # overwrite in place
    assert json.loads(path.read_text())["p_up"] == 0.9
    assert not path.with_suffix(".tmp").exists()


def test_tcp_query_roundtrip():
    pipe = _pipe_with_data()

    async def scenario():
        server = TcpStatusServer(
            lambda: status_payload("BTC/USD", "1h", pipe, pipe.monitor))
        await server.start("127.0.0.1", 0)  # ephemeral port
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        for _ in range(3):  # repeated queries on one connection
            writer.write(b"?\n")
            await writer.drain()
            line = await reader.readline()
            payload = json.loads(line)
            assert payload["pair"] == "BTC/USD"
            assert 0.0 <= payload["p_up"] <= 1.0
        writer.close()
        await server.close()

    asyncio.run(scenario())
