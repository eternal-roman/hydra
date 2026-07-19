"""REST client tests against a mocked transport (no network)."""

import pytest

from heartbeat.feed.kraken_rest import (KrakenRest, KrakenRestError,
                                        TokenBucket, interval_minutes,
                                        rest_pair)
from heartbeat.feed.tape import Side


def test_pair_and_interval_mapping():
    assert rest_pair("BTC/USD") == "XBTUSD"
    assert rest_pair("ZEC/USD") == "ZECUSD"
    assert interval_minutes("1h") == 60
    with pytest.raises(ValueError):
        interval_minutes("7m")


def test_token_bucket_enforces_rate():
    t = {"now": 0.0}
    slept = []
    bucket = TokenBucket(rate_per_s=1.0, burst=2,
                         clock=lambda: t["now"],
                         sleep=lambda s: (slept.append(s),
                                          t.__setitem__("now", t["now"] + s)))
    bucket.acquire(); bucket.acquire()   # burst OK, no sleep
    assert slept == []
    bucket.acquire()                     # must wait ~1s
    assert len(slept) == 1 and abs(slept[0] - 1.0) < 1e-9


class FakeRest(KrakenRest):
    """Override transport; keep parsing/pagination logic real."""

    def __init__(self, pages):
        super().__init__(rate_per_s=1000, burst=100)
        self.pages = pages  # list of (rows, last_cursor)
        self.calls = []

    def _get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/0/public/OHLC":
            rows = [[3600 * i, "100", "101", "99", "100.5", "100.2", "5.0", 7]
                    for i in range(4)]
            return {"XXBTZUSD": rows, "last": 3600 * 3}
        rows, last = self.pages.pop(0)
        return {"XXBTZUSD": rows, "last": last}


def test_ohlc_parse_drops_forming():
    r = FakeRest([])
    out = r.ohlc("BTC/USD", "1h")
    assert len(out) == 3  # 4 rows, last (forming) dropped
    assert out[0].open == 100.0 and out[0].count == 7


def _trade_row(ts, price="100", vol="1", side="b", tid=1):
    return [price, vol, ts, side, "l", "", tid]


def test_trades_pagination_complete():
    pages = [
        ([_trade_row(10.0, tid=1), _trade_row(11.0, side="s", tid=2)], 11_000_000_000),
        ([_trade_row(12.0, tid=3), _trade_row(200.0, tid=4)], 200_000_000_000),
    ]
    r = FakeRest(pages)
    trades, complete = r.trades_range("BTC/USD", 10.0, 20.0)
    assert complete
    assert [t.trade_id for t in trades] == [1, 2, 3]
    assert trades[1].side is Side.SELL


def test_trades_pagination_stall_is_complete_no_more_data():
    pages = [([_trade_row(10.0, tid=1)], 555), ([], 555)]  # cursor stops moving
    r = FakeRest(pages)
    r.pages[0] = ([_trade_row(10.0, tid=1)], 555)
    trades, complete = r.trades_range("BTC/USD", 10.0, 20.0)
    assert complete and len(trades) == 1


def test_trades_pagination_incomplete_flags_false():
    pages = [([_trade_row(10.0 + i, tid=i)], 1_000 + i) for i in range(50)]
    r = FakeRest(pages)
    trades, complete = r.trades_range("BTC/USD", 5.0, 10_000.0, max_pages=3)
    assert not complete  # never reached ts_end and pages kept moving


def test_error_payload_raises():
    class ErrRest(KrakenRest):
        def __init__(self):
            super().__init__(rate_per_s=1000, burst=10, max_retries=0)

        def _get(self, path, params):
            raise KrakenRestError("kraken error: EQuery:Unknown asset pair")

    with pytest.raises(KrakenRestError):
        ErrRest().ohlc("NOPE/USD", "1h")
