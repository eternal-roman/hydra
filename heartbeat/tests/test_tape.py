from heartbeat.feed.tape import (AlertKind, TaintRegistry, TapeMonitor,
                                 normalize_trades)
from helpers import mk_trade


def test_taint_merge_and_overlap():
    r = TaintRegistry()
    r.add(10, 20)
    r.add(15, 30)   # merges
    r.add(50, 60)
    assert r.ranges() == [(10, 30), (50, 60)]
    assert r.overlaps(25, 26)
    assert r.overlaps(0, 10)      # touching counts
    assert r.overlaps(60, 70)
    assert not r.overlaps(31, 49)
    assert not r.overlaps(61, 100)


def test_monitor_sequence_violation_taints():
    m = TapeMonitor()
    m.observe(mk_trade(100.0, 50000))
    m.observe(mk_trade(101.0, 50001))
    m.observe(mk_trade(100.5, 50002))  # backwards
    assert any(a.kind is AlertKind.SEQUENCE for a in m.alerts)
    assert m.taint.overlaps(100.5, 101.0)
    # equal timestamps are fine (Kraken same-batch trades)
    m2 = TapeMonitor()
    m2.observe(mk_trade(100.0, 1))
    m2.observe(mk_trade(100.0, 2))
    assert not m2.alerts


def test_monitor_clock_skew():
    m = TapeMonitor(clock_skew_alert_s=2.0)
    m.observe(mk_trade(100.0, 50000), local_ts=101.0)   # 1s ok
    assert not m.alerts
    m.observe(mk_trade(200.0, 50000), local_ts=203.5)   # 3.5s skew
    assert any(a.kind is AlertKind.CLOCK_SKEW for a in m.alerts)
    assert m.taint.overlaps(200.0, 200.0)
    assert m.max_skew_s == 3.5


def test_gap_marking():
    m = TapeMonitor()
    m.mark_gap(100, 200, "ws drop", backfilled=False)
    assert m.gap_count == 1
    assert m.taint.overlaps(150, 150)
    m.mark_gap(300, 400, "ws drop", backfilled=True)
    assert m.gap_count == 2
    assert not m.taint.overlaps(350, 350)  # complete backfill: no taint


def test_normalize_trades_dedup_and_order():
    a = mk_trade(2.0, 10, tid=2)
    b = mk_trade(1.0, 11, tid=1)
    dup = mk_trade(1.0, 11, tid=1)
    out = list(normalize_trades([a, b, dup]))
    assert out == [b, a]
