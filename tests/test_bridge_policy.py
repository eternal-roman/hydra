"""Bridge (SOL/BTC) signal-only default + engine exit_only drain mode.

Isolation study on real 1h tape (fees-on, realistic fills): the bridge
produced zero trades in 1y, and its only 2y trade lost money while dragging
portfolio Sharpe from -0.63 to -0.99. Default policy is therefore
signal-only with a drain path for existing inventory; HYDRA_BRIDGE_TRADING=1
opts back in. Evidence: .hydra-flywheel/bridge_isolation.json.
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent
from hydra_engine import HydraEngine


def _uptrend_engine(**kw) -> HydraEngine:
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/BTC", **kw)
    px = 0.0012
    for i in range(60):
        px *= 1.004
        eng.ingest_candle({
            "open": px * 0.999, "high": px * 1.002, "low": px * 0.997,
            "close": px, "volume": 100.0, "timestamp": 1700000000 + i * 3600,
        })
    return eng


# ─── engine exit_only mechanics ────────────────────────────────

def test_exit_only_refuses_buy():
    eng = _uptrend_engine(hold_through=False)
    eng.exit_only = True
    trade = eng.execute_signal("BUY", 0.9, "entry attempt", "MOMENTUM")
    assert trade is None


def test_exit_only_allows_sell():
    eng = _uptrend_engine(hold_through=False)
    eng.exit_only = True
    eng.position.size = 1.0
    eng.position.avg_entry = 0.001
    trade = eng.execute_signal("SELL", 0.9, "drain", "DEFENSIVE")
    assert trade is not None and trade.action == "SELL"
    assert eng.position.size == 0.0


def test_exit_only_default_off():
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.exit_only is False


def test_exit_only_does_not_block_hold_through_flatten():
    """Drain mode + hold-through force-flatten must compose: the flatten
    SELL flows through exit_only untouched."""
    eng = _uptrend_engine(hold_through=True)
    eng.exit_only = True
    eng.position.size = 1.0
    eng.position.avg_entry = 0.001
    # Downtrend history so rails force-flatten
    px = eng.prices[-1]
    for i in range(60):
        px *= 0.994
        eng.ingest_candle({
            "open": px * 1.001, "high": px * 1.003, "low": px * 0.997,
            "close": px, "volume": 100.0,
            "timestamp": 1700300000 + i * 3600,
        })
    state = eng.tick()
    assert eng.position.size == 0.0, state["signal"]


# ─── agent bridge policy ───────────────────────────────────────

def _agent_with_bridge(paper: bool, position: float = 0.0):
    agent = object.__new__(HydraAgent)
    agent.paper = paper
    eng = _uptrend_engine()
    eng.position.size = position
    eng.position.avg_entry = 0.001 if position else 0.0
    agent.engines = {"SOL/BTC": eng}
    agent.pairs = ["SOL/BTC"]
    return agent, eng


def test_bridge_seeds_signal_only_by_default(monkeypatch):
    monkeypatch.delenv("HYDRA_BRIDGE_TRADING", raising=False)
    agent, eng = _agent_with_bridge(paper=False)
    agent._set_engine_balances(100.0)
    assert eng.exit_only is True
    assert eng.tradable is False
    assert eng.balance == 0.0


def test_bridge_with_position_drains_not_strands(monkeypatch):
    """A held bridge position must remain sellable (tradable + exit_only),
    never frozen by the signal-only default. In TREND_UP the ride rail may
    defer the exit (riding a winner is intended even while draining), so
    drive the drain from a RANGING regime where rails pass SELLs through."""
    monkeypatch.delenv("HYDRA_BRIDGE_TRADING", raising=False)
    agent = object.__new__(HydraAgent)
    agent.paper = False
    # Pure ranging tape from scratch so hold-through does not ride the exit.
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/BTC")
    px = 0.0012
    for i in range(80):
        close = px * (1.0 + (0.002 if i % 2 else -0.002))
        eng.ingest_candle({
            "open": px, "high": px * 1.003, "low": px * 0.997,
            "close": close, "volume": 100.0,
            "timestamp": 1700300000 + i * 3600,
        })
    eng.position.size = 0.5
    eng.position.avg_entry = 0.001
    agent.engines = {"SOL/BTC": eng}
    agent.pairs = ["SOL/BTC"]
    agent._set_engine_balances(100.0)
    assert eng.exit_only is True
    assert eng.tradable is True  # exit path open
    trade = eng.execute_signal("SELL", 0.9, "drain", "DEFENSIVE")
    assert trade is not None and eng.position.size == 0.0
    # And no new entry can open
    assert eng.execute_signal("BUY", 0.9, "re-entry", "MOMENTUM") is None


def test_bridge_optin_restores_trading(monkeypatch):
    monkeypatch.setenv("HYDRA_BRIDGE_TRADING", "1")
    agent, eng = _agent_with_bridge(paper=True)
    agent._get_asset_prices = lambda: {"BTC": 80000.0}
    agent._set_engine_balances(100.0)
    assert eng.exit_only is False
    assert eng.tradable is True


def test_refresh_deactivates_flat_bridge(monkeypatch):
    monkeypatch.delenv("HYDRA_BRIDGE_TRADING", raising=False)
    agent, eng = _agent_with_bridge(paper=False, position=0.5)
    agent._set_engine_balances(100.0)
    assert eng.tradable is True
    eng.position.size = 0.0  # drained
    agent._refresh_tradable_flags()
    assert eng.tradable is False
    assert eng.exit_only is True


def test_refresh_never_reactivates_bridge_on_btc_balance(monkeypatch):
    """BTC landing in the account must not re-arm bridge entries."""
    monkeypatch.delenv("HYDRA_BRIDGE_TRADING", raising=False)
    agent, eng = _agent_with_bridge(paper=False)
    agent._set_engine_balances(100.0)
    agent._get_real_quote_balance = lambda q: 1.0  # plenty of BTC
    agent._refresh_tradable_flags()
    assert eng.tradable is False
    assert eng.exit_only is True
