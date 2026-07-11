"""PR-E: quant kill switch, R2/R3 priority, USDT map."""
from __future__ import annotations

import os

import pytest

from hydra_derivatives_stream import SPOT_TO_DERIVATIVES
from hydra_engine import CrossPairCoordinator


def test_usdt_pairs_in_derivatives_map():
    assert "BTC/USDT" in SPOT_TO_DERIVATIVES
    assert "SOL/USDT" in SPOT_TO_DERIVATIVES
    assert SPOT_TO_DERIVATIVES["BTC/USDT"]["perp"] == "PF_XBTUSD"


def test_rule2_recovery_not_overwritten_by_rule3():
    """BTC TREND_UP + SOL TREND_DOWN + bridge TREND_UP: prefer Rule 2 BUY ADJUST."""
    pairs = ["SOL/USD", "SOL/BTC", "BTC/USD"]
    coord = CrossPairCoordinator(pairs)
    states = {
        "BTC/USD": {
            "regime": "TREND_UP",
            "signal": {"action": "BUY", "confidence": 0.7},
            "position": {"size": 0.0},
            "tradable": True,
        },
        "SOL/USD": {
            "regime": "TREND_DOWN",
            "signal": {"action": "SELL", "confidence": 0.6},
            "position": {"size": 1.0},
            "tradable": True,
        },
        "SOL/BTC": {
            "regime": "TREND_UP",
            "signal": {"action": "BUY", "confidence": 0.7},
            "position": {"size": 0.0},
            "tradable": False,  # unfunded bridge
        },
    }
    ov = coord.get_overrides(states)
    sol = ov.get("SOL/USD")
    assert sol is not None
    assert sol["signal"] == "BUY"
    assert sol["action"] == "ADJUST"
    assert "swap" not in sol


def test_rule3_requires_bridge_tradable():
    pairs = ["SOL/USD", "SOL/BTC", "BTC/USD"]
    coord = CrossPairCoordinator(pairs)
    states = {
        "BTC/USD": {
            "regime": "RANGING",
            "signal": {"action": "HOLD", "confidence": 0.5},
            "position": {"size": 0.0},
            "tradable": True,
        },
        "SOL/USD": {
            "regime": "TREND_DOWN",
            "signal": {"action": "SELL", "confidence": 0.6},
            "position": {"size": 1.0},
            "tradable": True,
        },
        "SOL/BTC": {
            "regime": "TREND_UP",
            "signal": {"action": "BUY", "confidence": 0.7},
            "position": {"size": 0.0},
            "tradable": False,
        },
    }
    ov = coord.get_overrides(states)
    # No Rule 2 (btc not TREND_UP), Rule 3 blocked by untradable bridge
    sol = ov.get("SOL/USD")
    assert sol is None or "swap" not in (sol or {})
