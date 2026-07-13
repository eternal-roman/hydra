"""Brain prompt smoke test: ensure the six new feature names are in the
RM system prompt and that rendering an RM user message with populated
features surfaces the numeric values. This does not call the LLM."""
import pytest
from hydra_brain import HydraBrain, RISK_MANAGER_PROMPT


def test_risk_prompt_names_all_six_features():
    for name in (
        "realized_vol_1h_pct", "realized_vol_24h_pct",
        "drawdown_velocity_pct_per_hr", "fill_rate_24h",
        "avg_slippage_bps_24h", "cross_pair_corr_24h",
        "minutes_since_last_trade",
    ):
        assert name in RISK_MANAGER_PROMPT, f"{name} missing from RM prompt"


def test_risk_prompt_references_cues():
    # Each feature must have a concrete numeric cue so RM can cite a threshold.
    lower = RISK_MANAGER_PROMPT.lower()
    assert "drawdown_velocity" in lower and "bleed" in lower, \
        "DD velocity lacks 'bleed' cue"
    assert "fill_rate" in lower and "0.3" in lower, \
        "fill_rate lacks numeric cue"
    assert "cross_pair_corr" in lower and "0.8" in lower, \
        "correlation lacks numeric cue"


def test_format_rm_features_renders_agent_key_names():
    """_format_rm_features must read the exact keys the agent writes (realized_vol_*_pct). Pre-fix it read the un-suffixed
    names, so both vol lines rendered null on every tick while the section
    header still appeared — a silent remediation defeat no test caught."""
    state = {"quant_indicators": {
        "realized_vol_1h_pct": 42.5,
        "realized_vol_24h_pct": 18.3,
        "drawdown_velocity_pct_per_hr": -0.7,
        "fill_rate_24h": 0.9,
        "avg_slippage_bps_24h": 1.2,
        "cross_pair_corr_24h": 0.55,
        "minutes_since_last_trade": 12,
    }}
    block = HydraBrain._format_rm_features(state)
    assert "42.5" in block, "1h realized vol value missing (key mismatch?)"
    assert "18.3" in block, "24h realized vol value missing (key mismatch?)"
    # Header legend says "null = insufficient window" — check value slots only.
    assert ": null" not in block, f"populated features rendered null: {block}"


def test_format_rm_features_absent_returns_empty():
    assert HydraBrain._format_rm_features({"quant_indicators": {}}) == ""
