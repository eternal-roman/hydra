"""--pairs auto portfolio discovery, per-quote balance pools, and the
derivatives-coverage contract that keeps R10 from strangling satellites.
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from hydra_agent import HydraAgent, discover_portfolio_pairs
from hydra_engine import HydraEngine
from hydra_kraken_cli import KrakenCLI
from hydra_quant_rules import apply_rules


# v2.29: three independent stable-quoted cores — the SOL triangle is no
# longer the default universe (90d real-tape studies found no SOL edge).
CORES = ["BTC/USD", "ETH/USD", "ZEC/USD"]


def _stub_kraken(monkeypatch, balance, constants):
    monkeypatch.setattr(KrakenCLI, "balance", staticmethod(lambda: balance))
    monkeypatch.setattr(
        KrakenCLI, "load_pair_constants",
        classmethod(lambda cls, pairs: {
            p: constants[p] for p in pairs if p in constants
        }),
    )


NIGHT_USD = {"price_decimals": 6, "ordermin": 25.0, "costmin": 0.5,
             "base": "NIGHT", "quote": "USD", "lot_decimals": 8}
NIGHT_USDC = {"price_decimals": 6, "ordermin": 25.0, "costmin": 0.5,
              "base": "NIGHT", "quote": "USDC", "lot_decimals": 8}
SOL_USDC = {"price_decimals": 2, "ordermin": 0.02, "costmin": 0.5,
            "base": "SOL", "quote": "USDC", "lot_decimals": 8}


def test_cores_only_when_nothing_extra_held(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"ZUSD": 500.0}, {})
    assert discover_portfolio_pairs("USD") == CORES


def test_no_sol_pair_in_default_universe(monkeypatch):
    """Regression guard for the v2.29 default flip: with no SOL held,
    the discovered universe must contain no SOL pair at all."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"ZUSD": 500.0, "XXBT": 0.1, "XETH": 2.0}, {})
    pairs = discover_portfolio_pairs("USD")
    assert not any(p.startswith("SOL/") or p.endswith("/SOL") for p in pairs)
    assert pairs == CORES


def test_held_sol_becomes_tradable_satellite(monkeypatch):
    """SOL is no longer a core, but held SOL is operational balance like
    any other asset — it spawns a satellite engine and rotates freely."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "SOL": 5.0},
        {"SOL/USD": {**SOL_USDC, "quote": "USD"}},
    )
    assert discover_portfolio_pairs("USD") == CORES + ["SOL/USD"]


def test_non_usd_quote_core_falls_back_when_unlisted(monkeypatch):
    """ZEC/USDC does not exist on Kraken — a USDC-quoted core set must
    swap the unlisted core to BASE/USD instead of seeding a dead pair."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"USDC": 500.0},
        {"BTC/USDC": {"price_decimals": 1, "ordermin": 0.0001, "costmin": 0.5,
                      "base": "BTC", "quote": "USDC", "lot_decimals": 8},
         "ETH/USDC": {"price_decimals": 2, "ordermin": 0.001, "costmin": 0.5,
                      "base": "ETH", "quote": "USDC", "lot_decimals": 8}},
    )
    assert discover_portfolio_pairs("USDC") == [
        "BTC/USDC", "ETH/USDC", "ZEC/USD"]


def test_balance_error_falls_back_to_cores(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"error": "EAPI:Rate limit"}, {})
    assert discover_portfolio_pairs("USD") == CORES


def test_usd_only_listing_resolves_to_usd(monkeypatch):
    """NIGHT has no USDC pair on Kraken — USD is essential."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "USDC": 100.0, "NIGHT": 500.0},
        {"NIGHT/USD": NIGHT_USD},
    )
    assert discover_portfolio_pairs("USD") == CORES + ["NIGHT/USD"]


BTC_USDC = {"price_decimals": 1, "ordermin": 0.0001, "costmin": 0.5,
            "base": "BTC", "quote": "USDC", "lot_decimals": 8}
ETH_USDC = {"price_decimals": 2, "ordermin": 0.001, "costmin": 0.5,
            "base": "ETH", "quote": "USDC", "lot_decimals": 8}
USDC_CORES = ["BTC/USDC", "ETH/USDC", "ZEC/USD"]


def test_usdc_preferred_when_funded(monkeypatch):
    """Both quotes listed + USDC held → USDC wins (idle USDC earns yield)."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"USDC": 100.0, "SOL": 1.0},
        {"SOL/USDC": SOL_USDC,
         "SOL/USD": {**SOL_USDC, "quote": "USD"},
         "BTC/USDC": BTC_USDC, "ETH/USDC": ETH_USDC},
    )
    assert discover_portfolio_pairs("USD") == USDC_CORES + ["SOL/USDC"]


