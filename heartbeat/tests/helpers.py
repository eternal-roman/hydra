"""Shared test fixtures/builders."""

from __future__ import annotations

from heartbeat.engine.candle import ClosedCandle
from heartbeat.feed.tape import Side, Trade


def mk_candle(open_ts=0.0, tf=3600, o=100.0, h=101.0, l=99.0, c=100.5,
              vol=10.0, buy=6.0, sell=4.0, n=20, vwap=None, **kw) -> ClosedCandle:
    return ClosedCandle(
        open_ts=open_ts, close_ts=open_ts + tf, open=o, high=h, low=l,
        close=c, volume=vol, buy_vol=buy, sell_vol=sell, trade_count=n,
        vwap=vwap if vwap is not None else c, **kw)


def mk_trade(ts: float, price: float, qty: float = 1.0,
             side: str = "buy", tid: int = 0) -> Trade:
    return Trade(ts=ts, price=price, qty=qty,
                 side=Side.BUY if side == "buy" else Side.SELL,
                 trade_id=tid)


def base_config(**over) -> dict:
    cfg = {
        "pair": "BTC/USD", "timeframe": "1h",
        "decay": {"memory_candles": 30},
        "heartbeat": {"micro_bucket_ms": 500, "bucket_rate_threshold": 20.0,
                      "default_heartbeats_per_candle": 60},
        "features": {"enabled_tiers": [0], "overrides": {}, "weights": {},
                     "default_weight": 0.5},
        "scaling": {"window_candles": 500, "clip_mads": 3.0, "min_history": 30},
        "atr": {"period": 14, "outlier_mult": 3.0},
        "vol_z": {"window": 96},
        "labeler": {"ma_period": 9, "swing_window": 2, "down_leg_lookback": 30,
                    "bounce_atr": 1.0, "reversal_atr": 3.3,
                    "crash_range_atr": 3.0, "horizon_candles": 200,
                    "min_events": 60},
        "eval": {"auc_promote": 0.70, "checkpoints": [1, 2, 3]},
        "calibrate": {"l2": 1.0, "lr": 0.05, "iters": 2000,
                      "walk_forward_folds": 4, "min_train_events": 20},
        "store": {"root": "data"},
        "feed": {"ws_url": "wss://ws.kraken.com/v2",
                 "rest_url": "https://api.kraken.com",
                 "rest_rate_per_s": 100.0, "rest_burst": 10,
                 "clock_skew_alert_s": 2.0,
                 "reconnect_base_s": 0.01, "reconnect_max_s": 0.05},
        "api": {"status_file": "data/heartbeat_status.json",
                "tcp_host": "127.0.0.1", "tcp_port": 8790},
    }
    from heartbeat.config import deep_merge
    return deep_merge(cfg, over)
