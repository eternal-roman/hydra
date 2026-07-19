"""No-lookahead gate (Phase 2): feeding trades incrementally must yield,
at every heartbeat, exactly the posterior a from-scratch replay of the
prefix tape yields. If any feature peeked at the completed candle or any
state depended on future trades, these would diverge."""

from heartbeat.engine.pipeline import HeartbeatPipeline
from heartbeat.synth import SynthSpec, generate_tape
from helpers import base_config


def _collect(cfg, trades):
    pipe = HeartbeatPipeline(cfg, "BTC/USD", "1h")
    outs = []
    pipe.on_heartbeat = lambda out, prog: outs.append(out)
    for t in trades:
        pipe.feed_trade(t)
    return outs


def test_incremental_equals_prefix_replay():
    cfg = base_config()
    trades, _ = generate_tape(SynthSpec(seed=3, days=4))
    full = _collect(cfg, trades)
    assert len(full) > 500

    # Map heartbeat index -> trade index that produced it, by re-running
    # with a counter (micro-bucketing can skip heartbeats for some trades).
    pipe = HeartbeatPipeline(cfg, "BTC/USD", "1h")
    hb_trade_idx = []
    marker = {"i": -1}
    pipe.on_heartbeat = lambda out, prog: hb_trade_idx.append(marker["i"])
    for i, t in enumerate(trades):
        marker["i"] = i
        pipe.feed_trade(t)

    # Cut at several points spread through the tape; replay the prefix from
    # scratch and compare the FINAL heartbeat state bit-for-bit.
    n = len(full)
    for cut_hb in [10, n // 4, n // 2, (3 * n) // 4, n - 1]:
        prefix_trades = trades[:hb_trade_idx[cut_hb] + 1]
        replay = _collect(cfg, prefix_trades)
        a, b = full[cut_hb], replay[-1]
        assert a.ts == b.ts
        assert a.L == b.L, f"lookahead detected at heartbeat {cut_hb}"
        assert a.p_up == b.p_up
        assert a.raw == b.raw
        assert a.z == b.z


def test_appending_future_trades_never_changes_past():
    """The posterior series over a prefix is invariant to what comes after."""
    cfg = base_config()
    trades, _ = generate_tape(SynthSpec(seed=11, days=2))
    half = len(trades) // 2
    first = _collect(cfg, trades[:half])
    full = _collect(cfg, trades)
    assert [o.L for o in full[:len(first)]] == [o.L for o in first]
