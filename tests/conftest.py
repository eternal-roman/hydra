"""Shared test isolation.

HYDRA_TREND_SLEEVE=auto (the default) reads the operator's gate report from
research/data/trend_sleeve_gate.json. After run_strategy_gate.bat writes a
PASS there, every HydraAgent a test builds would trade the sleeve, which
skips the brain-free guardrail path several tests pin. Tests therefore see
"auto with no report" unless they set these variables themselves.
"""
import os

import pytest

_NO_GATE_REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "_no_trend_sleeve_gate_report.json")


@pytest.fixture(autouse=True)
def _no_operator_gate_report(monkeypatch):
    monkeypatch.setenv("HYDRA_TREND_SLEEVE", "auto")
    monkeypatch.setenv("HYDRA_TREND_SLEEVE_GATE", _NO_GATE_REPORT)
