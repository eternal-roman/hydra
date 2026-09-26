"""LiveExecutor smoke tests \u2014 Phase 3.

Exercises the LiveExecutor in isolation using a stub KrakenCLI. Does
NOT hit the real Kraken API.
"""
import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.config import live_execution_enabled
from hydra_companions.executor import (
    TradeProposal, LadderProposal, LadderRung, ProposalValidator,
)
from hydra_companions.live_executor import LiveExecutor, _proposal_userref
from hydra_companions.router import Router
from hydra_engine import HydraEngine


@pytest.fixture(autouse=True)
def _live_execution_on(monkeypatch):
    """v2.27.6: LiveExecutor re-checks live_execution_enabled() before place."""
    monkeypatch.setenv("HYDRA_COMPANION_LIVE_EXECUTION", "1")
    monkeypatch.delenv("HYDRA_COMPANION_DISABLED", raising=False)
    monkeypatch.delenv("HYDRA_COMPANION_PROPOSALS_ENABLED", raising=False)


class StubCLI:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.buys = []
        self.sells = []

    def order_buy(self, **kw):
        self.buys.append(kw)
        if self.fail:
            return {"error": "simulated failure"}
        return {"txid": ["OQ1234-ABCDE"], "descr": {"order": "buy"}}

    def order_sell(self, **kw):
        self.sells.append(kw)
        if self.fail:
            return {"error": "simulated failure"}
        return {"txid": ["OQ5678-FGHIJ"], "descr": {"order": "sell"}}


class StubBroadcaster:
    def __init__(self):
        self.msgs = []

    def broadcast_message(self, t, p):
        self.msgs.append((t, p))


class StubAgent:
    def __init__(self, *, fail=False):
        self.kraken_cli = StubCLI(fail=fail)
        self.broadcaster = StubBroadcaster()


class StubCoord:
    class _R:
        def safety_cap(self, cid, key, default=None):
            return {"max_trades_per_day": 6}.get(key, default)
    router = _R()
    _daily_trades = {}
    ladder_watcher = None


def _p():
    return TradeProposal(
        proposal_id="prop-abc", companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", size=0.1, limit_price=141.0,
        stop_loss=139.0, rationale="",
    )


def test_userref_is_stable_and_positive():
    u1 = _proposal_userref("prop-abc")
    u2 = _proposal_userref("prop-abc")
    assert u1 == u2
    assert u1 > 0


def test_userref_differs_per_rung():
    assert _proposal_userref("prop-x", 0) != _proposal_userref("prop-x", 1)


def test_trade_placement_broadcasts_executed():
    agent = StubAgent()
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    r = ex.execute_trade(_p())
    assert r["ok"]
    assert len(agent.kraken_cli.buys) == 1
    # userref flowed through
    assert agent.kraken_cli.buys[0]["userref"] == r["userref"]
    # broadcast signalled placement
    types = [t for t, _ in agent.broadcaster.msgs]
    assert "companion.trade.executed" in types


def test_trade_failure_broadcasts_failed():
    agent = StubAgent(fail=True)
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    r = ex.execute_trade(_p())
    assert not r["ok"]
    types = [t for t, _ in agent.broadcaster.msgs]
    assert "companion.trade.failed" in types


def test_ladder_places_each_rung_with_distinct_userref():
    agent = StubAgent()
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    p = LadderProposal(
        proposal_id="ladr-xyz", companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", total_size=0.3,
        rungs=(LadderRung(0.5, 141.0), LadderRung(0.5, 139.5)),
        stop_loss=138.0, invalidation_price=138.5, rationale="",
    )
    r = ex.execute_ladder(p)
    assert r["ok"]
    userrefs = [b["userref"] for b in agent.kraken_cli.buys]
    assert len(userrefs) == 2
    assert len(set(userrefs)) == 2


def _ladder():
    return LadderProposal(
        proposal_id="ladr-xyz", companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", total_size=0.3,
        rungs=(LadderRung(0.5, 141.0), LadderRung(0.5, 139.5)),
        stop_loss=138.0, invalidation_price=138.5, rationale="",
    )


