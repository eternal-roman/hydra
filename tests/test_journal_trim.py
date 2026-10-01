"""The in-memory journal cap must never evict a working order.

Regression: a tail slice at ORDER_JOURNAL_CAP counted session-only
PLACEMENT_FAILED rows, so a failure loop evicted real fills (then deleted from
disk by the next rolling write) and could evict a PLACED row, after which its
terminal event was dropped and the engine kept a phantom position.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import HydraAgent


def _row(state, tag):
    return {"placed_at": tag, "pair": "BTC/USD", "lifecycle": {"state": state}}


def _agent(rows, cap):
    a = HydraAgent.__new__(HydraAgent)
    a.ORDER_JOURNAL_CAP = cap
    a.order_journal = list(rows)
    return a


def test_failures_go_first_and_placed_rows_survive():
    rows = ([_row("PLACED", "0-working")]
            + [_row("FILLED", f"1-fill-{i}") for i in range(3)]
            + [_row("PLACEMENT_FAILED", f"2-fail-{i}") for i in range(5)])
    a = _agent(rows, cap=4)
    a._trim_journal()
    tags = [r["placed_at"] for r in a.order_journal]
    assert tags == ["0-working", "1-fill-0", "1-fill-1", "1-fill-2"]


def test_oldest_terminal_rows_go_after_failures_but_never_placed():
    rows = ([_row("PLACED", "0-working")]
            + [_row("FILLED", f"1-fill-{i}") for i in range(4)])
    a = _agent(rows, cap=2)
    a._trim_journal()
    tags = [r["placed_at"] for r in a.order_journal]
    assert tags == ["0-working", "1-fill-3"]


def test_under_cap_is_untouched():
    rows = [_row("FILLED", "a"), _row("PLACEMENT_FAILED", "b")]
    a = _agent(rows, cap=5)
    a._trim_journal()
    assert a.order_journal == rows