def test_cores_follow_funded_stable_when_usd_empty(monkeypatch):
    """`--pairs auto` advertised USDC-if-funded, but cores stayed on the
    DEFAULT_QUOTE (USD). A USDC-only account then ran BTC/USD+ETH/USD+ZEC/USD
    at $0 cash — prices printed, sizer refused every BUY, dashboard looked
    dead. Cores must spend the stable that is actually held."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"USDC": 27068.32, "XXBT": 0.085},
        {"BTC/USDC": BTC_USDC, "ETH/USDC": ETH_USDC},
    )
    assert discover_portfolio_pairs("USD") == USDC_CORES


def test_cores_keep_usd_when_usd_funded(monkeypatch):
    """Requested USD stays when the USD pool can actually fund engines."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 500.0, "USDC": 27000.0},
        {"BTC/USDC": BTC_USDC, "ETH/USDC": ETH_USDC},
    )
    assert discover_portfolio_pairs("USD") == CORES


def test_usd_preferred_when_usdc_unfunded(monkeypatch):
    """USDC pair exists but no USDC held → a USDC engine could never buy;
    fund from the quote actually in the account."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "SOL": 1.0},
        {"SOL/USDC": SOL_USDC,
         "SOL/USD": {**SOL_USDC, "quote": "USD"}},
    )
    assert discover_portfolio_pairs("USD") == CORES + ["SOL/USD"]


def test_auto_quote_env_forces(monkeypatch):
    monkeypatch.setenv("HYDRA_AUTO_QUOTE", "USD")
    _stub_kraken(
        monkeypatch,
        {"USDC": 100.0, "SOL": 1.0},
        {"SOL/USDC": SOL_USDC,
         "SOL/USD": {**SOL_USDC, "quote": "USD"},
         "BTC/USDC": BTC_USDC, "ETH/USDC": ETH_USDC},
    )
    # Satellite quote is forced to USD; cores still follow the funded stable.
    assert discover_portfolio_pairs("USD") == USDC_CORES + ["SOL/USD"]


def test_staked_and_dust_excluded(monkeypatch):
    """Bonded holdings can't be sold; sub-ordermin holdings have no
    actionable pair. Neither spawns an engine."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "NIGHT.S": 900.0, "NIGHT": 10.0},  # ordermin 25
        {"NIGHT/USD": NIGHT_USD},
    )
    assert discover_portfolio_pairs("USD") == CORES


def test_unlisted_asset_skipped(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"ZUSD": 100.0, "WEIRDCOIN": 5.0}, {})
    assert discover_portfolio_pairs("USD") == CORES


# ─── R10 derivatives-coverage contract ─────────────────────────

def test_uncovered_pair_not_force_held_by_r10():
    """A satellite with no Kraken Futures mapping must not be structurally
    force-held just because funding/OI fields don't exist."""
    result = apply_rules(
        engine_action="BUY",
        quant_output={"positioning_bias": "", "force_hold": False},
        quant_indicators={"derivatives_covered": False,
                          "cvd_divergence_sigma": 0.4},
    )
    assert result.force_hold is False
    assert not any(f.rule_id == "R10" for f in result.triggered)


def test_covered_pair_with_null_fields_still_blacked_out():
    """Coverage is structural: a covered pair with a stale/warming stream
    keeps the R10 fail-safe."""
    result = apply_rules(
        engine_action="BUY",
        quant_output={"positioning_bias": "", "force_hold": False},
        quant_indicators={"funding_bps_8h": None, "oi_delta_1h_pct": None,
                          "oi_price_regime": None, "basis_apr_pct": None,
                          "cvd_divergence_sigma": None},
    )
    assert result.force_hold is True
    assert any(f.rule_id == "R10" for f in result.triggered)


# ─── per-quote balance pools ───────────────────────────────────

class _NullBalanceStream:
    healthy = False

    def latest_balances(self):
        return {}


