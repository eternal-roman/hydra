"""Finished bars must reach the engine in their final form, and a dead
candle stream must not stop a live pair from ticking.

Regressions:
  * CandleStream kept only the newest push, so a 300s tick saw a finished
    1h bar as it stood up to 5 minutes before its close. Every engine bar,
    and every daily close the trend overlay used, carried a truncated
    close/high/low while the tape (and so the backtest) stored the true bar.
  * In live mode `_fetch_and_tick` returned None whenever the stream had no
    candle, skipping the pair's tick — no exits and no halt flatten — for
    the whole outage, although boot printed "falling back to REST ohlc".
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import HydraAgent
from hydra_engine import HydraEngine
from hydra_kraken_cli import KrakenCLI
from hydra_streams import CandleStream, candle_open_epoch

PAIR = "BTC/USD"


def _push(stream, begin, close, low=None):
    stream._on_message({"channel": "ohlc", "data": [{
        "symbol": PAIR, "interval_begin": begin, "open": 60000.0,
        "high": max(60100.0, close), "low": low if low is not None else min(59900.0, close),
        "close": close, "volume": 5.0,
    }]})


def test_rollover_keeps_the_final_push_of_the_finished_bar():
    stream = CandleStream([PAIR], interval=60, paper=True)
    _push(stream, "2026-10-01T10:00:00Z", 60050.0)
    _push(stream, "2026-10-01T10:00:00Z", 59300.0, low=59200.0)  # final 10:00 state
    assert stream.latest_closed_candle(PAIR) is None
    _push(stream, "2026-10-01T11:00:00Z", 59310.0)
    closed = stream.latest_closed_candle(PAIR)
    assert closed["close"] == 59300.0 and closed["low"] == 59200.0
    assert stream.latest_candle(PAIR)["interval_begin"] == "2026-10-01T11:00:00Z"


def test_a_late_push_for_an_older_bar_does_not_replace_the_forming_bar():
    stream = CandleStream([PAIR], interval=60, paper=True)
    _push(stream, "2026-10-01T11:00:00Z", 59310.0)
    _push(stream, "2026-10-01T10:00:00Z", 59000.0)
    assert stream.latest_candle(PAIR)["interval_begin"] == "2026-10-01T11:00:00Z"


def test_candle_open_epoch_parses_both_shapes():
    assert candle_open_epoch({"interval_begin": "1970-01-01T00:01:00Z"}) == 60.0
    assert candle_open_epoch({"timestamp": 120}) == 120.0
    assert candle_open_epoch({"interval_begin": "garbage"}) is None
    assert candle_open_epoch(None) is None


class _HealthyStream(CandleStream):
    @property
    def healthy(self):
        return True


def _agent(stream):
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.demo = False
    agent.candle_interval = 60
    agent.engines = {PAIR: HydraEngine(initial_balance=1000.0, asset=PAIR)}
    agent.candle_stream = stream
    agent.s3 = None
    agent._resting_entry = lambda pair: None
    return agent


def test_fetch_and_tick_rewrites_the_finished_bar_with_its_true_close():
    stream = _HealthyStream([PAIR], interval=60, paper=True)
    agent = _agent(stream)
    eng = agent.engines[PAIR]
    _push(stream, "2026-10-01T10:00:00Z", 60050.0)
    agent._fetch_and_tick(PAIR)  # tick at 10:55 sees the truncated bar
    assert eng.candles[-1].close == 60050.0
    _push(stream, "2026-10-01T10:00:00Z", 59300.0, low=59200.0)
    _push(stream, "2026-10-01T11:00:00Z", 59310.0)
    agent._fetch_and_tick(PAIR)  # tick at 11:03
    ten = [c for c in eng.candles if c.timestamp == candle_open_epoch(
        {"interval_begin": "2026-10-01T10:00:00Z"})][0]
    assert ten.close == 59300.0 and ten.low == 59200.0
    assert eng.candles[-1].close == 59310.0


def test_live_pair_ticks_from_the_cli_when_the_stream_is_dead():
    stream = CandleStream([PAIR], interval=60, paper=False)  # never started
    agent = _agent(stream)
    rows = [{"timestamp": 1_700_000_000 + 3600 * i, "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0 + i, "volume": 1.0} for i in range(3)]
    with mock.patch.object(KrakenCLI, "ohlc", return_value=rows) as ohlc, \
            mock.patch("hydra_agent.time.sleep"):
        state = agent._fetch_and_tick(PAIR)
    assert ohlc.call_count == 1
    assert state is not None
    assert agent.engines[PAIR].candles[-1].close == 102.0
