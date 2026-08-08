"""Validator + token + executor contract tests (Phase 2)."""
import sys
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.executor import (
    TradeProposal, LadderProposal, LadderRung, ProposalValidator,
    MockExecutor, new_proposal_id, new_ladder_id,
)
from hydra_companions.router import Router
from hydra_companions.tokens import TokenBroker


class FakeBroadcaster:
    def __init__(self, state=None):
        self.latest_state = state or {
            "pairs": {"SOL/USDC": {"price": 142.0, "portfolio": {"equity": 100.0}}},
            "balance_usd": {"total_usd": 100.0},
        }
        self.msgs = []

    def broadcast_message(self, msg_type, payload):
        self.msgs.append((msg_type, payload))


class FakeAgent:
    def __init__(self, broadcaster=None):
        self.broadcaster = broadcaster or FakeBroadcaster()
        self.kraken_cli = None


def _valid_trade():
    return TradeProposal(
        proposal_id=new_proposal_id(), companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", size=0.1, limit_price=141.0,
        stop_loss=139.0, rationale="test",
    )


def test_valid_trade_passes():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    result = v.validate_trade(_valid_trade())
    assert result.ok, result.reason


def test_missing_stop_rejected():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    p = TradeProposal(**{**_valid_trade().to_dict(), "stop_loss": 0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "stop" in result.reason.lower()


def test_buy_stop_above_entry_rejected():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    p = TradeProposal(**{**_valid_trade().to_dict(), "stop_loss": 145.0})
    assert not v.validate_trade(p).ok


def test_price_band_enforced():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    # Apex cap is 4%; put limit 20% off mid.
    p = TradeProposal(**{**_valid_trade().to_dict(), "limit_price": 170.0, "stop_loss": 168.0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "band" in result.reason.lower()


def test_risk_cap_enforced():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    # 100 equity, 10% risk attempted -> fails apex 1% cap.
    p = TradeProposal(**{**_valid_trade().to_dict(), "size": 5.0, "stop_loss": 139.0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "risk" in result.reason.lower()


def test_broski_higher_risk_cap():
    """Broski's 1.5% per-trade risk cap admits a size apex's 1.0% rejects.

    The stop is deliberately WIDE (141 -> 134). Risk and notional are locked
    in the ratio limit/(limit-stop), so the original 2-wide stop meant a
    1.4%-of-equity risk implied a 98.7%-of-equity position - which the
    notional cap now (correctly) rejects before the risk cap is reached.
    A 7-wide stop separates the two caps so this test measures only risk:
    size 0.2 -> risk 1.4 (1.4% of 100 equity), notional 28.2 (28.2%, under
    the 30% max_position_pct default).
    """
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    base = {**_valid_trade().to_dict(), "stop_loss": 134.0, "size": 0.2}
    mid_size = TradeProposal(**{**base, "companion_id": "apex"})
    apex_result = v.validate_trade(mid_size)
    assert not apex_result.ok
    assert "risk" in apex_result.reason.lower(), apex_result.reason
    bro_size = TradeProposal(**{**base, "companion_id": "broski"})
    assert v.validate_trade(bro_size).ok, v.validate_trade(bro_size).reason


def test_portfolio_drawdown_halt_blocks_companion_buy():
    """The 15% PORTFOLIO breaker must stop a confirmed companion BUY.

    `_portfolio_buy_halted` is a separate flag from `engine.halted`: it arms
    off summed equity, so it fires while every individual engine is still
    under its own threshold and `engine.halted` is False everywhere. The
    validator checked only the engine flags, so a companion BUY walked
    straight through a halted portfolio.
    """
    agent = FakeAgent()
    agent._portfolio_buy_halted = True
    agent._portfolio_max_drawdown_pct = 17.3
    v = ProposalValidator(agent=agent, router=Router())
    result = v.validate_trade(_valid_trade())
    assert not result.ok
    assert "portfolio" in result.reason.lower(), result.reason


def test_portfolio_drawdown_halt_still_allows_companion_sell():
    """SELL/flatten survives the portfolio halt (PR-A exit guarantee)."""
    agent = FakeAgent()
    agent._portfolio_buy_halted = True
    agent._portfolio_max_drawdown_pct = 17.3
    v = ProposalValidator(agent=agent, router=Router())
    # Sell stop sits above entry.
    p = TradeProposal(**{**_valid_trade().to_dict(),
                         "side": "sell", "stop_loss": 148.0})
    assert v.validate_trade(p).ok, v.validate_trade(p).reason


def test_notional_cap_rejects_tight_stop_oversize():
    """A tight stop must not buy an unbounded position.

    The per-trade risk cap alone is not a size cap: |limit - stop| * size is
    small whenever the stop is close, so an arbitrarily large order reads as
    "low risk". Here 0.7 SOL @ 141 with a 2-wide stop is 1.4% risk - inside
    broski's 1.5% cap - but 98.7% of a 100-equity account in a single order.
    """
    agent = FakeAgent()
    v = ProposalValidator(agent=agent, router=Router())
    p = TradeProposal(**{**_valid_trade().to_dict(),
                         "size": 0.7, "companion_id": "broski"})
    result = v.validate_trade(p)
    assert not result.ok
    assert "notional" in result.reason.lower(), result.reason


def test_equity_unavailable_fails_closed():
    """No equity reading => reject, never silently skip the risk cap.

    `broadcaster.latest_state` is {} at agent boot, so equity reads 0.0.
    The cap used to be skipped entirely in exactly that window.
    """
    agent = FakeAgent()
    # Assign directly: FakeBroadcaster's `state or {...}` treats {} as falsy
    # and would substitute the populated default.
    agent.broadcaster.latest_state = {}
    v = ProposalValidator(agent=agent, router=Router())
    result = v.validate_trade(_valid_trade())
    assert not result.ok
    assert "equity" in result.reason.lower(), result.reason


def test_ladder_rung_sum():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    bad = LadderProposal(
        proposal_id=new_ladder_id(), companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", total_size=0.2,
        rungs=(LadderRung(0.5, 141.0), LadderRung(0.3, 140.0)),  # sums to 0.8
        stop_loss=138.0, invalidation_price=138.0, rationale="",
    )
    assert not v.validate_ladder(bad).ok


def test_token_mint_verify():
    broker = TokenBroker(ttl_seconds=30.0)
    b = broker.mint("prop-abc")
    assert broker.verify(proposal_id="prop-abc", token=b.token, nonce=b.nonce, expires_at=b.expires_at)


def test_token_rejects_bad_signature():
    broker = TokenBroker(ttl_seconds=30.0)
    b = broker.mint("prop-abc")
    assert not broker.verify(proposal_id="prop-abc", token="0" * 64,
                             nonce=b.nonce, expires_at=b.expires_at)


def test_token_rejects_expired():
    broker = TokenBroker(ttl_seconds=0.001)
    b = broker.mint("prop-abc")
    time.sleep(0.05)
    assert not broker.verify(proposal_id="prop-abc", token=b.token,
                             nonce=b.nonce, expires_at=b.expires_at)


def test_mock_executor_broadcasts_executed():
    bc = FakeBroadcaster()
    m = MockExecutor(broadcaster=bc)
    p = _valid_trade()
    m.execute_trade(p)
    assert any(msg_type == "companion.trade.executed" for msg_type, _ in bc.msgs)


def test_no_write_tools_in_tools_readonly_registry():
    from hydra_companions import tools_readonly
    for name in tools_readonly.TOOL_REGISTRY:
        assert "place" not in name
        assert "cancel" not in name
        assert "propose" not in name


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all proposal tests passed")
