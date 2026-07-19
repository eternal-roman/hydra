import pytest

from heartbeat.engine.candle import CandleBuilder, candles_from_trades
from helpers import mk_trade


def test_single_candle_ohlcv_hand_computed():
    b = CandleBuilder("1h")
    trades = [
        mk_trade(3600.0, 100.0, 2.0, "buy"),
        mk_trade(3700.0, 105.0, 1.0, "sell"),
        mk_trade(3800.0, 95.0, 3.0, "buy"),
        mk_trade(3900.0, 102.0, 0.5, "sell"),
    ]
    for t in trades:
        assert b.on_trade(t) == []
    f = b.forming
    assert (f.open, f.high, f.low, f.close) == (100.0, 105.0, 95.0, 102.0)
    assert f.volume == 6.5
    assert f.buy_vol == 5.0 and f.sell_vol == 1.5
    assert f.trade_count == 4
    # vwap = (100*2 + 105*1 + 95*3 + 102*0.5) / 6.5
    assert abs(f.vwap - (200 + 105 + 285 + 51) / 6.5) < 1e-12


def test_close_on_boundary_and_empty_gap_fill():
    b = CandleBuilder("1h")
    b.on_trade(mk_trade(3600.0, 100.0))
    closed = b.on_trade(mk_trade(3 * 3600 + 10.0, 110.0))  # skips hour 2
    assert len(closed) == 2
    real, empty = closed
    assert real.close == 100.0 and real.trade_count == 1
    assert empty.trade_count == 0
    assert empty.open == empty.close == 100.0  # carried prev close
    assert b.forming.open_ts == 3 * 3600


def test_out_of_order_trade_raises():
    b = CandleBuilder("1h")
    b.on_trade(mk_trade(2 * 3600.0, 100.0))
    with pytest.raises(ValueError, match="already-closed"):
        b.on_trade(mk_trade(3599.0, 99.0))


def test_streaks_and_bottom_third():
    b = CandleBuilder("1h")
    b.on_trade(mk_trade(3600.0, 100.0, 1.0, "buy"))
    b.on_trade(mk_trade(3601.0, 101.0, 1.0, "buy"))
    b.on_trade(mk_trade(3602.0, 102.0, 1.0, "buy"))
    b.on_trade(mk_trade(3603.0, 99.0, 2.0, "sell"))
    f = b.forming
    assert f.max_buy_streak == 3
    assert f.max_sell_streak == 1
    # last trade at 99 with range 99..102: bottom third is <= 100
    assert f.vol_bottom_third == 2.0


def test_flush_and_batch_equivalence():
    trades = [mk_trade(3600.0 + i * 60, 100.0 + i, 1.0,
                       "buy" if i % 2 else "sell", tid=i + 1)
              for i in range(120)]  # spans 2 hours
    batch = candles_from_trades(trades, "1h")
    b = CandleBuilder("1h")
    inc = []
    for t in trades:
        inc.extend(b.on_trade(t))
    last = b.flush()
    if last:
        inc.append(last)
    assert batch == inc
    assert len(batch) == 2
