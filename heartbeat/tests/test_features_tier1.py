"""Hand-computed fixtures for Tier 1 features."""

import math

from heartbeat.engine.candle import FormingCandle
from heartbeat.features.registry import FeatureContext, all_features
from heartbeat.features.tier1 import (aggressor_run, flow_persistence,
                                      size_skew, vwap_dev, wick_absorption)
from helpers import base_config, mk_candle


def _forming(**kw):
    f = FormingCandle(open_ts=3600.0, tf_s=3600)
    f.open, f.high, f.low, f.close = 100.0, 110.0, 90.0, 105.0
    f.trade_count = 10
    f.volume = 10.0
    f.vwap_num = 100.0 * 10.0  # vwap 100
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def _ctx(f, closed=(), atr=None):
    return FeatureContext(forming=f, closed=tuple(closed), atr=atr,
                          config=base_config())


def test_size_skew_hand():
    f = _forming(buy_size_sum=6.0, buy_count=2, sell_size_sum=3.0, sell_count=3)
    # mean buy 3.0, mean sell 1.0 -> log(3)
    assert abs(size_skew(_ctx(f)) - math.log(3.0)) < 1e-12
    assert size_skew(_ctx(_forming(buy_count=0, sell_count=3,
                                   sell_size_sum=3.0))) is None


def test_aggressor_run_signed():
    assert aggressor_run(_ctx(_forming(max_buy_streak=7, max_sell_streak=3))) == 7.0
    assert aggressor_run(_ctx(_forming(max_buy_streak=2, max_sell_streak=5))) == -5.0


def test_vwap_dev_hand():
    f = _forming()  # close 105, vwap 100
    assert abs(vwap_dev(_ctx(f, atr=2.0)) - 2.5) < 1e-12
    assert vwap_dev(_ctx(f, atr=None)) is None


def test_wick_absorption_hand():
    # O=100 C=105 L=90 H=110: lower wick = (100-90)/20 = 0.5
    f = _forming(vol_bottom_third=4.0)
    assert abs(wick_absorption(_ctx(f)) - 2.0) < 1e-12


def test_flow_persistence_hand():
    # alternating flow -> negative autocorr; trending flow -> positive
    alt = [mk_candle(open_ts=i * 3600, buy=(8 if i % 2 else 2),
                     sell=(2 if i % 2 else 8)) for i in range(6)]
    trend = [mk_candle(open_ts=i * 3600, buy=2 + i, sell=8 - i)
             for i in range(6)]
    f = _forming()
    assert flow_persistence(_ctx(f, alt)) < 0
    assert flow_persistence(_ctx(f, trend)) > 0
    assert flow_persistence(_ctx(f, trend[:5])) is None


def test_registry_metadata_complete():
    feats = all_features()
    # every feature declares tier, inputs, lookback, hypothesis
    assert {"ofi", "clv", "range_atr", "vol_z", "ofi_momentum"} <= set(feats)
    assert {"size_skew", "aggressor_run", "vwap_dev", "wick_absorption",
            "flow_persistence"} <= set(feats)
    assert {"book_imbalance", "cancel_asymmetry", "btc_lead",
            "funding_basis"} <= set(feats)
    for f in feats.values():
        assert f.hypothesis and f.inputs and f.tier in (0, 1, 2)
