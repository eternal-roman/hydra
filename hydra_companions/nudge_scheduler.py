"""Proactive nudge scheduler \u2014 Phase 6.

Monitors live-state transitions and, when triggered, pushes an
in-character message from the active companion. Rate-limited (600s
default between nudges) and silenced if the user has typed within 90s.

Kill switch: HYDRA_COMPANION_NUDGES=0 disables proactive messages (default ON).
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


DEFAULT_MIN_INTERVAL_S = 600.0
USER_ACTIVITY_SUPPRESSION_S = 90.0
POLL_INTERVAL_S = 5.0


@dataclass
class _NudgeState:
    last_nudge_ts: float = 0.0
    last_regime_by_pair: dict = field(default_factory=dict)
    last_user_msg_ts: float = 0.0
    muted_until: float = 0.0


class NudgeScheduler:
    def __init__(self, *, coordinator, agent):
        self.coordinator = coordinator
        self.agent = agent
        self._state = _NudgeState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="NudgeScheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def record_user_activity(self):
        with self._lock:
            self._state.last_user_msg_ts = time.time()

    def mute(self, seconds: float) -> None:
        with self._lock:
            self._state.muted_until = time.time() + float(seconds)

    # ----- internal -----

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            if self._stop.wait(POLL_INTERVAL_S):
                return

    def _tick(self):
        now = time.time()
        with self._lock:
            if now < self._state.muted_until:
                return
            if now - self._state.last_nudge_ts < DEFAULT_MIN_INTERVAL_S:
                return
            if now - self._state.last_user_msg_ts < USER_ACTIVITY_SUPPRESSION_S:
                return

        # Probe for a trigger reason.
        trigger, context = self._check_triggers()
        if not trigger:
            return

        # Pick the currently-active companion (best proxy for who the
        # user wants to hear from). Coordinator doesn't track "active";
        # we approximate by favouring a coin-flip across the three.
        # In practice the dashboard surfaces nudges as unread dots per
        # companion, so we broadcast under whatever companion the trigger
        # semantically belongs to \u2014 default apex for the objective
        # "regime flipped" nudges.
        companion_id = self._companion_for_trigger(trigger)
        comp = self.coordinator.get(companion_id)
        if comp is None:
            return

        # Build a short in-character prompt.
        prompt = self._nudge_prompt(trigger, context, comp.soul.display_name)
        # Run the turn via the companion so the voice stays in soul.
        try:
            result = comp.respond(prompt)
        except Exception:
            return
        if result.error:
            return

        with self._lock:
            self._state.last_nudge_ts = now

        # Broadcast as a message the UI treats like any other assistant turn.
        try:
            self.agent.broadcaster.broadcast_message("companion.message.complete", {
                "message_id": f"nudge-{int(now)}",
                "companion_id": companion_id,
                "user_id": "local",
                "text": result.message,
                "intent": "idle_proactive_nudge",
                "model_used": result.model_used,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost_usd": round(result.cost_usd, 6),
                "proactive": True,
            })
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    def _check_triggers(self) -> tuple[Optional[str], dict]:
        snap = getattr(self.agent.broadcaster, "latest_state", {}) or {}
        pairs = snap.get("pairs") or {}
        # Regime-flip trigger
        new_regimes = {p: (pdata or {}).get("regime") for p, pdata in pairs.items()}
        with self._lock:
            last = dict(self._state.last_regime_by_pair)
            self._state.last_regime_by_pair = dict(new_regimes)
        flips = []
        for p, r in new_regimes.items():
            prev = last.get(p)
            if prev is not None and prev != r and r is not None:
                flips.append((p, prev, r))
        if flips:
            return "regime_flip", {"flips": flips}
        return None, {}

    def _companion_for_trigger(self, trigger: str) -> str:
        """Route a nudge trigger to the best-fit companion voice.

        Phase 6 v1 only ships regime-flip triggers and routes them to
        Apex (trader-speak). Future triggers \u2014 drawdown \u2192 Athena,
        narrative spike \u2192 Broski, etc. \u2014 should extend this map.
        Unknown triggers fall back to Apex as the neutral choice.
        """
        trigger_to_companion = {
            "regime_flip": "apex",
            # Reserved mappings for Phase 6.x expansion:
            # "drawdown_gt_5pct": "athena",
            # "narrative_spike":  "broski",
            # "funding_extreme":  "apex",
        }
        return trigger_to_companion.get(trigger, "apex")

    def _nudge_prompt(self, trigger: str, context: dict, display_name: str) -> str:
        if trigger == "regime_flip":
            flips = context.get("flips", [])
            lines = [f"{p}: {old} -> {new}" for p, old, new in flips[:2]]
            return (
                "system: volunteer a brief proactive market observation. "
                f"regime flip just landed: {'; '.join(lines)}. "
                "one to two sentences in your voice. do not ask a question \u2014 just note it."
            )
        return "system: brief market observation in your voice."
