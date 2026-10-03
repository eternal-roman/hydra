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


def test_free_balance_error_metadata_still_fails_open():
    """A real CLI envelope carries message/retryable. That is still not $0."""
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.balance_stream = _NullBalanceStream()
    agent.engines = {}
    agent._cached_free_balance = {
        "error": "auth",
        "message": "bad key",
        "error_category": "auth",
        "retryable": False,
    }
    agent._cached_balance = {"ZUSD": 90.0}
    assert agent._get_real_quote_balance("USD") == 90.0


def _spend_agent(stream):
    agent = object.__new__(HydraAgent)
    agent.paper = False
    agent.engines = {}
    agent.balance_stream = stream
    return agent


def test_fully_locked_free_balance_spends_zero_equity_stays_gross():
    """Hold covers the whole gross. Spend path is 0; equity still sees gross.

    The sizer must refuse the buy. Drawdown/equity keep the locked funds.
    """
    from hydra_streams import BalanceStream

    bs = BalanceStream()
    bs._on_message({
        "channel": "balances",
        "type": "snapshot",
        "data": [{
            "asset": "USD",
            "balance": 100.0,
            "hold_trade": 100.0,
            "asset_class": "currency",
        }],
    })
    bs.health_status = lambda: (True, "")
    agent = _spend_agent(bs)
    agent._cached_balance = {"ZUSD": 100.0}
    agent._cached_free_balance = {"ZUSD": 0.0}
    assert bs.latest_balances()["USD"] == 100.0
    assert bs.latest_free_balances() == {"USD": 0.0}
    assert agent._get_real_quote_balance("USD") == 0.0
    equity = agent._compute_balance_usd(agent._cached_balance)
    assert equity["total_usd"] == 100.0
    assert equity["tradable_usd"] == 100.0
    eng = HydraEngine(initial_balance=100.0, asset="BTC/USD")
    assert eng.sizer.calculate(
        0.9, agent._get_real_quote_balance("USD"), 60000.0, "BTC/USD",
    ) == 0.0


def test_cached_explicit_zero_free_does_not_fall_open():
    """Startup REST cache: asset present with free 0 is not missing data."""
    agent = _spend_agent(_NullBalanceStream())
    agent._cached_free_balance = {"ZUSD": 0.0}
    agent._cached_balance = {"ZUSD": 80.0}
    assert agent._get_real_quote_balance("USD") == 0.0
    assert agent._compute_balance_usd(agent._cached_balance)["total_usd"] == 80.0


def test_empty_free_map_after_successful_read_is_not_gross():
    """Empty free map because everything is on hold must not size against gross."""
    class _Stream:
        healthy = True

        def latest_free_balances(self):
            return {}

        def latest_balances(self):
            return {"USD": 100.0}

    stream = _Stream()
    agent = _spend_agent(stream)
    agent._cached_balance = {"ZUSD": 100.0}
    agent._cached_free_balance = None
    assert agent._get_real_quote_balance("USD") == 0.0
    assert agent._compute_balance_usd(stream.latest_balances())["total_usd"] == 100.0


def test_partial_free_update_does_not_zero_other_assets():
    """USD fully locked must not hide BTC the free update never mentioned."""
    class _Stream:
        healthy = True

        def latest_free_balances(self):
            return {"USD": 0.0}

        def latest_balances(self):
            return {"USD": 100.0, "BTC": 0.01}

        def free_view_is_complete(self):
            return False

    agent = _spend_agent(_Stream())
    agent._cached_free_balance = None
    agent._cached_balance = {}
    assert agent._get_real_quote_balance("USD") == 0.0
    assert agent._get_real_quote_balance("BTC") == pytest.approx(0.01)


def test_unknown_free_balance_falls_open_to_gross():
    """No free payload yet: spend the gross view (pre-v2.32 fail-open)."""
    class _Stream:
        healthy = True

        def latest_free_balances(self):
            return None

        def latest_balances(self):
            return {"USD": 40.0}

    agent = _spend_agent(_Stream())
    agent._cached_balance = {"ZUSD": 1.0}
    agent._cached_free_balance = None
    assert agent._get_real_quote_balance("USD") == 40.0


def test_resume_keeps_cash_for_open_position_and_seeds_flat():
    """Restored inventory keeps its cash. Flat books still get a seed.

    Free quote already excludes the resting buy's hold, and that hold's
    remaining cash is still inside the free pool. The flat engine is
    seeded from what is left, not from free/N of the whole pool.
    """
    agent = _mixed_quote_agent({"ZUSD": 90.0, "USDC": 50.0})
    sol = agent.engines["SOL/USD"]
    sol.balance = 60.0
    sol.position.size = 0.2
    sol.position.avg_entry = 150.0
    sol.peak_equity = 500.0
    agent.engines["BTC/USD"].balance = 999.0
    agent.engines["ETH/USDC"].balance = 7.0
    agent.order_journal = []
    agent._set_engine_balances(9999.0)
    assert sol.balance == 60.0
    assert sol.peak_equity == 500.0
    assert agent.engines["BTC/USD"].balance == pytest.approx(30.0)
    assert agent.engines["ETH/USDC"].balance == pytest.approx(50.0)
    assert agent.engines["BTC/USD"].tradable is True


