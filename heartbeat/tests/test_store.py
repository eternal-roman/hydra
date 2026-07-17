from heartbeat.store import Store
from helpers import mk_trade


def test_tape_roundtrip_sorted_dedup(tmp_path):
    store = Store(tmp_path)
    a = [mk_trade(10.0 + i, 100.0 + i, tid=i + 1) for i in range(5)]
    b = [mk_trade(12.0 + i, 102.0 + i, tid=i + 3) for i in range(5)]  # overlaps a
    store.append_tape("BTC/USD", "1h", a)
    store.append_tape("BTC/USD", "1h", b)
    out = store.read_tape("BTC/USD", "1h")
    assert [t.trade_id for t in out] == [1, 2, 3, 4, 5, 6, 7]
    assert out == sorted(out, key=lambda t: t.sort_key())


def test_tape_range_filter(tmp_path):
    store = Store(tmp_path)
    store.append_tape("BTC/USD", "1h", [mk_trade(float(i), 100.0, tid=i)
                                        for i in range(1, 11)])
    out = store.read_tape("BTC/USD", "1h", ts_start=3.0, ts_end=6.0)
    assert [t.ts for t in out] == [3.0, 4.0, 5.0, 6.0]


def test_posterior_roundtrip_last_write_wins(tmp_path):
    store = Store(tmp_path)
    row = {"ts": 3600.0, "candle_open_ts": 0.0, "open": 1.0, "high": 2.0,
           "low": 0.5, "close": 1.5, "volume": 10.0, "buy_vol": 6.0,
           "sell_vol": 4.0, "trade_count": 7, "vwap": 1.4, "L": 0.2,
           "p_up": 0.55, "tainted": False, "features_json": "{}"}
    store.append_posterior("BTC/USD", "1h", [row])
    row2 = dict(row, p_up=0.6)
    store.append_posterior("BTC/USD", "1h", [row2])
    out = store.read_posterior("BTC/USD", "1h")
    assert len(out) == 1 and out[0]["p_up"] == 0.6


def test_scaler_persistence_atomic(tmp_path):
    store = Store(tmp_path)
    state = {"ofi": {"window": 500, "clip_mads": 3.0, "min_history": 30,
                     "values": [0.1, 0.2]}}
    store.save_scalers("BTC/USD", "1h", state)
    assert store.load_scalers("BTC/USD", "1h") == state
    assert store.load_scalers("ETH/USD", "1h") is None


def test_part_files_never_overwritten(tmp_path):
    store = Store(tmp_path)
    trades = [mk_trade(100.0, 1.0, tid=1)]
    p1 = store.append_tape("BTC/USD", "1h", trades)
    p2 = store.append_tape("BTC/USD", "1h", trades)  # same first ts
    assert p1 != p2 and p1.exists() and p2.exists()
