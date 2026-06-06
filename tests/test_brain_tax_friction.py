"""Tax/fee friction nudge: the analyst gets a soft advisory line when a SELL
would realize a sub-floor gain. It is advisory only — never a gate — and must
fail silent on malformed state. Does not call the LLM."""
import pytest
from hydra_brain import HydraBrain, TAX_FRICTION_FLOOR_USD


def _state(action="SELL", size=1.0, pnl=10.0):
    return {"signal": {"action": action},
            "position": {"size": size, "unrealized_pnl": pnl}}


def test_fires_on_small_sell_gain():
    out = HydraBrain._format_tax_friction(_state(pnl=12.50))
    assert "TAX/FEE FRICTION" in out
    assert "$12.50" in out
    assert f"${TAX_FRICTION_FLOOR_USD:.0f}" in out  # cites the floor


def test_silent_on_loss():
    # Cutting a loser is risk management, not churn — no nudge.
    assert HydraBrain._format_tax_friction(_state(pnl=-5.0)) == ""


def test_silent_at_or_above_floor():
    assert HydraBrain._format_tax_friction(_state(pnl=TAX_FRICTION_FLOOR_USD)) == ""
    assert HydraBrain._format_tax_friction(_state(pnl=120.0)) == ""


def test_silent_on_buy():
    assert HydraBrain._format_tax_friction(_state(action="BUY", pnl=10.0)) == ""


def test_silent_without_position():
    assert HydraBrain._format_tax_friction(_state(size=0.0, pnl=10.0)) == ""


def test_silent_on_malformed_state():
    assert HydraBrain._format_tax_friction({}) == ""
    assert HydraBrain._format_tax_friction({"signal": {"action": "SELL"},
                                            "position": {"unrealized_pnl": "n/a"}}) == ""


def test_env_override_raises_floor(monkeypatch):
    monkeypatch.setenv("HYDRA_TAX_FRICTION_FLOOR_USD", "200")
    # A $120 gain is below a $200 floor now → nudge fires.
    out = HydraBrain._format_tax_friction(_state(pnl=120.0))
    assert "$120.00" in out and "$200" in out


def test_env_zero_disables(monkeypatch):
    monkeypatch.setenv("HYDRA_TAX_FRICTION_FLOOR_USD", "0")
    assert HydraBrain._format_tax_friction(_state(pnl=10.0)) == ""


def test_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HYDRA_TAX_FRICTION_FLOOR_USD", "not-a-number")
    out = HydraBrain._format_tax_friction(_state(pnl=10.0))
    assert "TAX/FEE FRICTION" in out


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
