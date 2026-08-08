"""Trade + ladder proposals, validator, and pluggable executors.

Phase 2 ships with MockExecutor only. Phase 3 plugs LiveExecutor in
without changing any other code. Validator is always the same and
fires regardless of which executor is active.
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Literal, Optional

from hydra_companions.config import PROPOSALS_LOG
from hydra_pair_registry import default_registry


# ════════════════════════════════════════════════════════════════════
# DATA SHAPES
# ════════════════════════════════════════════════════════════════════

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    companion_id: str
    user_id: str
    pair: str
    side: Side
    size: float           # base-asset units
    limit_price: float
    stop_loss: float
    rationale: str
    risk_usd: float = 0.0
    risk_pct_equity: float = 0.0
    estimated_cost: float = 0.0
    created_at: float = 0.0
    expires_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LadderRung:
    pct_of_total: float
    limit_price: float


@dataclass(frozen=True)
class LadderProposal:
    proposal_id: str
    companion_id: str
    user_id: str
    pair: str
    side: Side
    total_size: float
    rungs: tuple   # tuple[LadderRung, ...]
    stop_loss: float
    invalidation_price: float
    rationale: str
    risk_usd: float = 0.0
    risk_pct_equity: float = 0.0
    created_at: float = 0.0
    expires_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rungs"] = [asdict(r) for r in self.rungs]
        return d


# ════════════════════════════════════════════════════════════════════
# VALIDATOR  (hard-coded rules, same across executors)
# ════════════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    ok: bool
    reason: Optional[str] = None

    @classmethod
    def good(cls):
        return cls(ok=True)

    @classmethod
    def bad(cls, reason: str):
        return cls(ok=False, reason=reason)


class ProposalValidator:
    def __init__(self, *, agent, router):
        self.agent = agent
        self.router = router

    # ----- shared helpers -----

    def _current_equity_usd(self) -> float:
        snap = getattr(self.agent.broadcaster, "latest_state", {}) or {}
        bal = snap.get("balance_usd") or {}
        total = bal.get("total_usd")
        if isinstance(total, (int, float)):
            return float(total)
        # Fallback: sum engine equities
        pairs = snap.get("pairs") or {}
        return float(sum((p.get("portfolio") or {}).get("equity", 0) for p in pairs.values()))

    def _current_price(self, pair: str) -> Optional[float]:
        snap = getattr(self.agent.broadcaster, "latest_state", {}) or {}
        pdata = (snap.get("pairs") or {}).get(pair) or {}
        price = pdata.get("price")
        return float(price) if isinstance(price, (int, float)) else None

    def _system_healthy(self, side: str = "") -> ValidationResult:
        # Kraken status is an agent-level attribute, not in the broadcaster
        # snapshot. Walk the agent directly.
        status = getattr(self.agent, "_last_kraken_status", None)
        if status in ("maintenance", "cancel_only"):
            return ValidationResult.bad(f"kraken status: {status}")
        # Portfolio-wide 15% drawdown breaker. This is a SEPARATE flag from
        # the per-engine halt: the portfolio breaker arms off summed equity,
        # so it fires while every individual engine is still under its own
        # threshold and `engine.halted` is False everywhere. Checking only the
        # engine flags let a confirmed companion BUY through a halted
        # portfolio — the one thing the breaker exists to stop. BUY only;
        # SELL/flatten stays allowed, matching PR-A exit guarantees.
        if side == "buy" and getattr(self.agent, "_portfolio_buy_halted", False):
            dd = getattr(self.agent, "_portfolio_max_drawdown_pct", 0.0)
            return ValidationResult.bad(
                f"portfolio drawdown halt ({dd:.1f}%) — new BUYs blocked"
            )
        # Engine halts (circuit breaker) live on the engine instances.
        engines = getattr(self.agent, "engines", {}) or {}
        for pair, engine in engines.items():
            if getattr(engine, "halted", False):
                return ValidationResult.bad(f"{pair}: engine halted")
        return ValidationResult.good()

    # ----- core validation -----

    def validate_trade(self, p: TradeProposal) -> ValidationResult:
        cap = self.router
        caps = {
            "max_trades_per_day": cap.safety_cap(p.companion_id, "max_trades_per_day", 0),
            "max_risk_per_trade_pct_equity": cap.safety_cap(p.companion_id, "max_risk_per_trade_pct_equity", 1.0),
            "max_price_band_from_mid_pct": cap.safety_cap(p.companion_id, "max_price_band_from_mid_pct", 3.0),
        }

        if not p.pair or "/" not in p.pair:
            return ValidationResult.bad(f"bad pair: {p.pair!r}")
        # COMPANION_SPEC §8 step 2: the pair must be one the agent actually
        # runs. Without this an unknown pair also silently disabled the price
        # band below (`_current_price` returns None for a pair absent from the
        # snapshot), so ANY limit price was accepted on a pair the engine,
        # journal and LadderWatcher cannot track.
        engines = getattr(self.agent, "engines", {}) or {}
        if engines and p.pair not in engines:
            return ValidationResult.bad(
                f"{p.pair} is not an active trading pair "
                f"({', '.join(sorted(engines))})"
            )
        if p.side not in ("buy", "sell"):
            return ValidationResult.bad(f"bad side: {p.side!r}")
        if p.size <= 0:
            return ValidationResult.bad("size must be > 0")
        if p.limit_price <= 0:
            return ValidationResult.bad("limit_price must be > 0")
        if p.stop_loss <= 0:
            return ValidationResult.bad("stop_loss required and > 0")

        # Stop must be on the right side of entry.
        if p.side == "buy" and p.stop_loss >= p.limit_price:
            return ValidationResult.bad("buy stop must be below limit")
        if p.side == "sell" and p.stop_loss <= p.limit_price:
            return ValidationResult.bad("sell stop must be above limit")

        # Price band vs current mid.
        mid = self._current_price(p.pair)
        if mid is not None and mid > 0:
            band = caps["max_price_band_from_mid_pct"]
            diff_pct = abs(p.limit_price - mid) / mid * 100
            if diff_pct > band:
                return ValidationResult.bad(f"limit {diff_pct:.2f}% from mid exceeds {band}% band")

        # Risk check (vs equity). Fails CLOSED: at agent boot the broadcaster
        # snapshot is empty, so equity reads 0.0 and the whole cap used to be
        # skipped — exactly when the least is known about the account.
        equity = self._current_equity_usd()
        if equity <= 0:
            return ValidationResult.bad(
                "account equity unavailable — cannot size risk"
            )
        risk_usd = abs(p.limit_price - p.stop_loss) * p.size
        risk_pct = (risk_usd / equity) * 100
        if risk_pct > caps["max_risk_per_trade_pct_equity"]:
            return ValidationResult.bad(
                f"risk {risk_pct:.2f}% exceeds cap {caps['max_risk_per_trade_pct_equity']}%"
            )
        # The risk cap alone is not a size cap: tightening the stop makes an
        # arbitrarily large order "low risk" (5 BTC with a $1 stop is $5 of
        # risk and $500k of notional). Bound gross notional by the same
        # max_position_pct the engine enforces (PR-B), so a companion order
        # cannot exceed what the engine would size for itself.
        notional = p.size * p.limit_price
        engine = engines.get(p.pair) if engines else None
        max_position_pct = getattr(
            getattr(engine, "sizer", None), "max_position_pct", 0.30
        )
        max_notional = equity * float(max_position_pct)
        if notional > max_notional:
            return ValidationResult.bad(
                f"notional ${notional:,.2f} exceeds "
                f"{float(max_position_pct) * 100:.0f}% of equity "
                f"(${max_notional:,.2f})"
            )

        # Kraken min size/cost. Read from the pair registry — the single
        # source of truth. This previously read `agent.kraken_cli`, an
        # attribute HydraAgent never assigns, for `MIN_ORDER_SIZE`/`MIN_COST`,
        # which live on hydra_engine.PositionSizer and not on KrakenCLI — so
        # both lookups yielded {} and the check silently passed everything.
        meta = default_registry().get(p.pair)
        if meta is None:
            return ValidationResult.bad(f"{p.pair} not in pair registry")
        if p.size < meta.ordermin:
            return ValidationResult.bad(
                f"size {p.size} below Kraken ordermin {meta.ordermin}"
            )
        if notional < meta.costmin:
            return ValidationResult.bad(
                f"cost {notional:.4f} below Kraken costmin {meta.costmin}"
            )

        health = self._system_healthy(p.side)
        if not health.ok:
            return health
        return ValidationResult.good()

    def validate_ladder(self, p: LadderProposal) -> ValidationResult:
        if not p.rungs:
            return ValidationResult.bad("ladder needs at least one rung")
        max_rungs = self.router.safety_cap(p.companion_id, "max_ladder_rungs", 4)
        if len(p.rungs) > max_rungs:
            return ValidationResult.bad(f"rung count {len(p.rungs)} > max {max_rungs}")
        pct_sum = sum(r.pct_of_total for r in p.rungs)
        if not 0.98 <= pct_sum <= 1.02:
            return ValidationResult.bad(f"rung % must sum to 1.0 (got {pct_sum:.3f})")
        # Validate each rung as a mini-trade.
        for i, r in enumerate(p.rungs):
            rung_size = p.total_size * r.pct_of_total
            fake = TradeProposal(
                proposal_id=f"{p.proposal_id}_R{i}",
                companion_id=p.companion_id, user_id=p.user_id,
                pair=p.pair, side=p.side, size=rung_size,
                limit_price=r.limit_price, stop_loss=p.stop_loss,
                rationale="", created_at=p.created_at, expires_at=p.expires_at,
            )
            sub = self.validate_trade(fake)
            if not sub.ok:
                return ValidationResult.bad(f"rung {i}: {sub.reason}")
        return ValidationResult.good()


# ════════════════════════════════════════════════════════════════════
# EXECUTORS
# ════════════════════════════════════════════════════════════════════

class MockExecutor:
    """Phase 2 executor \u2014 writes proposal + synthetic fill to journal."""

    def __init__(self, *, broadcaster):
        self.broadcaster = broadcaster

    def execute_trade(self, proposal: TradeProposal) -> dict:
        self._journal(event="CONFIRMED", proposal=proposal.to_dict())
        # Simulate a quick fill path so the UI can render lifecycle states.
        self.broadcaster.broadcast_message("companion.trade.executed", {
            "proposal_id": proposal.proposal_id,
            "companion_id": proposal.companion_id,
            "user_id": proposal.user_id,
            "mock": True,
            "fill_price": proposal.limit_price,
            "fill_size": proposal.size,
            "status": "filled",
        })
        return {"ok": True, "mock": True}

    def execute_ladder(self, proposal: LadderProposal) -> dict:
        self._journal(event="LADDER_CONFIRMED", proposal=proposal.to_dict())
        self.broadcaster.broadcast_message("companion.ladder.executed", {
            "proposal_id": proposal.proposal_id,
            "companion_id": proposal.companion_id,
            "user_id": proposal.user_id,
            "mock": True,
            "rungs": [{"limit_price": r.limit_price, "filled": True,
                       "size": proposal.total_size * r.pct_of_total}
                      for r in proposal.rungs],
            "status": "filled",
        })
        return {"ok": True, "mock": True}

    def _journal(self, *, event: str, proposal: dict) -> None:
        try:
            with PROPOSALS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "event": event, "proposal": proposal,
                }) + "\n")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")


# ════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════

def new_proposal_id() -> str:
    return f"prop-{uuid.uuid4().hex[:12]}"


def new_ladder_id() -> str:
    return f"ladr-{uuid.uuid4().hex[:12]}"
