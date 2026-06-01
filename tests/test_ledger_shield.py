"""Tests for the hard BTC ledger-shield guard.

The ledger shield is a capital-preservation floor: the live SELL path must
never drop BTC holdings below `HYDRA_LEDGER_SHIELD_BTC`. Unlike the prior
thesis-layer implementation (an advisory string the LLM brain could ignore),
this is a deterministic clamp/skip enforced in `_place_order`.

The real floor lives only in the operator's gitignored `.env` — no holdings
value is hardcoded in source (opsec). When the env var is unset/0/invalid the
shield is DISABLED (returns None → no constraint), and the agent warns loudly
at startup.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import ledger_shield_sellable, read_ledger_shield_btc  # noqa: E402


# ─── read_ledger_shield_btc(): env parsing ───────────────────────────────

def test_shield_env_unset_is_disabled(monkeypatch):
    monkeypatch.delenv("HYDRA_LEDGER_SHIELD_BTC", raising=False)
    assert read_ledger_shield_btc() == 0.0


def test_shield_env_value_parsed(monkeypatch):
    monkeypatch.setenv("HYDRA_LEDGER_SHIELD_BTC", "0.20")
    assert read_ledger_shield_btc() == 0.20


def test_shield_env_blank_is_disabled(monkeypatch):
    monkeypatch.setenv("HYDRA_LEDGER_SHIELD_BTC", "   ")
    assert read_ledger_shield_btc() == 0.0


def test_shield_env_invalid_is_disabled(monkeypatch):
    monkeypatch.setenv("HYDRA_LEDGER_SHIELD_BTC", "not-a-number")
    assert read_ledger_shield_btc() == 0.0


def test_shield_env_negative_is_disabled(monkeypatch):
    monkeypatch.setenv("HYDRA_LEDGER_SHIELD_BTC", "-1.0")
    assert read_ledger_shield_btc() == 0.0


# ─── ledger_shield_sellable(): the pure clamp logic ──────────────────────

def test_disabled_shield_imposes_no_constraint():
    # shield 0 → None (caller treats None as "no shield")
    assert ledger_shield_sellable(0.0, "BTC", "USD", 1.0) is None


def test_non_btc_base_not_constrained():
    # selling SOL for BTC (the bridge) must never be shielded — it INCREASES BTC
    assert ledger_shield_sellable(0.20, "SOL", "BTC", 5.0) is None


def test_btc_sold_for_non_stable_not_constrained():
    # shield only guards stable-quoted BTC sells; a hypothetical BTC/ETH is N/A
    assert ledger_shield_sellable(0.20, "BTC", "ETH", 1.0) is None


def test_btc_usd_sell_clamped_to_floor():
    # 1.0 BTC, floor 0.20 → at most 0.80 BTC sellable
    assert ledger_shield_sellable(0.20, "BTC", "USD", 1.0) == 0.80


def test_btc_at_floor_yields_zero_sellable():
    # holdings already below floor → 0.0 sellable (caller blocks the sell)
    assert ledger_shield_sellable(0.20, "BTC", "USD", 0.15) == 0.0


def test_btc_exactly_at_floor_yields_zero():
    assert ledger_shield_sellable(0.20, "BTC", "USD", 0.20) == 0.0


def test_usdc_and_usdt_quotes_also_guarded():
    assert ledger_shield_sellable(0.20, "BTC", "USDC", 1.0) == 0.80
    assert ledger_shield_sellable(0.20, "BTC", "USDT", 1.0) == 0.80


def test_xbt_alias_normalizes_to_btc():
    # Kraken's XBT alias must resolve to BTC so the shield still fires
    assert ledger_shield_sellable(0.20, "XBT", "USD", 1.0) == 0.80
