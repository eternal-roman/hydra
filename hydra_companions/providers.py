"""Unified LLM provider shim.

Wraps anthropic + openai (xAI base URL) SDKs behind one synchronous
interface. Phase 1 returns complete messages (non-streaming) to keep
the handler thread model simple; Phase 6+ will add streaming deltas.

Cost tracking returns (tokens_in, tokens_out, est_cost_usd) alongside
each response.

Tool-use loop is NOT implemented in Phase 1 \u2014 companions only have
read-only tools available in Phase 2+. For Phase 1 the interface
accepts a `tools` kwarg but ignores it; callers pass [] and the
companion relies on the system prompt alone.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional


# Per-million-token cost for budget estimation. Source: model_routing.json
# so we don't diverge; we read once at import.
try:
    import json as _json
    from pathlib import Path as _Path
    _ROUTING = _json.loads((_Path(__file__).parent / "model_routing.json").read_text(encoding="utf-8"))
    _COST_TABLE = {}
    for provider, pdef in _ROUTING["providers"].items():
        for model_id, mdef in pdef["models"].items():
            _COST_TABLE[(provider, model_id)] = (
                float(mdef.get("est_input_per_mtok_usd", 0.0)),
                float(mdef.get("est_output_per_mtok_usd", 0.0)),
            )
except Exception:
    _COST_TABLE = {}


@dataclass
class ProviderResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model_id: str = ""
    provider: str = ""
    stop_reason: str = ""
    error: Optional[str] = None


# Opus 5.5 rejects temperature/top_p/top_k. Effort "max" is its highest
# setting; thinking shares the max_tokens budget with the reply.
_OPUS_EFFORT = "max"
_OPUS_MAX_TOKENS_FLOOR = 8192
# grok-4.7's deepest reasoning_effort. grok-4.3 does not accept it.
_GROK_TRADING_MODEL = "grok-4.7"
_GROK_REASONING_EFFORT = "xhigh"
_GROK_MAX_TOKENS_FLOOR = 8192
# Under the dashboard wait (120s) so a hung socket fails as an error.
_CALL_TIMEOUT_S = 110.0


def _est_cost(provider: str, model_id: str, tokens_in: int, tokens_out: int) -> float:
    rates = _COST_TABLE.get((provider, model_id))
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return (tokens_in / 1_000_000) * in_rate + (tokens_out / 1_000_000) * out_rate


class ProviderClient:
    """Lazy-inits SDK clients on first use. Safe to construct without keys."""

    def __init__(self):
        self._anthropic = None
        self._xai = None

    def _anthropic_client(self):
        if self._anthropic is None:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=key)
        return self._anthropic

    def _xai_client(self):
        if self._xai is None:
            key = os.environ.get("XAI_API_KEY")
            if not key:
                raise RuntimeError("XAI_API_KEY not set")
            import openai
            self._xai = openai.OpenAI(api_key=key, base_url="https://api.x.ai/v1")
        return self._xai

    def call(self, *, provider: str, model_id: str,
             system: str, messages: list, max_tokens: int,
             temperature: float, tools: Optional[list] = None) -> ProviderResponse:
        """Blocking call. Returns ProviderResponse. Exceptions -> response with .error set."""
        try:
            if provider == "anthropic":
                return self._call_anthropic(model_id, system, messages, max_tokens, temperature)
            if provider == "xai":
                return self._call_xai(model_id, system, messages, max_tokens, temperature)
            return ProviderResponse(text="", provider=provider, model_id=model_id,
                                    error=f"unknown provider: {provider}")
        except Exception as e:
            return ProviderResponse(
                text="", provider=provider, model_id=model_id,
                error=f"{type(e).__name__}: {e}",
            )

    # ----- anthropic -----

    def _call_anthropic(self, model_id: str, system: str, messages: list,
                        max_tokens: int, temperature: float) -> ProviderResponse:
        client = self._anthropic_client()
        # Opus 5.5 returns 400 if temperature is present. The routing table
        # still carries a temperature for the xAI fallback.
        resp = client.with_options(timeout=_CALL_TIMEOUT_S).messages.create(
            model=model_id,
            max_tokens=max(int(max_tokens), _OPUS_MAX_TOKENS_FLOOR),
            system=system,
            messages=messages,
            output_config={"effort": _OPUS_EFFORT},
        )
        text_parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        tokens_in = getattr(resp.usage, "input_tokens", 0) or 0
        tokens_out = getattr(resp.usage, "output_tokens", 0) or 0
        return ProviderResponse(
            text="".join(text_parts),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_est_cost("anthropic", model_id, tokens_in, tokens_out),
            model_id=model_id,
            provider="anthropic",
            stop_reason=getattr(resp, "stop_reason", "end_turn") or "end_turn",
        )

    # ----- xai (openai-compatible) -----

    def _call_xai(self, model_id: str, system: str, messages: list,
                  max_tokens: int, temperature: float) -> ProviderResponse:
        client = self._xai_client()
        chat_messages = [{"role": "system", "content": system}]
        chat_messages.extend(messages)
        call = dict(
            model=model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=chat_messages,
        )
        if model_id == _GROK_TRADING_MODEL:
            call["max_tokens"] = max(int(max_tokens), _GROK_MAX_TOKENS_FLOOR)
            call["extra_body"] = {"reasoning_effort": _GROK_REASONING_EFFORT}
        resp = client.with_options(timeout=_CALL_TIMEOUT_S).chat.completions.create(**call)
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        return ProviderResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_est_cost("xai", model_id, tokens_in, tokens_out),
            model_id=model_id,
            provider="xai",
            stop_reason=getattr(choice, "finish_reason", "stop") or "stop",
        )
