"""LadderWatcher tests \u2014 Phase 4."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.ladder_watcher import LadderWatcher, ActiveLadder
from hydra_companions.executor import LadderProposal, LadderRung, new_ladder_id


class StubBroadcaster:
    def __init__(self, state):
        self.latest_state = state
        self.msgs = []

    def broadcast_message(self, t, p):
        self.msgs.append((t, p))


class StubCLI:
    """Mirrors the real KrakenCLI.cancel_order signature (positional *txids
    only) so the kwargs-call regression fixed in v2.26.2 fails here the way
    it did in production."""
    def __init__(self, *, fail=False):
        self.fail = fail
        self.cancels = []

    def cancel_order(self, *txids):
        self.cancels.append(txids)
        if self.fail:
            return {"error": "EOrder:Unknown order"}
        return {"count": len(txids)}


class StubAgent:
    def __init__(self, bc, cli=None):
        self.broadcaster = bc
        self.kraken_cli = cli if cli is not None else StubCLI()


def _ladder(side="buy"):
    return LadderProposal(
        proposal_id=new_ladder_id(), companion_id="broski", user_id="local",
        pair="SOL/USDC", side=side, total_size=0.2,
        rungs=(LadderRung(0.5, 141.0), LadderRung(0.5, 140.0)),
        stop_loss=138.0, invalidation_price=138.5, rationale="",
    )


def _placed_rungs():
    return [
        {"idx": 0, "userref": 111, "size": 0.1, "limit_price": 141.0, "status": "placed", "txid": "TX-1"},
        {"idx": 1, "userref": 222, "size": 0.1, "limit_price": 140.0, "status": "placed", "txid": "TX-2"},
    ]


def test_register_and_count():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 140.5}}})
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    w.register(_ladder(), _placed_rungs(), autostart=False)
    assert w.active_count() == 1


def test_no_invalidation_when_price_above():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 140.0}}})  # still above inv=138.5
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    w.register(_ladder(), _placed_rungs(), autostart=False)
    w._tick()
    assert not any(t == "companion.ladder.invalidation_triggered" for t, _ in bc.msgs)


def test_invalidation_fires_on_breach_buy():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 138.0}}})  # below inv=138.5
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    w.register(_ladder("buy"), _placed_rungs(), autostart=False)
    w._tick()
    types = [t for t, _ in bc.msgs]
    assert "companion.ladder.invalidation_triggered" in types


def test_invalidation_fires_on_breach_sell():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 139.0}}})  # above inv=138.5
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    w.register(_ladder("sell"), _placed_rungs(), autostart=False)
    w._tick()
    types = [t for t, _ in bc.msgs]
    assert "companion.ladder.invalidation_triggered" in types


def test_does_not_fire_twice():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 137.0}}})
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    w.register(_ladder(), _placed_rungs(), autostart=False)
    w._tick()
    w._tick()
    count = sum(1 for t, _ in bc.msgs if t == "companion.ladder.invalidation_triggered")
    assert count == 1


def test_mark_fill_excludes_from_cancel():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 137.0}}})
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    lad = _ladder()
    w.register(lad, _placed_rungs(), autostart=False)
    w.mark_fill(lad.proposal_id, 0)  # first rung was filled
    w._tick()
    msgs = [p for t, p in bc.msgs if t == "companion.ladder.invalidation_triggered"]
    assert msgs
    # only userref for unfilled rung (222) is listed
    assert 222 in msgs[0]["cancelled_userrefs"]
    assert 111 not in msgs[0]["cancelled_userrefs"]


def test_invalidation_cancels_by_positional_txid():
    """v2.26.2 regression: cancel_order was called with userref=/txid= kwargs,
    raising TypeError on every rung (swallowed), so no order was ever
    cancelled. Cancels must reach the CLI as positional txids."""
    cli = StubCLI()
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 137.0}}})
    w = LadderWatcher(agent=StubAgent(bc, cli=cli), broadcaster=bc)
    w.register(_ladder(), _placed_rungs(), autostart=False)
    w._tick()
    assert cli.cancels == [("TX-1",), ("TX-2",)]
    msgs = [p for t, p in bc.msgs if t == "companion.ladder.invalidation_triggered"]
    assert msgs[0]["cancelled_userrefs"] == [111, 222]


def test_invalidation_reports_only_acknowledged_cancels():
    """If the exchange rejects the cancel, the userref must NOT be reported
    as cancelled — the pre-fix code claimed success unconditionally."""
    cli = StubCLI(fail=True)
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 137.0}}})
    w = LadderWatcher(agent=StubAgent(bc, cli=cli), broadcaster=bc)
    w.register(_ladder(), _placed_rungs(), autostart=False)
    w._tick()
    assert len(cli.cancels) == 2  # both attempted
    msgs = [p for t, p in bc.msgs if t == "companion.ladder.invalidation_triggered"]
    assert msgs[0]["cancelled_userrefs"] == []


def test_deregister_removes_ladder():
    bc = StubBroadcaster({"pairs": {"SOL/USDC": {"price": 140.0}}})
    w = LadderWatcher(agent=StubAgent(bc), broadcaster=bc)
    lad = _ladder()
    w.register(lad, _placed_rungs(), autostart=False)
    w.deregister(lad.proposal_id)
    assert w.active_count() == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all ladder watcher tests passed")