def test_trade_under_cap_is_allowed():
    """Empty daily-trade counter (below cap of 6) does not block a trade."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {}  # instance-level, avoids cross-test pollution
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_trade(_p())
    assert r["ok"]


def test_trade_blocked_when_daily_cap_exceeded():
    """execute_trade refuses when the reservation-inclusive count exceeds the
    cap. v2.26.2: the coordinator reserves the slot (count includes the
    in-flight trade) before dispatching, so the executor backstop fires at
    count > cap — the 7th trade against a cap of 6 sees count 7."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 7}  # cap is 6; 7th reserved
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_trade(_p())
    assert not r["ok"]
    assert r["error"] == "daily cap hit"
    assert agent.kraken_cli.buys == []  # nothing reached the exchange


def test_trade_allowed_at_reservation_inclusive_cap():
    """The cap-th trade (count == cap with its own reservation included)
    must NOT bounce off the executor backstop."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 6}  # 6th trade, cap 6
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_trade(_p())
    assert r["ok"]


def test_ladder_blocked_when_daily_cap_exceeded():
    """execute_ladder enforces the same per-companion daily cap as the final
    pre-exchange gate (symmetry with execute_trade). Over the
    reservation-inclusive cap it places no rungs and broadcasts a failure.
    Guards the audit-2026-05-28 finding that the ladder path lacked the
    executor-level cap gate that trades had."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 7}  # cap is 6; 7th reserved
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_ladder(_ladder())
    assert not r["ok"]
    assert r["error"] == "daily cap hit"
    assert agent.kraken_cli.buys == []  # no rung reached the exchange
    types = [t for t, _ in agent.broadcaster.msgs]
    assert "companion.trade.failed" in types


class StubCLIFailSecondRung(StubCLI):
    """Rung 0 places fine; rung 1 is rejected. cancel_order mirrors the real
    KrakenCLI signature (positional *txids only) so a keyword-arg regression
    fails here the way it would in production."""
    def __init__(self):
        super().__init__()
        self.cancels = []

    def order_buy(self, **kw):
        self.buys.append(kw)
        if len(self.buys) >= 2:
            return {"error": "EOrder:Post only order"}
        return {"txid": [f"OQ-RUNG{len(self.buys) - 1}"], "descr": {"order": "buy"}}

    def cancel_order(self, *txids):
        self.cancels.append(txids)
        return {"count": len(txids)}


def test_ladder_mid_failure_cancels_placed_rungs():
    """v2.26.2: a rung-1 placement failure must roll back rung 0 instead of
    leaving it live on Kraken behind an ok:False reply."""
    agent = StubAgent()
    agent.kraken_cli = StubCLIFailSecondRung()
    coord = StubCoord()
    coord._daily_trades = {}
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_ladder(_ladder())
    assert not r["ok"]
    # rung 0's txid was cancelled, positionally
    assert agent.kraken_cli.cancels == [("OQ-RUNG0",)]
    assert r["cancelled_userrefs"] == [_proposal_userref("ladr-xyz", 0)]
    assert r["placed_rungs"][0]["status"] == "cancelled"
    types = [t for t, _ in agent.broadcaster.msgs]
    assert "companion.trade.failed" in types


def _sell(size=0.1):
    return TradeProposal(
        proposal_id="prop-sell", companion_id="apex", user_id="local",
        pair="SOL/USDC", side="sell", size=size, limit_price=141.0,
        stop_loss=148.0, rationale="",
    )


def _engine(**kwargs):
    eng = HydraEngine(initial_balance=1000.0, asset="SOL/USDC")
    eng.exit_only = kwargs.get("exit_only", False)
    eng.tradable = kwargs.get("tradable", True)
    eng.position.size = kwargs.get("size", 0.0)
    return eng


