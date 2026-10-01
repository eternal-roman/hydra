"""A breaker armed only by an unfilled BUY's optimistic book must release
when that BUY ends partially filled, not only when it is fully cancelled.

Regression: release_unfilled_buy_halt() ran on CANCELLED/REJECTED only. A 1%
fill before the cancel left the real book at 14.01% (under 15%) but halted,
and only HYDRA_RESET_CIRCUIT_BREAKER=1 could clear it — no new entries ever.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_agent_tuner_fill as h  # shared agent/journal/event fixtures
from hydra_engine import HydraEngine, Trade


def _armed(monkeypatch, state_name, vol):
    monkeypatch.setenv("HYDRA_TREND_OVERLAY", "0")
    eng = HydraEngine(initial_balance=1000.0, asset=h.PAIR, hold_through=False)
    eng.prices.append(100.0)
    eng.peak_equity = 1000.0 / 0.86          # pre-buy book: 14% drawdown
    agent = h._agent(eng)
    agent.paper = True
    snap = eng.snapshot_position()
    eng.position.size, eng.position.avg_entry = 4.0, 100.0
    eng.balance -= 400.0                      # optimistic BUY booked
    trade = Trade("BUY", h.PAIR, 100.0, 4.0, 400.0, "entry", 0.8, "MOMENTUM",
                  params_at_entry=dict(h.DEFAULT_PARAMS))
    entry = h._journal(trade, snap, "OID-BUY")
    agent.order_journal.append(entry)
    eng.prices.append(97.0)                   # optimistic book crosses 15%
    was = eng.halted
    eng.tick(generate_only=True)
    agent._note_unfilled_buy_halt(h.PAIR, eng, was)
    assert eng.halted and eng._halt_from_unfilled_buy
    agent._apply_execution_event(h._event(
        entry, eng, snap, state=state_name, vol=vol,
        price=100.0 if vol else None, fee=0.0, side="BUY"))
    return eng


def test_full_cancel_releases(monkeypatch):
    eng = _armed(monkeypatch, "CANCELLED_UNFILLED", 0.0)
    assert not eng.halted


def test_partial_fill_releases_when_the_real_book_is_under_15(monkeypatch):
    eng = _armed(monkeypatch, "PARTIALLY_FILLED", 0.04)
    assert eng.position.size == 0.04
    assert eng.current_drawdown_pct() < eng.CIRCUIT_BREAKER_PCT
    assert not eng.halted
