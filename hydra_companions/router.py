"""Per-intent per-companion model selection.

Deterministic. Reads model_routing.json once at construction. Applies
fallback cascade on provider errors. Logs every decision to
.hydra-companions/routing.jsonl for auditing.
"""
from __future__ import annotations
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hydra_companions.config import ROUTING_CONFIG


@dataclass(frozen=True)
class RouteDecision:
    provider: str          # "anthropic" | "xai"
    model_id: str          # "claude-sonnet-4-6" | "grok-4.3" | ...
    max_tokens: int
    temperature: float
    intent: str
    companion_id: str


class Router:
    def __init__(self, config_path: Optional[Path] = None):
        path = config_path or ROUTING_CONFIG
        try:
            self._cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"Router: failed to load {path}: {type(e).__name__}: {e}"
            ) from e
        self._routing = self._cfg["routing"]
        self._intents = self._cfg["intents"]
        self._fallbacks = self._cfg.get("fallbacks", {})
        self._pools = self._cfg.get("rotation_pools", {})
        self._caps = self._cfg.get("safety_caps", {})
        self._budgets = self._cfg.get("budgets", {})

    # ----- public API -----

    def pick(self, companion_id: str, intent: str, *,
             serious_mode: bool = False,
             seed: Optional[int] = None) -> RouteDecision:
        routes = self._routing.get(companion_id, {})
        intent_def = self._intents.get(intent) or self._intents["unknown"]
        entry = routes.get(intent) or routes.get("unknown") or routes.get("market_state_query", {})

        # Rotation pool override (e.g., broski.banter_humor)
        pool_key = f"{companion_id}.{intent}"
        pool = self._pools.get(pool_key)
        if pool:
            rng = random.Random(seed)
            model_id = _weighted_choice(pool, rng)
        else:
            model_id = entry.get("primary", "xai:grok-4.3")

        temperature = float(entry.get("temperature", 0.5))
        # Broski serious-mode temperature delta
        if serious_mode and companion_id == "broski":
            override = routes.get("serious_mode_override", {})
            if intent in override.get("applies_to_intents", []):
                temperature = max(0.0, temperature + float(override.get("temperature_delta", 0)))

        max_tokens = int(intent_def.get("default_max_tokens", 300))
        provider, model = _split_model_id(model_id)
        return RouteDecision(
            provider=provider, model_id=model,
            max_tokens=max_tokens, temperature=temperature,
            intent=intent, companion_id=companion_id,
        )

    def fallback(self, decision: RouteDecision,
                 already_tried: Optional[list] = None) -> Optional[RouteDecision]:
        """Return the next provider/model to try after a failure.

        `already_tried` is a list of "provider:model_id" strings the caller
        has already attempted. Returns None if the chain is exhausted.
        Callers should pass the running attempt list so we walk past
        candidates that also failed.
        """
        already = set(already_tried or [])
        already.add(f"{decision.provider}:{decision.model_id}")
        full_id = f"{decision.provider}:{decision.model_id}"
        chain = self._fallbacks.get(full_id, [])
        for candidate in chain:
            if candidate in already:
                continue
            provider, model = _split_model_id(candidate)
            return RouteDecision(
                provider=provider, model_id=model,
                max_tokens=decision.max_tokens, temperature=decision.temperature,
                intent=decision.intent, companion_id=decision.companion_id,
            )
        return None

    def safety_cap(self, companion_id: str, key: str, default=None):
        return self._caps.get(companion_id, {}).get(key, default)

    def daily_budget_usd(self, companion_id: str) -> float:
        return float(self._budgets.get(companion_id, {}).get("daily_usd", 0.0))


# ----- helpers -----

def _split_model_id(full_id: str) -> tuple[str, str]:
    if ":" not in full_id:
        return "xai", full_id
    provider, model = full_id.split(":", 1)
    return provider, model


def _weighted_choice(pool: list, rng: random.Random) -> str:
    total = sum(p.get("weight", 0) for p in pool)
    if total <= 0:
        return pool[0]["model"]
    r = rng.random() * total
    acc = 0.0
    for p in pool:
        acc += p.get("weight", 0)
        if r <= acc:
            return p["model"]
    return pool[-1]["model"]