def _mixed_quote_agent(cached_balance):
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.balance_stream = _NullBalanceStream()
    agent._cached_balance = cached_balance
    agent.pairs = ["SOL/USD", "BTC/USD", "ETH/USDC"]
    agent.engines = {}
    for pair, price in (("SOL/USD", 150.0), ("BTC/USD", 80000.0),
                        ("ETH/USDC", 3000.0)):
        eng = HydraEngine(initial_balance=0.0, asset=pair)
        eng.prices = [price]
        agent.engines[pair] = eng
    return agent


def test_per_quote_pools_fund_from_own_quote():
    """USD engines split the USD pool; the USDC engine gets the USDC pool.
    No engine is funded with money it cannot spend."""
    agent = _mixed_quote_agent({"ZUSD": 200.0, "USDC": 50.0})
    agent._set_engine_balances(per_pair_usd=999.0)  # legacy arg must be ignored live
    assert agent.engines["SOL/USD"].balance == 100.0   # 200 / 2 USD pairs
    assert agent.engines["BTC/USD"].balance == 100.0
    assert agent.engines["ETH/USDC"].balance == 50.0   # own pool
    for pair in agent.pairs:
        assert agent.engines[pair].tradable is True


def test_unfunded_quote_pool_seeds_zero_but_stays_sellable():
    """No USDC held → the USDC engine gets 0 balance (sizer refuses entries)
    but remains tradable so held inventory can still exit."""
    agent = _mixed_quote_agent({"ZUSD": 200.0})
    agent.engines["ETH/USDC"].position.size = 0.5
    agent.engines["ETH/USDC"].position.avg_entry = 2800.0
    agent._set_engine_balances(per_pair_usd=999.0)
    eth = agent.engines["ETH/USDC"]
    assert eth.balance == 0.0
    assert eth.tradable is True
    # Entry sizing collapses to zero without funds
    assert eth.sizer.calculate(0.9, eth.balance, 3000.0, "ETH/USDC") == 0.0


def test_unfunded_usd_pool_clears_constructor_dummy_peak():
    """Live first-seed with no USD cash must not inherit --balance/N as peak.

    That printed Eq $0 / DD 100% and armed the 15% BUY halt on tick 1
    for crypto-only (or USDC-only) accounts trading USD pairs.
    """
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.initial_balance = 100.0
    agent.balance_stream = _NullBalanceStream()
    agent._cached_balance = {"XXBT": 0.01, "XETH": 0.5, "XZEC": 2.0}
    agent.pairs = ["BTC/USD", "ETH/USD", "ZEC/USD"]
    agent.engines = {}
    for pair, px in (("BTC/USD", 63121.7), ("ETH/USD", 1883.82), ("ZEC/USD", 490.94)):
        eng = HydraEngine(initial_balance=100.0 / 3, asset=pair)
        eng.prices = [px]
        agent.engines[pair] = eng
        assert eng.peak_equity == pytest.approx(100.0 / 3)

    agent._set_engine_balances(per_pair_usd=100.0 / 3)
    for pair in agent.pairs:
        eng = agent.engines[pair]
        assert eng.balance == 0.0
        assert eng.peak_equity == 0.0
        assert eng.tradable is True
        # First tick must not report 100% DD / arm the breaker.
        equity = eng.balance + eng.position.size * eng.prices[-1]
        dd = ((eng.peak_equity - equity) / eng.peak_equity * 100) if eng.peak_equity > 0 else 0.0
        assert dd == 0.0


def test_unfunded_usd_pool_preserves_snapshot_peak():
    """A --resume peak above the constructor dummy is still never lowered."""
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.initial_balance = 100.0
    agent.balance_stream = _NullBalanceStream()
    agent._cached_balance = {"XXBT": 0.01}
    agent.pairs = ["BTC/USD"]
    eng = HydraEngine(initial_balance=100.0, asset="BTC/USD")
    eng.prices = [63000.0]
    eng.peak_equity = 5000.0
    eng.initial_balance = 5000.0
    agent.engines = {"BTC/USD": eng}
    agent._set_engine_balances(per_pair_usd=100.0)
    assert eng.balance == 0.0
    assert eng.peak_equity == 5000.0


def test_get_real_quote_balance_error_envelope_fails_open():
    """A truthy `{error: ...}` free-balance payload is not $0 cash."""
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.balance_stream = _NullBalanceStream()
    agent._cached_free_balance = {"error": "EAPI:Invalid key"}
    agent._cached_balance = {"ZUSD": 90.0}
    assert agent._get_real_quote_balance("USD") == 90.0
