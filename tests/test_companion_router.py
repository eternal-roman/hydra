"""Router tests \u2014 per-intent per-companion model selection."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.router import Router


def test_every_companion_intent_resolves():
    r = Router()
    intents = ["greeting", "small_talk", "market_state_query",
               "trade_proposal", "teaching_explanation", "banter_humor"]
    for cid in ("athena", "apex", "broski"):
        for intent in intents:
            d = r.pick(cid, intent)
            assert d.provider in ("anthropic", "xai")
            assert d.model_id
            assert d.max_tokens > 0
            assert 0.0 <= d.temperature <= 1.0


def test_athena_teaching_uses_claude():
    r = Router()
    d = r.pick("athena", "teaching_explanation")
    assert d.provider == "anthropic"


def test_broski_banter_uses_xai():
    r = Router()
    d = r.pick("broski", "banter_humor", seed=0)
    assert d.provider == "xai"


def test_broski_serious_mode_lowers_temperature():
    r = Router()
    normal = r.pick("broski", "trade_proposal", serious_mode=False)
    serious = r.pick("broski", "trade_proposal", serious_mode=True)
    assert serious.temperature < normal.temperature


def test_fallback_chain_returns_other_provider():
    r = Router()
    primary = r.pick("athena", "teaching_explanation")
    fb = r.fallback(primary)
    assert fb is not None
    assert (fb.provider, fb.model_id) != (primary.provider, primary.model_id)


def test_fallback_cascade_walks_past_tried_candidates():
    """If the first fallback was already attempted, return the next one."""
    r = Router()
    primary = r.pick("athena", "teaching_explanation")  # anthropic:claude-opus-5-5
    fb1 = r.fallback(primary)
    # Simulate fb1 also failed; ask for the next one.
    tried = [f"{primary.provider}:{primary.model_id}",
             f"{fb1.provider}:{fb1.model_id}"]
    fb2 = r.fallback(fb1, already_tried=tried)
    # For anthropic's chain (xai fast-reasoning, xai reasoning), fb2 should differ from fb1.
    if fb2 is not None:
        assert (fb2.provider, fb2.model_id) != (fb1.provider, fb1.model_id)


def test_fallback_cascade_returns_none_when_exhausted():
    r = Router()
    primary = r.pick("athena", "teaching_explanation")
    # Try the whole chain + primary
    tried = [f"{primary.provider}:{primary.model_id}"]
    chain = r._fallbacks.get(f"{primary.provider}:{primary.model_id}", [])
    for candidate in chain:
        tried.append(candidate)
    assert r.fallback(primary, already_tried=tried) is None


def test_safety_cap_lookup():
    r = Router()
    assert r.safety_cap("athena", "max_trades_per_day") == 4
    assert r.safety_cap("apex", "max_trades_per_day") == 6
    assert r.safety_cap("broski", "max_trades_per_day") == 9


def test_daily_budget_nonzero():
    r = Router()
    for cid in ("athena", "apex", "broski"):
        assert r.daily_budget_usd(cid) > 0


# ───────────────────────────────────────────────────────────────
# v1.1 — chart_analysis intent + Apex Grok migration
# ───────────────────────────────────────────────────────────────

def test_v11_chart_analysis_intent_resolves_for_all_souls():
    r = Router()
    for cid in ("athena", "apex", "broski"):
        d = r.pick(cid, "chart_analysis")
        assert d.provider in ("anthropic", "xai")
        assert d.model_id
        assert d.intent == "chart_analysis"


def test_v11_apex_trade_proposal_uses_grok_reasoning():
    """v1.1 migration: Apex execution-class calls move to Grok reasoning."""
    r = Router()
    d = r.pick("apex", "trade_proposal", seed=0)
    assert d.provider == "xai"
    assert d.model_id == "grok-4.7"


def test_v11_apex_ladder_proposal_uses_grok_reasoning():
    r = Router()
    d = r.pick("apex", "ladder_proposal", seed=0)
    assert d.provider == "xai"
    assert d.model_id == "grok-4.7"


def test_v11_apex_teaching_rotates_grok():
    """Apex teaching has a rotation pool between Grok reasoning and Grok fast."""
    r = Router()
    # Deterministic via seed — verify the pool is used (xai, not anthropic).
    d = r.pick("apex", "teaching_explanation", seed=0)
    assert d.provider == "xai"


def test_athena_deep_intents_use_opus():
    """Athena's deep intents stay on Anthropic, now Opus 5.5."""
    r = Router()
    for intent in ("teaching_explanation", "trade_proposal", "chart_analysis"):
        decision = r.pick("athena", intent)
        assert decision.provider == "anthropic"
        assert decision.model_id == "claude-opus-5-5"


def test_v11_apex_chart_analysis_primary_is_grok_reasoning():
    r = Router()
    d = r.pick("apex", "chart_analysis", seed=0)
    assert d.provider == "xai"


def test_v11_intent_classifier_has_chart_analysis_heuristic():
    """Classifier heuristics include a chart_analysis rule in v1.1."""
    from hydra_companions.intent_classifier import IntentClassifier
    ic = IntentClassifier()
    # Loosely validate the heuristic rules list has a chart_analysis entry.
    import json, pathlib
    cfg_path = pathlib.Path(__file__).resolve().parent.parent / "hydra_companions" / "model_routing.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rules = cfg["intent_classifier"]["heuristic_rules"]
    intents = {r["intent"] for r in rules}
    assert "chart_analysis" in intents


# ───────────────────────────────────────────────────────────────
# v2.12.5 — journal-visibility patch (classifier coverage)
# ───────────────────────────────────────────────────────────────

def test_v125_journal_queries_route_to_market_state_query():
    """Journal/history-style prompts must classify as market_state_query
    (previously fell to small_talk or the question fallback, which
    excluded the journal from the context blob)."""
    from hydra_companions.intent_classifier import IntentClassifier
    ic = IntentClassifier()
    phrases = [
        "can you see my order journal?",
        "look at my prior trades in the journal",
        "show me my recent fills",
        "what did I trade today",
        "pull up my trade history",
        "order history please",
    ]
    for p in phrases:
        r = ic.classify(p)
        assert r.intent == "market_state_query", \
            f"{p!r} classified as {r.intent} (expected market_state_query)"
        assert r.method == "heuristic", \
            f"{p!r} fell to {r.method} instead of a heuristic match"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all router tests passed")
