"""--pairs auto portfolio discovery, per-quote balance pools, and the
derivatives-coverage contract that keeps R10 from strangling satellites.
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_agent import HydraAgent, discover_portfolio_pairs
from hydra_engine import HydraEngine
from hydra_kraken_cli import KrakenCLI
from hydra_quant_rules import apply_rules


TRIANGLE = ["SOL/USD", "SOL/BTC", "BTC/USD"]


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
ETH_USDC = {"price_decimals": 2, "ordermin": 0.002, "costmin": 0.5,
            "base": "ETH", "quote": "USDC", "lot_decimals": 8}


def test_triangle_only_when_nothing_extra_held(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"ZUSD": 500.0}, {})
    assert discover_portfolio_pairs("USD") == TRIANGLE


def test_balance_error_falls_back_to_triangle(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"error": "EAPI:Rate limit"}, {})
    assert discover_portfolio_pairs("USD") == TRIANGLE


def test_usd_only_listing_resolves_to_usd(monkeypatch):
    """NIGHT has no USDC pair on Kraken — USD is essential."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "USDC": 100.0, "NIGHT": 500.0},
        {"NIGHT/USD": NIGHT_USD},
    )
    assert discover_portfolio_pairs("USD") == TRIANGLE + ["NIGHT/USD"]


def test_usdc_preferred_when_funded(monkeypatch):
    """Both quotes listed + USDC held → USDC wins (idle USDC earns yield)."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"USDC": 100.0, "ETH": 1.0},
        {"ETH/USDC": ETH_USDC,
         "ETH/USD": {**ETH_USDC, "quote": "USD"}},
    )
    assert discover_portfolio_pairs("USD") == TRIANGLE + ["ETH/USDC"]


def test_usd_preferred_when_usdc_unfunded(monkeypatch):
    """USDC pair exists but no USDC held → a USDC engine could never buy;
    fund from the quote actually in the account."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "ETH": 1.0},
        {"ETH/USDC": ETH_USDC,
         "ETH/USD": {**ETH_USDC, "quote": "USD"}},
    )
    assert discover_portfolio_pairs("USD") == TRIANGLE + ["ETH/USD"]


def test_auto_quote_env_forces(monkeypatch):
    monkeypatch.setenv("HYDRA_AUTO_QUOTE", "USD")
    _stub_kraken(
        monkeypatch,
        {"USDC": 100.0, "ETH": 1.0},
        {"ETH/USDC": ETH_USDC,
         "ETH/USD": {**ETH_USDC, "quote": "USD"}},
    )
    assert discover_portfolio_pairs("USD") == TRIANGLE + ["ETH/USD"]


def test_staked_and_dust_excluded(monkeypatch):
    """Bonded holdings can't be sold; sub-ordermin holdings have no
    actionable pair. Neither spawns an engine."""
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(
        monkeypatch,
        {"ZUSD": 100.0, "NIGHT.S": 900.0, "NIGHT": 10.0},  # ordermin 25
        {"NIGHT/USD": NIGHT_USD},
    )
    assert discover_portfolio_pairs("USD") == TRIANGLE


def test_unlisted_asset_skipped(monkeypatch):
    monkeypatch.delenv("HYDRA_AUTO_QUOTE", raising=False)
    _stub_kraken(monkeypatch, {"ZUSD": 100.0, "WEIRDCOIN": 5.0}, {})
    assert discover_portfolio_pairs("USD") == TRIANGLE


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
