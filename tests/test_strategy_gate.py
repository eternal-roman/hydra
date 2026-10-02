"""Evidence-gated strategy selection (hydra_strategy_gate).

A pair trades the daily trend sleeve under HYDRA_TREND_SLEEVE=auto only
when the pre-registered gate passed, significantly, on the operator's own
data for that base asset, for the rules the engine runs now, recently.
"""
import calendar
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_engine import HydraEngine  # noqa: E402
from hydra_strategy_gate import resolve_trend_sleeve, sleeve_mode  # noqa: E402

NOW = calendar.timegm((2026, 10, 2, 0, 0, 0, 0, 0, 0))
PASS_ASSET = {"verdict": "PASS",
              "engine_check": {"complete": True, "G_beats_F": True, "fidelity": True}}


def _report(**overrides):
    report = {
        "generated_at": "2026-09-20T00:00:00Z",
        "verdict": "PASS",
        "significant": True,
        "params": {"resize_days": HydraEngine.SLEEVE_RESIZE_DAYS,
                   "resize_tol": HydraEngine.SLEEVE_RESIZE_TOL,
                   "long_at": HydraEngine.TREND_SCORE_LONG},
        "assets": {"BTC/USD": dict(PASS_ASSET), "ETH/USD": dict(PASS_ASSET),
                   "ZEC/USD": {"verdict": "FAIL"}},
    }
    report.update(overrides)
    return report


def _resolve(pairs, report, env=None):
    return resolve_trend_sleeve(pairs, env=env or {}, report=report, now=NOW)


def test_mode_parsing():
    assert sleeve_mode("1") == sleeve_mode(" ON ") == "on"
    assert sleeve_mode("0") == sleeve_mode("false") == "off"
    assert sleeve_mode(None) == sleeve_mode("") == sleeve_mode("auto") == "auto"


def test_explicit_switches_override_the_evidence():
    on = resolve_trend_sleeve(["ZEC/USD"], env={"HYDRA_TREND_SLEEVE": "1"}, report=None)
    assert on["ZEC/USD"].enabled
    off = resolve_trend_sleeve(["BTC/USD"], env={"HYDRA_TREND_SLEEVE": "0"},
                               report=_report(), now=NOW)
    assert not off["BTC/USD"].enabled


def test_no_gate_result_keeps_the_current_engine(tmp_path):
    out = resolve_trend_sleeve(["BTC/USD"], env={},
                               gate_path=str(tmp_path / "missing.json"), now=NOW)
    assert not out["BTC/USD"].enabled and "no gate result" in out["BTC/USD"].reason


def test_a_significant_pass_enables_only_the_assets_that_passed():
    out = _resolve(["BTC/USD", "ETH/USDC", "ZEC/USD", "SOL/USD"], _report())
    assert out["BTC/USD"].enabled
    assert out["ETH/USDC"].enabled           # same base asset, other stable quote
    assert not out["ZEC/USD"].enabled and "FAIL" in out["ZEC/USD"].reason
    assert not out["SOL/USD"].enabled and "not in the gate" in out["SOL/USD"].reason


def test_every_refusal_condition_keeps_the_pair_off():
    cases = {
        "daily only": _report(verdict="PASS_DAILY_ONLY"),
        "not significant": _report(significant=False),
        "stale": _report(generated_at="2025-01-01T00:00:00Z"),
        "undated": _report(generated_at=None),
        "other rules": _report(params={"resize_days": 0, "resize_tol": 0.10, "long_at": 0.6}),
        "old report": _report(params={}),
    }
    for name, report in cases.items():
        assert not _resolve(["BTC/USD"], report)["BTC/USD"].enabled, name
    bad_check = _report(assets={"BTC/USD": {"verdict": "PASS",
                                            "engine_check": {"complete": True,
                                                             "G_beats_F": False,
                                                             "fidelity": True}}})
    decision = _resolve(["BTC/USD"], bad_check)["BTC/USD"]
    assert not decision.enabled and "G_beats_F" in decision.reason


def test_the_age_limit_is_configurable():
    report = _report(generated_at="2026-01-01T00:00:00Z")  # ~274 days before NOW
    assert not _resolve(["BTC/USD"], report)["BTC/USD"].enabled
    env = {"HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS": "365"}
    assert _resolve(["BTC/USD"], report, env)["BTC/USD"].enabled


def test_a_malformed_file_is_ignored(tmp_path):
    path = tmp_path / "gate.json"
    path.write_text("{not json", encoding="utf-8")
    out = resolve_trend_sleeve(["BTC/USD"], env={}, gate_path=str(path), now=NOW)
    assert not out["BTC/USD"].enabled


def test_the_agent_builds_engines_from_the_decisions(tmp_path, monkeypatch):
    path = tmp_path / "gate.json"
    report = _report(generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.delenv("HYDRA_TREND_SLEEVE", raising=False)
    monkeypatch.setenv("HYDRA_TREND_SLEEVE_GATE", str(path))
    monkeypatch.chdir(tmp_path)
    from hydra_agent import HydraAgent
    agent = HydraAgent(pairs=["BTC/USD", "ZEC/USD"], initial_balance=1000.0,
                       interval_seconds=1, duration_seconds=1, ws_port=0, demo=True)
    assert agent.engines["BTC/USD"].trend_sleeve is True
    assert agent.engines["ZEC/USD"].trend_sleeve is False
    assert agent.sleeve_decisions["ZEC/USD"].reason.startswith("ZEC gate")


def _demo_agent(monkeypatch, pairs, sleeve_env):
    monkeypatch.setenv("HYDRA_TREND_SLEEVE", sleeve_env)
    from hydra_agent import HydraAgent
    return HydraAgent(pairs=pairs, initial_balance=1000.0, interval_seconds=1,
                      duration_seconds=1, ws_port=0, demo=True)


def _buy_state(reason):
    return {"signal": {"action": "BUY", "confidence": 1.0, "reason": reason},
            "position": {"size": 0.0, "avg_entry": 0.0}, "price": 100.0}


def test_sleeve_pairs_bypass_the_brain_and_the_rules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _demo_agent(monkeypatch, ["BTC/USD"], "1")
    agent.brain = object()  # any configured brain
    guardrail_calls = []
    agent._apply_quant_guardrails = lambda pair, state, **kw: guardrail_calls.append(pair)
    states = {"BTC/USD": _buy_state("TREND_SLEEVE:enter|score=1.0")}
    all_states, brain_pairs = agent._route_phase2(states)
    assert brain_pairs == []
    assert all_states["BTC/USD"]["signal"]["action"] == "BUY"
    assert all_states["BTC/USD"]["decision_layer"] == "trend_sleeve"
    agent.brain = None
    all_states, brain_pairs = agent._route_phase2(
        {"BTC/USD": _buy_state("TREND_SLEEVE:enter|score=1.0")})
    assert guardrail_calls == [] and "BTC/USD" in all_states


def test_rails_pairs_still_get_the_brain_or_the_rules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _demo_agent(monkeypatch, ["BTC/USD"], "0")
    agent.brain = object()
    states = {"BTC/USD": _buy_state("Momentum entry")}
    all_states, brain_pairs = agent._route_phase2(states)
    assert [p for p, _ in brain_pairs] == ["BTC/USD"] and "BTC/USD" in all_states
    agent.brain = None
    calls = []
    agent._apply_quant_guardrails = lambda pair, state, **kw: calls.append(pair)
    all_states, brain_pairs = agent._route_phase2({"BTC/USD": _buy_state("Momentum entry")})
    assert calls == ["BTC/USD"] and brain_pairs == [] and "BTC/USD" in all_states
