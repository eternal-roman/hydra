"""LiveExecutor \u2014 Phase 3 executor that places real Kraken orders.

Plugs into the same interface as MockExecutor:
    .execute_trade(proposal) -> dict
    .execute_ladder(proposal) -> dict

Uses `KrakenCLI.order_buy/sell` with a numeric `userref` derived from
the proposal ID so the existing ExecutionStream lifecycle
(PLACED/FILLED/PARTIALLY_FILLED/CANCELLED_UNFILLED/REJECTED) can
attribute fills to this companion + proposal.

Daily caps (per-companion) live in the coordinator, not here \u2014
coordinator.handle_confirm runs the cap check before dispatching.
"""
from __future__ import annotations
import hashlib
import json
import time
from typing import TYPE_CHECKING

from hydra_companions.config import PROPOSALS_LOG

if TYPE_CHECKING:
    from hydra_companions.executor import TradeProposal, LadderProposal


def _proposal_userref(proposal_id: str, rung_idx: int = 0) -> int:
    """Derive a stable 32-bit positive int from proposal_id (+ rung)."""
    h = hashlib.sha256(f"{proposal_id}|R{rung_idx}".encode("utf-8")).digest()
    # 31 bits so it stays positive in Kraken's int32 userref field
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


class LiveExecutor:
    def __init__(self, *, agent, coordinator):
        self.agent = agent
        self.coordinator = coordinator

    # ----- public -----

    def execute_trade(self, p: "TradeProposal") -> dict:
        # Per-companion daily cap check (runs late in the path so this is
        # the final gate before the exchange).
        cap = self.coordinator.router.safety_cap(p.companion_id, "max_trades_per_day", 0)
        if cap > 0:
            k = (p.user_id, p.companion_id)
            count_today = self.coordinator._daily_trades.get(k, 0)
            if count_today >= cap:
                self._broadcast_failed(p.proposal_id, p.companion_id,
                                       f"daily cap hit ({count_today}/{cap})")
                return {"ok": False, "error": "daily cap hit"}

        cli = self._kraken_cli()
        userref = _proposal_userref(p.proposal_id)
        fn = cli.order_buy if p.side == "buy" else cli.order_sell
        try:
            result = fn(
                pair=p.pair, volume=float(p.size), price=float(p.limit_price),
                order_type="limit", post_only=True, userref=userref,
            )
        except Exception as e:
            self._broadcast_failed(p.proposal_id, p.companion_id, f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

        self._journal(event="LIVE_PLACED", proposal=p.to_dict(), extra={
            "userref": userref, "kraken_result": _safe_trim(result),
        })

        if result.get("error"):
            self._broadcast_failed(p.proposal_id, p.companion_id, result["error"])
            return {"ok": False, "error": result["error"]}

        # Placement succeeded; lifecycle continues via ExecutionStream.
        self._broadcast("companion.trade.executed", {
            "proposal_id": p.proposal_id,
            "companion_id": p.companion_id,
            "user_id": p.user_id,
            "status": "placed",
            "userref": userref,
            "txid": result.get("txid") or result.get("ordertx"),
        })
        return {"ok": True, "userref": userref}

    def execute_ladder(self, p: "LadderProposal") -> dict:
        # Per-companion daily cap — final gate before the exchange, mirroring
        # execute_trade(). The coordinator already pre-checks the cap for both
        # trade and ladder confirms; this redundant check closes the
        # concurrent-confirm TOCTOU window on the money path so a ladder cannot
        # slip through after the coordinator's check-then-place releases its lock.
        cap = self.coordinator.router.safety_cap(p.companion_id, "max_trades_per_day", 0)
        if cap > 0:
            k = (p.user_id, p.companion_id)
            count_today = self.coordinator._daily_trades.get(k, 0)
            if count_today >= cap:
                self._broadcast_failed(p.proposal_id, p.companion_id,
                                       f"daily cap hit ({count_today}/{cap})")
                return {"ok": False, "error": "daily cap hit"}

        cli = self._kraken_cli()
        fn = cli.order_buy if p.side == "buy" else cli.order_sell
        placed = []
        for i, rung in enumerate(p.rungs):
            userref = _proposal_userref(p.proposal_id, rung_idx=i)
            size = p.total_size * rung.pct_of_total
            try:
                result = fn(
                    pair=p.pair, volume=float(size), price=float(rung.limit_price),
                    order_type="limit", post_only=True, userref=userref,
                )
            except Exception as e:
                self._broadcast_failed(p.proposal_id, p.companion_id,
                                       f"rung {i}: {type(e).__name__}: {e}")
                return {"ok": False, "error": str(e), "placed_rungs": placed}
            if result.get("error"):
                self._broadcast_failed(p.proposal_id, p.companion_id,
                                       f"rung {i}: {result['error']}")
                return {"ok": False, "error": result["error"], "placed_rungs": placed}
            placed.append({
                "idx": i, "userref": userref, "size": size,
                "limit_price": rung.limit_price, "status": "placed",
                "txid": result.get("txid") or result.get("ordertx"),
            })
            self._journal(event="LIVE_LADDER_RUNG", proposal=p.to_dict(),
                          extra={"rung": i, "userref": userref,
                                 "kraken_result": _safe_trim(result)})

        # Register ladder for the watcher so invalidation cancels remaining
        # unfilled rungs. mark_fill() is the companion hook for an
        # ExecutionStream integration: when a fill arrives for a specific
        # userref, look it up here by matching against placed rung userrefs
        # and call watcher.mark_fill(proposal_id, rung_idx). Until the
        # execution-stream companion bridge lands, the watcher treats every
        # rung as unfilled and cancels all remaining on invalidation \u2014
        # which is conservative and safe.
        watcher = getattr(self.coordinator, "ladder_watcher", None)
        if watcher is not None:
            try:
                watcher.register(p, placed)
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        self._broadcast("companion.ladder.executed", {
            "proposal_id": p.proposal_id,
            "companion_id": p.companion_id,
            "user_id": p.user_id,
            "rungs": placed,
            "status": "placed",
        })
        return {"ok": True, "rungs": placed}

    # ----- helpers -----

    def _kraken_cli(self):
        """Prefer an agent-attached instance; fall back to the static class."""
        cli = getattr(self.agent, "kraken_cli", None)
        if cli is not None:
            return cli
        from hydra_agent import KrakenCLI
        return KrakenCLI

    def _broadcast(self, msg_type: str, payload: dict) -> None:
        try:
            self.agent.broadcaster.broadcast_message(msg_type, payload)
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    def _broadcast_failed(self, proposal_id: str, companion_id: str, reason: str) -> None:
        self._broadcast("companion.trade.failed", {
            "proposal_id": proposal_id,
            "companion_id": companion_id,
            "reason": reason,
        })

    def _journal(self, *, event: str, proposal: dict, extra: dict | None = None) -> None:
        try:
            entry = {"ts": time.time(), "event": event, "proposal": proposal}
            if extra:
                entry.update(extra)
            with PROPOSALS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")


def _safe_trim(r: dict) -> dict:
    """Return only the interesting fields from a Kraken CLI result, trimmed."""
    if not isinstance(r, dict):
        return {"repr": repr(r)[:200]}
    out = {}
    for k in ("txid", "ordertx", "descr", "error"):
        if k in r:
            out[k] = r[k]
    return out
