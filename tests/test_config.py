"""Tests for hydra_config — role-bound trading triangle and runtime config.

Coverage:
  - TradingTriangle role bindings for USD and USDC quotes
  - HydraConfig.from_quote builds a coherent triangle
  - Triangle quote consistency (stable_sol.quote == stable_btc.quote)
  - Bridge is always the cross-asset pair (SOL/BTC)
  - Unknown / unsupported quote raises informative error
  - HYDRA_QUOTE environment override
  - --quote CLI arg parsing
  - argparse defaults match policy (USD)
  - pairs property returns exactly the three triangle pairs
  - HydraConfig is hashable / immutable
"""
import os
import argparse
import pytest

from hydra_pair_registry import default_registry
from hydra_config import (
    TradingTriangle,
    HydraConfig,
    add_config_args,
    DEFAULT_QUOTE,
)


# ─── Defaults policy ───

def test_default_quote_is_usd():
    """v2.19 ships USD as the default stable quote."""
    assert DEFAULT_QUOTE == "USD"


# ─── TradingTriangle ───

def test_triangle_for_usd():
    reg = default_registry()
    cfg = HydraConfig.from_quote("USD", registry=reg)
    t = cfg.triangle
    assert t.stable_sol.cli_format == "SOL/USD"
    assert t.stable_btc.cli_format == "BTC/USD"
    assert t.bridge.cli_format == "SOL/BTC"
    assert t.quote == "USD"


def test_triangle_for_usdc():
    reg = default_registry()
    cfg = HydraConfig.from_quote("USDC", registry=reg)
    t = cfg.triangle
    assert t.stable_sol.cli_format == "SOL/USDC"
    assert t.stable_btc.cli_format == "BTC/USDC"
    assert t.bridge.cli_format == "SOL/BTC"
    assert t.quote == "USDC"


def test_triangle_for_usdt():
    """USDT is a first-class stable quote — fallback catalog must build a triangle."""
    reg = default_registry()
    cfg = HydraConfig.from_quote("USDT", registry=reg)
    t = cfg.triangle
    assert t.stable_sol.cli_format == "SOL/USDT"
    assert t.stable_btc.cli_format == "BTC/USDT"
    assert t.bridge.cli_format == "SOL/BTC"
    assert t.quote == "USDT"


def test_triangle_quote_consistency():
    """The two stable-quoted legs must share the same quote currency."""
    reg = default_registry()
    for q in ("USD", "USDC", "USDT"):
        cfg = HydraConfig.from_quote(q, registry=reg)
        assert cfg.triangle.stable_sol.quote == cfg.triangle.stable_btc.quote == q


def test_triangle_bridge_is_always_sol_btc():
    reg = default_registry()
    for q in ("USD", "USDC", "USDT"):
        cfg = HydraConfig.from_quote(q, registry=reg)
        assert cfg.triangle.bridge.base == "SOL"
        assert cfg.triangle.bridge.quote == "BTC"


def test_triangle_is_frozen():
    reg = default_registry()
    cfg = HydraConfig.from_quote("USD", registry=reg)
    with pytest.raises((AttributeError, Exception)):
        cfg.triangle.quote = "USDC"


# ─── HydraConfig ───

def test_config_pairs_are_triangle_pairs():
    reg = default_registry()
    cfg = HydraConfig.from_quote("USD", registry=reg)
    assert set(p.cli_format for p in cfg.pairs) == {"SOL/USD", "BTC/USD", "SOL/BTC"}


def test_config_primary_quote_alias():
    reg = default_registry()
    cfg = HydraConfig.from_quote("USD", registry=reg)
    assert cfg.primary_quote == "USD" == cfg.quote


def test_config_unsupported_quote_raises():
    reg = default_registry()
    with pytest.raises(ValueError) as exc:
        HydraConfig.from_quote("EUR", registry=reg)
    assert "EUR" in str(exc.value)


def test_config_unknown_quote_raises():
    reg = default_registry()
    with pytest.raises(ValueError):
        HydraConfig.from_quote("XYZ", registry=reg)


def test_config_quote_normalized_to_upper():
    reg = default_registry()
    cfg = HydraConfig.from_quote("usd", registry=reg)
    assert cfg.quote == "USD"
    assert cfg.triangle.stable_sol.cli_format == "SOL/USD"


def test_config_default_registry_when_omitted():
    """If no registry passed, build one from the static catalog."""
    cfg = HydraConfig.from_quote("USD")
    assert cfg.registry is not None
    assert cfg.registry.get("SOL/USD") is not None


def test_config_pair_symbols_helper():
    """Convenience: list of cli_format strings for the triangle pairs."""
    reg = default_registry()
    cfg = HydraConfig.from_quote("USD", registry=reg)
    assert cfg.pair_symbols() == ("SOL/USD", "BTC/USD", "SOL/BTC")


# ─── CLI arg parsing ───

def test_add_config_args_default_usd():
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args([])
    assert args.quote == "USD"


def test_add_config_args_explicit_usdc():
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args(["--quote", "USDC"])
    assert args.quote == "USDC"


def test_config_from_args():
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args(["--quote", "USDC"])
    cfg = HydraConfig.from_args(args)
    assert cfg.quote == "USDC"
    assert cfg.triangle.stable_sol.cli_format == "SOL/USDC"


# ─── Env override ───

def test_hydra_quote_env_override(monkeypatch):
    monkeypatch.setenv("HYDRA_QUOTE", "USDC")
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args([])
    cfg = HydraConfig.from_args(args)
    assert cfg.quote == "USDC"  # env wins over default


def test_hydra_quote_cli_beats_env(monkeypatch):
    """Explicit --quote on the CLI overrides HYDRA_QUOTE env."""
    monkeypatch.setenv("HYDRA_QUOTE", "USDC")
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args(["--quote", "USD"])
    cfg = HydraConfig.from_args(args)
    assert cfg.quote == "USD"


def test_no_env_no_cli_uses_default(monkeypatch):
    monkeypatch.delenv("HYDRA_QUOTE", raising=False)
    p = argparse.ArgumentParser()
    add_config_args(p)
    args = p.parse_args([])
    cfg = HydraConfig.from_args(args)
    assert cfg.quote == DEFAULT_QUOTE == "USD"
