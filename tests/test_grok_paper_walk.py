"""Causal Grok paper walk: no same-bar fill, no future bars in the packet."""
import os

from tools.grok_paper_walk import completed_daily, score_window, walk_book


def _bar(ts, close, low=None, high=None):
    return {
        "timestamp": ts,
        "open": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 10.0,
    }


def _series():
    base = 1_700_000_000
    bars = []
    for i in range(40):
        px = 100.0 if i % 2 == 0 else 101.0
        bars.append(_bar(base + i * 3600, px))
    # Scored hours. The 101 and 111 bars are the next-bar fill tape.
    scored = [
        _bar(base + 40 * 3600, 100.0),
        _bar(base + 41 * 3600, 101.0, low=99.0, high=103.0),
        _bar(base + 42 * 3600, 110.0),
        _bar(base + 43 * 3600, 111.0, low=108.0, high=112.0),
    ]
    bars.extend(scored)
    start = base + 40 * 3600
    end = base + 44 * 3600
    return {"BTC/USD": bars}, start, end


def test_score_window_keeps_a_tail_for_the_last_fill():
    stamps = [i * 3600 for i in range(100)]
    start, end = score_window(stamps, score_hours=24, skip_last_hours=6)
    assert end == stamps[-1] - 5 * 3600
    assert start == end - 24 * 3600
    included = [t for t in stamps if start <= t < end]
    assert len(included) == 24
    assert stamps[-1] not in included


def test_completed_daily_excludes_the_open_day():
    asof = 1_700_000_000.0
    daily = [
        _bar(asof - 2 * 86400, 10.0),
        _bar(asof - 86400, 11.0),
        _bar(asof, 12.0),
    ]
    kept = completed_daily(daily, asof)
    assert [row["close"] for row in kept] == [10.0, 11.0]


def test_opinion_sees_only_the_current_hour_and_fill_is_next_bar():
    bars, start, end = _series()
    seen = []

    def decider(ts, view):
        row = view["BTC/USD"]
        seen.append((ts, row["price"], row["history_through"]))
        assert row["history_through"] == ts
        assert all(t <= ts for t in row["prior_times"])
        if row["price"] == 100.0:
            return {"BTC/USD": {
                "action": "BUY", "confidence": 0.85,
                "size_multiplier": 1.0, "reason": "buy the test hour",
            }}
        if row["price"] == 110.0:
            return {"BTC/USD": {
                "action": "SELL", "confidence": 0.85,
                "size_multiplier": 1.0, "reason": "sell the test hour",
            }}
        return {"BTC/USD": {
            "action": "HOLD", "confidence": 0.5,
            "size_multiplier": 1.0, "reason": "flat",
        }}

    old = os.environ.get("HYDRA_FRICTION_GATE_DISABLED")
    os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"
    try:
        report = walk_book(
            bars, {}, None, 10_000.0, start, end,
            decider=decider, fill_model="optimistic", hold_through=False,
        )
    finally:
        if old is None:
            os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)
        else:
            os.environ["HYDRA_FRICTION_GATE_DISABLED"] = old

    assert [px for _ts, px, _hist in seen] == [100.0, 101.0, 110.0, 111.0]
    assert report["status"] == "complete"
    assert len(report["closed_trades"]) == 1
    trade = report["closed_trades"][0]
    assert trade["entry_price"] == 100.0
    assert trade["exit_price"] == 110.0
    assert trade["fill_ts"] > trade["decision_ts"]
    assert trade["exit_fill_ts"] > trade["exit_decision_ts"]
    assert trade["net_usd"] < trade["gross_usd"]
    assert trade["net_usd"] > 0
    assert report["before_usd"] == 10_000.0
    assert report["after_usd"] > report["before_usd"]
    assert abs(
        report["accumulated_roi"] - (report["after_usd"] - report["before_usd"]) / report["before_usd"]
    ) < 1e-12
    assert abs(report["net_usd"] - (report["after_usd"] - report["before_usd"])) < 1e-8


def test_same_bar_cannot_fill_and_a_miss_does_not_change_cash():
    bars, start, _end = _series()
    # One decision hour, then a next bar that never trades down to the bid.
    base_bars = [b for b in bars["BTC/USD"] if b["timestamp"] <= start]
    nxt = dict(base_bars[-1])
    nxt["timestamp"] = start + 3600
    nxt["close"] = 102.0
    nxt["low"] = 101.0
    nxt["high"] = 103.0
    base_bars.append(nxt)
    end = start + 3600

    def decider(ts, view):
        return {"BTC/USD": {
            "action": "BUY", "confidence": 0.85,
            "size_multiplier": 1.0, "reason": "buy",
        }}

    old = os.environ.get("HYDRA_FRICTION_GATE_DISABLED")
    os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"
    try:
        report = walk_book(
            {"BTC/USD": base_bars}, {}, None, 10_000.0, start, end,
            decider=decider, fill_model="optimistic", hold_through=False,
        )
    finally:
        if old is None:
            os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)
        else:
            os.environ["HYDRA_FRICTION_GATE_DISABLED"] = old

    assert report["closed_trades"] == []
    assert report["open_marks"] == []
    assert report["after_usd"] == report["before_usd"]
    assert any(row["status"] == "rejected" for row in report["intents"])


def test_missing_opinion_stops_before_using_a_later_hour():
    bars, start, end = _series()
    report = walk_book(
        bars, {}, {}, 10_000.0, start, end, fill_model="optimistic", hold_through=False,
    )
    assert report["status"] == "need_decision"
    assert report["timestamp"] == start
    assert report["pairs"]["BTC/USD"]["price"] == 100.0
    assert report["pairs"]["BTC/USD"]["history_through"] == start
