"""Hand-computed fixtures for every Tier 0 feature (Phase 2 gate)."""

from heartbeat.engine.candle import FormingCandle
from heartbeat.features.registry import FeatureContext
from heartbeat.features.tier0 import (clv, ofi, ofi_momentum, range_atr,
                                      robust_atr, vol_z)
from helpers import base_config, mk_candle


def _forming(o=100.0, h=110.0, l=90.0, c=105.0, buy=6.0, sell=4.0, n=10,
             vol=None, last_ts=7200.0, open_ts=3600.0):
    f = FormingCandle(open_ts=open_ts, tf_s=3600)
    f.open, f.high, f.low, f.close = o, h, l, c
    f.buy_vol, f.sell_vol = buy, sell
    f.volume = vol if vol is not None else buy + sell
    f.trade_count = n
    f.vwap_num = f.close * f.volume
    f._last_ts = last_ts
    return f


def _ctx(forming, closed=(), atr=None, cfg=None):
    return FeatureContext(forming=forming, closed=tuple(closed), atr=atr,
                          config=cfg or base_config())


def test_ofi_hand():
    # (6 - 4) / (6 + 4) = 0.2
    assert abs(ofi(_ctx(_forming(buy=6, sell=4))) - 0.2) < 1e-12
    assert ofi(_ctx(_forming(buy=0, sell=0, vol=0))) is None
    assert ofi(_ctx(_forming(buy=10, sell=0))) == 1.0


def test_clv_hand():
    # ((C-L) - (H-C)) / (H-L) = ((105-90) - (110-105)) / 20 = 10/20 = 0.5
    assert abs(clv(_ctx(_forming())) - 0.5) < 1e-12
    # close at high -> +1; close at low -> -1
    assert clv(_ctx(_forming(c=110.0))) == 1.0
    assert clv(_ctx(_forming(c=90.0))) == -1.0
    # zero range -> 0
    assert clv(_ctx(_forming(h=100, l=100, c=100))) == 0.0


def test_robust_atr_hand_and_outlier_drop():
    # 15 candles, constant true range 2.0 except one 50.0 spike
    candles = [mk_candle(open_ts=i * 3600, o=100, h=101, l=99, c=100)
               for i in range(15)]
    atr = robust_atr(candles, period=14)
    assert abs(atr - 2.0) < 1e-12
    spike = mk_candle(open_ts=15 * 3600, o=100, h=150, l=100, c=100)
    atr2 = robust_atr(candles + [spike], period=14)
    # spike TR=50 > 3 * median(2) -> dropped; ATR stays 2.0
    assert abs(atr2 - 2.0) < 1e-12
    assert robust_atr(candles[:10], period=14) is None  # insufficient


def test_range_atr_hand():
    candles = [mk_candle(open_ts=i * 3600, h=101, l=99, c=100)
               for i in range(15)]
    f = _forming(h=104.0, l=100.0)  # range 4, atr 2 -> 2.0
    ctx = FeatureContext(forming=f, closed=tuple(candles), atr=2.0,
                         config=base_config())
    assert abs(range_atr(ctx) - 2.0) < 1e-12
    assert range_atr(_ctx(f, candles, atr=None)) is None


def test_vol_z_hand():
    # 96 candles vol=10 except last=20: mean=10.104..., but easier:
    # all 96 at vol 10 -> sd 0 -> None; mix to get known z
    closed = [mk_candle(open_ts=i * 3600, vol=10.0) for i in range(48)] + \
             [mk_candle(open_ts=(48 + i) * 3600, vol=20.0) for i in range(48)]
    # mean=15, var=25, sd=5. forming vol 20 at progress 1.0 -> z = 1.0
    f = _forming(vol=20.0, open_ts=96 * 3600, last_ts=97 * 3600)
    ctx = FeatureContext(forming=f, closed=tuple(closed), atr=None,
                         config=base_config())
    assert abs(vol_z(ctx) - 1.0) < 1e-12
    # progress floor: at 10% elapsed, projection uses 25% floor
    f2 = _forming(vol=5.0, open_ts=96 * 3600, last_ts=96 * 3600 + 360)
    z2 = vol_z(FeatureContext(forming=f2, closed=tuple(closed), atr=None,
                              config=base_config()))
    assert abs(z2 - ((5.0 / 0.25) - 15.0) / 5.0) < 1e-12
    # insufficient history
    assert vol_z(_ctx(f, closed[:50])) is None
    # zero variance -> None
    flat = [mk_candle(open_ts=i * 3600, vol=10.0) for i in range(96)]
    assert vol_z(_ctx(f, flat)) is None


def test_ofi_momentum_hand():
    # closed OFIs: (2-8)/10=-0.6 then (5-5)/10=0. forming: (6-4)/10=0.2
    closed = [mk_candle(open_ts=0, buy=2, sell=8),
              mk_candle(open_ts=3600, buy=5, sell=5)]
    f = _forming(buy=6, sell=4, open_ts=7200)
    ctx = FeatureContext(forming=f, closed=tuple(closed), atr=None,
                         config=base_config())
    # slope = (0.2 - (-0.6)) / 2 = 0.4
    assert abs(ofi_momentum(ctx) - 0.4) < 1e-12
    assert ofi_momentum(_ctx(f, closed[:1])) is None
