"""Shadow ledger: dedupe across restarts, arm-specific closes, atomic
state, JSONL integrity, no order path."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.candles import DailyBar  # noqa: E402
from s3bounce.shadow import FEE_PER_SIDE, ShadowLedger  # noqa: E402

DAY = 86400


def bar(i, o, h, low, c):
    return DailyBar(open_ts=float(i * DAY), open=o, high=h, low=low,
                    close=c, volume=1.0)


def propose(led, arms=("x0_registered", "x1_close_stop")):
    return led.propose(asset="BTC/USD", low_ts=10 * DAY, low_px=100.0,
                       atr=2.0, low_idx=10, entry_idx=12,
                       entry_ts=12 * DAY, entry_px=103.0, score=0.61,
                       arms=list(arms), confirmer={"status": "no_opinion"})


def test_dedupe_survives_restart(tmp_path):
    d = str(tmp_path / "led")
    led = ShadowLedger(d)
    assert propose(led) is True
    assert propose(led) is False
    led2 = ShadowLedger(d)                     # restart
    assert propose(led2) is False
    assert len(led2.open) == 2                 # both arms restored


def test_arm_specific_closes(tmp_path):
    led = ShadowLedger(str(tmp_path / "led"))
    propose(led)
    # wick below L0, close above: x0 stops (fill L0), x1 stays open
    closes = led.mark_bar("BTC/USD", bar(13, 102, 103, 99.0, 101.0))
    assert [c["arm"] for c in closes] == ["x0_registered"]
    assert closes[0]["exit_px"] == 100.0 and closes[0]["reason"] == "stop"
    assert abs(closes[0]["ret_net"] -
               (100.0 / 103.0 - 1 - 2 * FEE_PER_SIDE)) < 1e-12
    # close below L0: x1 stops at close
    closes = led.mark_bar("BTC/USD", bar(14, 101, 102, 98.0, 99.0))
    assert [c["arm"] for c in closes] == ["x1_close_stop"]
    assert closes[0]["reason"] == "stop_close" and closes[0]["exit_px"] == 99.0
    assert led.open == []


def test_bars_before_entry_ignored(tmp_path):
    led = ShadowLedger(str(tmp_path / "led"))
    propose(led)
    assert led.mark_bar("BTC/USD", bar(12, 100, 101, 90.0, 95.0)) == []
    assert all(p["bars_seen"] == 0 for p in led.open)


def test_garbage_state_treated_as_empty(tmp_path):
    d = tmp_path / "led"
    d.mkdir()
    (d / "state.json").write_text("{corrupt")
    (d / "state.tmp").write_text("garbage from interrupted write")
    led = ShadowLedger(str(d))
    assert led.open == [] and led.proposed == set()
    assert propose(led) is True                # functional after corruption


def test_events_are_valid_jsonl(tmp_path):
    led = ShadowLedger(str(tmp_path / "led"))
    propose(led)
    led.mark_bar("BTC/USD", bar(13, 101, 110, 100.5, 108))   # target both arms
    lines = led.events_path.read_text().strip().splitlines()
    events = [json.loads(x) for x in lines]
    assert [e["type"] for e in events] == ["proposal", "close", "close"]
    assert all(e["reason"] == "target" for e in events[1:])


def test_no_order_path():
    src = (Path(__file__).resolve().parents[1] / "s3bounce").glob("*.py")
    forbidden = ("add_order", "_place_order", "execute_signal", "requests.",
                 "urllib", "websocket", "socket")
    for f in src:
        text = f.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{f.name} contains {token!r}"