def test_resume_keeps_cash_for_placed_order_without_position():
    """A working order locks the book even if the position is already flat."""
    agent = _mixed_quote_agent({"ZUSD": 90.0, "USDC": 30.0})
    btc = agent.engines["BTC/USD"]
    btc.balance = 10.0
    btc.position.size = 0.0
    agent.engines["SOL/USD"].balance = 999.0
    agent.order_journal = [{
        "pair": "btc/usd",
        "side": "BUY",
        "lifecycle": {"state": "PLACED"},
    }]
    agent._set_engine_balances(9999.0)
    assert btc.balance == 10.0
    assert agent.engines["SOL/USD"].balance == pytest.approx(80.0)
    assert agent.engines["ETH/USDC"].balance == pytest.approx(30.0)


def test_filled_journal_row_does_not_lock_resume_cash():
    agent = _mixed_quote_agent({"ZUSD": 90.0})
    agent.engines["SOL/USD"].balance = 999.0
    agent.engines["BTC/USD"].balance = 999.0
    agent.order_journal = [{
        "pair": "SOL/USD",
        "lifecycle": {"state": "FILLED"},
    }]
    agent._set_engine_balances(9999.0)
    assert agent.engines["SOL/USD"].balance == pytest.approx(45.0)
    assert agent.engines["BTC/USD"].balance == pytest.approx(45.0)


def _usd_agent(cash_by_pair, pool, restored=None, paper=False):
    """Flat USD engines whose cash came from a snapshot (``restored``)."""
    agent = object.__new__(HydraAgent)
    agent.paper = paper
    agent.initial_balance = 3000.0
    agent.balance_stream = _NullBalanceStream()
    agent._cached_balance = {"ZUSD": pool}
    agent.pairs = list(cash_by_pair)
    agent.engines = {}
    for pair, cash in cash_by_pair.items():
        eng = HydraEngine(initial_balance=1000.0, asset=pair)
        eng.prices = [100.0]
        eng.balance = cash
        eng.peak_equity = cash
        agent.engines[pair] = eng
    agent._snapshot_restored_pairs = set(cash_by_pair if restored is None else restored)
    return agent


def _cash(agent):
    return {p: round(e.balance, 6) for p, e in agent.engines.items()}


def test_a_restart_keeps_each_restored_flat_books_cash():
    """A sleeve that banked gains while its siblings sat flat keeps them.

    The equal re-split gave each engine 1100: BTC came back 15.4% under its
    own 1300 peak, its breaker halted it, and the reset could not clear it
    because the next boot split the same way.
    """
    agent = _usd_agent({"BTC/USD": 1300.0, "ETH/USD": 1000.0, "ZEC/USD": 1000.0}, 3300.0)
    agent._set_engine_balances(per_pair_usd=1100.0)
    assert _cash(agent) == {"BTC/USD": 1300.0, "ETH/USD": 1000.0, "ZEC/USD": 1000.0}
    btc = agent.engines["BTC/USD"]
    assert btc.peak_equity == pytest.approx(1300.0)
    assert btc.balance == pytest.approx(btc.peak_equity)   # no fake drawdown


def test_the_real_pool_still_sets_the_total():
    books = {"BTC/USD": 1300.0, "ETH/USD": 1000.0, "ZEC/USD": 1000.0}
    deposit = _usd_agent(books, 3600.0)      # +300 is shared equally
    deposit._set_engine_balances(per_pair_usd=1200.0)
    assert _cash(deposit) == {"BTC/USD": 1400.0, "ETH/USD": 1100.0, "ZEC/USD": 1100.0}
    shortfall = _usd_agent(books, 2970.0)    # 10% less shrinks every book by 10%
    shortfall._set_engine_balances(per_pair_usd=990.0)
    assert _cash(shortfall) == {"BTC/USD": 1170.0, "ETH/USD": 900.0, "ZEC/USD": 900.0}


def test_a_pair_new_this_session_shares_only_the_surplus():
    books = {"BTC/USD": 1300.0, "ETH/USD": 1000.0, "ZEC/USD": 1000.0}
    restored = ["BTC/USD", "ETH/USD"]        # ZEC holds the constructor placeholder
    agent = _usd_agent(books, 2300.0, restored=restored)
    agent._set_engine_balances(per_pair_usd=766.0)
    assert _cash(agent) == {"BTC/USD": 1300.0, "ETH/USD": 1000.0, "ZEC/USD": 0.0}
    agent = _usd_agent(books, 2600.0, restored=restored)
    agent._set_engine_balances(per_pair_usd=866.0)
    assert _cash(agent) == {"BTC/USD": 1400.0, "ETH/USD": 1100.0, "ZEC/USD": 100.0}


def test_a_fresh_start_still_splits_the_pool_equally():
    agent = _usd_agent({"BTC/USD": 1000.0, "ETH/USD": 1000.0, "ZEC/USD": 1000.0},
                       3300.0, restored=[])
    agent._set_engine_balances(per_pair_usd=1100.0)
    assert _cash(agent) == {"BTC/USD": 1100.0, "ETH/USD": 1100.0, "ZEC/USD": 1100.0}


def test_paper_resume_keeps_its_books():
    """Paper has no exchange balance: the restored books are the truth, so a
    resume no longer resets every flat book to the constructor split."""
    books = {"BTC/USD": 1300.0, "ETH/USD": 950.0, "ZEC/USD": 1000.0}
    agent = _usd_agent(books, 0.0, restored=["BTC/USD", "ETH/USD"], paper=True)
    agent._set_engine_balances(per_pair_usd=1000.0)
    assert _cash(agent) == {"BTC/USD": 1300.0, "ETH/USD": 950.0, "ZEC/USD": 1000.0}
