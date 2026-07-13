"""Real-time chart feed: CandleStream pushes relay to the dashboard as
`candle_update` messages so charts move at WS speed, not tick cadence."""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent


class _CaptureBroadcaster:
    def __init__(self):
        self.sent = []

    def broadcast_message(self, msg_type, payload):
        self.sent.append((msg_type, payload))


def _agent():
    a = HydraAgent.__new__(HydraAgent)
    a.broadcaster = _CaptureBroadcaster()
    return a


def test_relay_converts_ws_entry_to_chart_candle():
    a = _agent()
    a._relay_candle_update("SOL/USD", {
        "symbol": "SOL/USD", "open": "150.1", "high": 151.0, "low": 149.5,
        "close": 150.7, "volume": 12.5,
        "interval_begin": "2026-07-13T18:00:00.000000Z",
    })
    assert len(a.broadcaster.sent) == 1
    msg_type, payload = a.broadcaster.sent[0]
    assert msg_type == "candle_update"
    assert payload["pair"] == "SOL/USD"
    c = payload["candle"]
    assert c["o"] == 150.1 and c["h"] == 151.0
    assert c["l"] == 149.5 and c["c"] == 150.7
    # interval_begin ISO → epoch, matching the tick loop's engine ingestion
    assert isinstance(c["t"], float) and c["t"] == 1783965600.0


def test_relay_numeric_timestamp_passthrough():
    a = _agent()
    a._relay_candle_update("BTC/USD", {
        "open": 80000, "high": 80100, "low": 79900, "close": 80050,
        "timestamp": 1783965600,
    })
    assert a.broadcaster.sent[0][1]["candle"]["t"] == 1783965600.0


def test_relay_never_raises_on_junk_or_missing_broadcaster():
    """Runs inside the WS thread — any exception here would be caught by
    the stream's callback guard, but the relay must not even get that far."""
    bare = HydraAgent.__new__(HydraAgent)  # no broadcaster attribute
    bare._relay_candle_update("X/Y", {})
    a = _agent()
    a.broadcaster = None
    a._relay_candle_update("X/Y", {"open": None, "interval_begin": object()})
    a2 = _agent()
    a2._relay_candle_update("X/Y", {"open": None, "high": "", "low": None,
                                    "close": None})
    assert a2.broadcaster.sent[0][1]["candle"]["o"] == 0.0
