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

    def _kraken_cli(self):
        return getattr(self.agent, "kraken_cli", None)

    def _system_healthy(self) -> ValidationResult:
        # Kraken status is an agent-level attribute, not in the broadcaster
        # snapshot. Walk the agent directly.
        status = getattr(self.agent, "_last_kraken_status", None)
        if status in ("maintenance", "cancel_only"):
            return ValidationResult.bad(f"kraken status: {status}")
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

        # Risk check (vs equity).
        equity = self._current_equity_usd()
        if equity > 0:
            risk_usd = abs(p.limit_price - p.stop_loss) * p.size
            risk_pct = (risk_usd / equity) * 100
            if risk_pct > caps["max_risk_per_trade_pct_equity"]:
                return ValidationResult.bad(
                    f"risk {risk_pct:.2f}% exceeds cap {caps['max_risk_per_trade_pct_equity']}%"
                )

        # Kraken min size/cost (best-effort).
        cli = self._kraken_cli()
        if cli is not None:
            ordermin = getattr(cli, "MIN_ORDER_SIZE", {}).get(p.pair.split("/")[0])
            costmin = getattr(cli, "MIN_COST", {}).get(p.pair.split("/")[1])
            if ordermin is not None and p.size < ordermin:
                return ValidationResult.bad(f"size {p.size} below Kraken ordermin {ordermin}")
            if costmin is not None and (p.size * p.limit_price) < costmin:
                return ValidationResult.bad(f"cost {p.size * p.limit_price:.4f} below Kraken costmin {costmin}")

        health = self._system_healthy()
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
