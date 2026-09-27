"""Ledger opinions stay inside the hour they are shown."""
from tools.grok_paper_ledger import ledger_opinion, opinion_for_pair
from tools.grok_paper_walk import walk_book
from tests.test_grok_paper_walk import _series


def _row(**overrides):
    base = {
        "asset": "ETH/USD",
        "regime": "TREND_UP",
        "daily_trend_long": True,
        "price": 2633.0,
        "position": {"size": 0.0},
        "signal": {
            "action": "BUY",
            "confidence": 0.66,
            "reason": "Momentum confirmed: RSI 62.7",
        },
        "indicators": {"rsi": 62.7, "macd_histogram": 6.4, "bb_upper": 2700.0, "bb_lower": 2500.0},
    }
    base.update(overrides)
    return base


def test_confirms_a_trend_buy_under_the_chase_line():
    opinion = opinion_for_pair(_row())
    assert opinion["action"] == "BUY"
    assert opinion["confidence"] == 0.66
    assert abs(
        opinion["quant"]["scenario"]["p_up"]
        + opinion["quant"]["scenario"]["p_flat"]
        + opinion["quant"]["scenario"]["p_down"]
        - 1.0
    ) < 1e-9


def test_does_not_chase_an_engine_buy_at_rsi_68():
    opinion = opinion_for_pair(_row(indicators={
        "rsi": 69.4, "macd_histogram": 6.5, "bb_upper": 1400.0, "bb_lower": 1100.0,
    }, price=1268.0))
    assert opinion["action"] == "HOLD"
    assert "not taken" in opinion["reason"]


def test_sells_an_open_long_when_the_regime_flips():
    opinion = opinion_for_pair(_row(
        regime="TREND_DOWN",
        position={"size": 0.2},
        signal={"action": "SELL", "confidence": 0.7, "reason": "flatten"},
    ))
    assert opinion["action"] == "SELL"


def test_ledger_walk_records_each_scored_hour_and_no_later_close():
    bars, start, end = _series()
    seen = []

    def wrapped(ts, view):
        for row in view.values():
            assert row["history_through"] == ts
        seen.append(ts)
        return ledger_opinion(ts, view)

    report = walk_book(
        bars, {}, None, 10_000.0, start, end,
        decider=wrapped, fill_model="optimistic", hold_through=False,
    )
    assert report["status"] == "complete"
    assert seen == [start + i * 3600 for i in range(4)]
    assert all(t <= seen[-1] for t in seen)
