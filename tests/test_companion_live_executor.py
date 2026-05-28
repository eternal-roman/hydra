"""LiveExecutor smoke tests \u2014 Phase 3.

Exercises the LiveExecutor in isolation using a stub KrakenCLI. Does
NOT hit the real Kraken API.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.executor import TradeProposal, LadderProposal, LadderRung
from hydra_companions.live_executor import LiveExecutor, _proposal_userref


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


def test_trade_blocked_when_daily_cap_reached():
    """execute_trade refuses once the per-companion daily cap is hit."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 6}  # cap is 6
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_trade(_p())
    assert not r["ok"]
    assert r["error"] == "daily cap hit"
    assert agent.kraken_cli.buys == []  # nothing reached the exchange


def test_ladder_blocked_when_daily_cap_reached():
    """execute_ladder enforces the same per-companion daily cap as the final
    pre-exchange gate (symmetry with execute_trade). At/over cap it places no
    rungs and broadcasts a failure. Guards the audit-2026-05-28 finding that
    the ladder path lacked the executor-level cap gate that trades had."""
    agent = StubAgent()
    coord = StubCoord()
    coord._daily_trades = {("local", "apex"): 6}  # cap is 6
    ex = LiveExecutor(agent=agent, coordinator=coord)
    r = ex.execute_ladder(_ladder())
    assert not r["ok"]
    assert r["error"] == "daily cap hit"
    assert agent.kraken_cli.buys == []  # no rung reached the exchange
    types = [t for t, _ in agent.broadcaster.msgs]
    assert "companion.trade.failed" in types


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