def test_live_flag_defaults_off_and_places_nothing():
    """HYDRA_COMPANION_LIVE_EXECUTION stays off unless it is exactly \"1\"."""
    prev = os.environ.get("HYDRA_COMPANION_LIVE_EXECUTION")
    prev_dis = os.environ.get("HYDRA_COMPANION_DISABLED")
    prev_prop = os.environ.get("HYDRA_COMPANION_PROPOSALS_ENABLED")
    try:
        os.environ.pop("HYDRA_COMPANION_LIVE_EXECUTION", None)
        os.environ.pop("HYDRA_COMPANION_DISABLED", None)
        os.environ.pop("HYDRA_COMPANION_PROPOSALS_ENABLED", None)
        assert live_execution_enabled() is False
        agent = StubAgent()
        ex = LiveExecutor(agent=agent, coordinator=StubCoord())
        r = ex.execute_trade(_p())
        assert r["ok"] is False
        assert r["error"] == "live execution disabled"
        assert agent.kraken_cli.buys == []
        assert agent.kraken_cli.sells == []
    finally:
        def _restore(key, val):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        _restore("HYDRA_COMPANION_LIVE_EXECUTION", prev)
        _restore("HYDRA_COMPANION_DISABLED", prev_dis)
        _restore("HYDRA_COMPANION_PROPOSALS_ENABLED", prev_prop)


def test_buy_rejected_before_order_when_exit_only_or_not_tradable():
    agent = StubAgent()
    agent.engines = {"SOL/USDC": _engine(exit_only=True)}
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    r = ex.execute_trade(_p())
    assert r["ok"] is False
    assert "exit_only" in r["error"]
    assert agent.kraken_cli.buys == []
    assert agent.kraken_cli.sells == []

    agent.engines = {"SOL/USDC": _engine(tradable=False)}
    r = ex.execute_trade(_p())
    assert r["ok"] is False
    assert "tradable" in r["error"]
    assert agent.kraken_cli.buys == []


def test_sell_over_position_rejected_before_order_sell():
    agent = StubAgent()
    agent.engines = {"SOL/USDC": _engine(size=0.05)}
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    r = ex.execute_trade(_sell(0.2))
    assert r["ok"] is False
    assert "exceeds" in r["error"]
    assert agent.kraken_cli.sells == []
    assert agent.kraken_cli.buys == []

    # A sell inside inventory still reaches the exchange as a limit, not a market.
    agent.engines = {"SOL/USDC": _engine(size=0.2, tradable=False)}
    r = ex.execute_trade(_sell(0.1))
    assert r["ok"] is True, r
    assert len(agent.kraken_cli.sells) == 1
    assert agent.kraken_cli.sells[0]["order_type"] == "limit"
    assert agent.kraken_cli.sells[0]["post_only"] is True
    assert agent.kraken_cli.buys == []


def test_ladder_buy_rejected_before_any_rung_when_exit_only():
    agent = StubAgent()
    agent.engines = {"SOL/USDC": _engine(exit_only=True)}
    ex = LiveExecutor(agent=agent, coordinator=StubCoord())
    r = ex.execute_ladder(_ladder())
    assert r["ok"] is False
    assert "exit_only" in r["error"]
    assert agent.kraken_cli.buys == []


def test_validate_trade_rejects_exit_only_untradable_and_oversell():
    agent = StubAgent()
    agent.engines = {"SOL/USDC": _engine(exit_only=True)}
    v = ProposalValidator(agent=agent, router=Router())
    buy = v.validate_trade(_p())
    assert not buy.ok
    assert "exit_only" in buy.reason

    agent.engines = {"SOL/USDC": _engine(tradable=False)}
    blocked = v.validate_trade(_p())
    assert not blocked.ok
    assert "tradable" in blocked.reason

    agent.engines = {"SOL/USDC": _engine(size=0.05)}
    sell = v.validate_trade(_sell(0.2))
    assert not sell.ok
    assert "exceeds" in sell.reason


def test_ladder_allowed_when_under_daily_cap():
    """Under the cap, all rungs are placed (cap gate must not over-block)."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 5}  # under cap of 6
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_ladder(_ladder())
    assert r["ok"]
    assert len(agent.kraken_cli.buys) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all live executor tests passed")
