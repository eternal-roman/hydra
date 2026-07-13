"""
HYDRA Balance & Asset Conversion Test Suite
Validates staked asset detection, asset name normalization,
USD conversion, and engine balance initialization from exchange data.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import HydraAgent
from hydra_kraken_cli import KrakenCLI
from hydra_engine import HydraEngine


# ═══════════════════════════════════════════════════════════════
# TEST: Staked asset detection
# ═══════════════════════════════════════════════════════════════

class TestStakedAssets:
    def test_bonded_suffix_detected(self):
        assert KrakenCLI._is_staked("XBT.B") is True

    def test_staked_suffix_detected(self):
        assert KrakenCLI._is_staked("SOL.S") is True

    def test_margin_suffix_detected(self):
        assert KrakenCLI._is_staked("ETH.M") is True

    def test_plain_asset_not_staked(self):
        assert KrakenCLI._is_staked("XBT") is False

    def test_usdc_not_staked(self):
        assert KrakenCLI._is_staked("USDC") is False

    def test_asset_with_dot_in_middle_not_staked(self):
        """Only trailing suffixes count — 'B.XBT' should not match."""
        assert KrakenCLI._is_staked("B.XBT") is False

    def test_single_letter_asset_not_false_positive(self):
        """Asset name 'B' alone should not be considered staked."""
        assert KrakenCLI._is_staked("B") is False

    def test_empty_string_not_staked(self):
        assert KrakenCLI._is_staked("") is False


# ═══════════════════════════════════════════════════════════════
# TEST: Asset name normalization
# ═══════════════════════════════════════════════════════════════

class TestNormalizeAsset:
    def test_xxbt_normalizes_to_btc(self):
        assert KrakenCLI._normalize_asset("XXBT") == "BTC"

    def test_xbt_normalizes_to_btc(self):
        assert KrakenCLI._normalize_asset("XBT") == "BTC"

    def test_btc_passes_through(self):
        assert KrakenCLI._normalize_asset("BTC") == "BTC"

    def test_usdc_passes_through(self):
        assert KrakenCLI._normalize_asset("USDC") == "USDC"

    def test_zusdc_normalizes(self):
        assert KrakenCLI._normalize_asset("ZUSDC") == "USDC"

    def test_zusd_normalizes(self):
        assert KrakenCLI._normalize_asset("ZUSD") == "USD"

    def test_sol_passes_through(self):
        assert KrakenCLI._normalize_asset("SOL") == "SOL"

    def test_xsol_normalizes(self):
        assert KrakenCLI._normalize_asset("XSOL") == "SOL"

    def test_staked_suffix_stripped_then_normalized(self):
        """XBT.B → strip .B → XBT → normalize to BTC."""
        assert KrakenCLI._normalize_asset("XBT.B") == "BTC"

    def test_staked_xxbt_suffix_stripped_then_normalized(self):
        """XXBT.B → strip .B → XXBT → normalize to BTC."""
        assert KrakenCLI._normalize_asset("XXBT.B") == "BTC"

    def test_sol_staked_normalizes(self):
        assert KrakenCLI._normalize_asset("SOL.S") == "SOL"

    def test_unknown_asset_passes_through(self):
        assert KrakenCLI._normalize_asset("DOGE") == "DOGE"


# ═══════════════════════════════════════════════════════════════
# TEST: USD balance computation
# ═══════════════════════════════════════════════════════════════

class TestComputeBalanceUsd:
    """Tests _compute_balance_usd using a minimal HydraAgent with mocked engines."""

    def _make_agent(self):
        """Create a HydraAgent-like object with engines that have known prices."""
        agent = object.__new__(HydraAgent)
        agent.engines = {}

        # Create engines with known prices (no full init, just set prices)
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 84000.0), ("SOL/BTC", 0.001547)]:
            engine = object.__new__(HydraEngine)
            engine.prices = [price]
            agent.engines[pair] = engine

        return agent

    def test_usdc_valued_at_one_dollar(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"USDC": 500.0})
        assert result["total_usd"] == 500.0
        assert result["tradable_usd"] == 500.0
        assert result["staked_usd"] == 0

    def test_sol_converted_using_engine_price(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"SOL": 10.0})
        assert result["total_usd"] == 1300.0  # 10 * 130

    def test_btc_converted_using_engine_price(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"BTC": 1.0})
        assert result["total_usd"] == 84000.0

    def test_staked_excluded_from_tradable(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC": 1.0,
            "BTC.B": 0.5,
            "USDC": 100.0,
        })
        # Total includes staked: 84000 + 42000 + 100 = 126100
        assert result["total_usd"] == 126100.0
        # Tradable excludes staked: 84000 + 100 = 84100
        assert result["tradable_usd"] == 84100.0
        # Staked: 0.5 * 84000 = 42000
        assert result["staked_usd"] == 42000.0

    def test_multiple_staked_assets(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC.B": 0.5,
            "SOL.S": 5.0,
        })
        expected_staked = 0.5 * 84000.0 + 5.0 * 130.0  # 42650
        assert result["staked_usd"] == expected_staked
        assert result["tradable_usd"] == 0

    def test_unknown_asset_valued_at_zero(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"DOGE": 1000.0})
        assert result["total_usd"] == 0
        # Asset still appears in breakdown
        assert len(result["assets"]) == 1
        assert result["assets"][0]["usd_value"] == 0

    def test_empty_balance_returns_zeros(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({})
        assert result["total_usd"] == 0
        assert result["tradable_usd"] == 0
        assert result["staked_usd"] == 0
        assert result["assets"] == []

    def test_assets_sorted_tradable_first(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC.B": 0.5,
            "USDC": 100.0,
            "SOL": 10.0,
        })
        assets = result["assets"]
        # Tradable assets first (SOL, USDC alphabetical), then staked (BTC.B)
        assert assets[0]["asset"] == "SOL"
        assert assets[0]["staked"] is False
        assert assets[1]["asset"] == "USDC"
        assert assets[1]["staked"] is False
        assert assets[2]["asset"] == "BTC.B"
        assert assets[2]["staked"] is True

    def test_xxbt_normalized_for_price_lookup(self):
        """Kraken returns 'XXBT' — should normalize and find BTC price."""
        agent = self._make_agent()
        result = agent._compute_balance_usd({"XXBT": 1.0})
        assert result["total_usd"] == 84000.0

    def test_staked_xxbt_normalized_for_price_lookup(self):
        """XXBT.B should strip .B, normalize XXBT→BTC, use BTC price."""
        agent = self._make_agent()
        result = agent._compute_balance_usd({"XXBT.B": 1.0})
        assert result["total_usd"] == 84000.0

    def test_usdc_flex_earn_counted_in_total(self):
        """Regression (v2.16.2): Kraken earn-flex products use the '.F'
        suffix. Before .F joined STAKED_SUFFIXES the normalizer left the
        asset as 'USDC.F' — which is not a price-table key — so the
        balance silently valued at $0 in the dashboard history chart."""
        agent = self._make_agent()
        result = agent._compute_balance_usd({"USDC.F": 500.0})
        assert result["total_usd"] == 500.0
        # Earn-flex is instant-redeem but not directly tradable for
        # limit-post-only orders, so it counts as staked.
        assert result["staked_usd"] == 500.0
        assert result["tradable_usd"] == 0.0

    def test_flex_suffix_detected_as_staked(self):
        assert KrakenCLI._is_staked("USDC.F") is True
        assert KrakenCLI._is_staked("BTC.F") is True

    def test_flex_suffix_stripped_in_normalize(self):
        assert KrakenCLI._normalize_asset("USDC.F") == "USDC"
        assert KrakenCLI._normalize_asset("XXBT.F") == "BTC"


# ═══════════════════════════════════════════════════════════════
# TEST: Asset price derivation
# ═══════════════════════════════════════════════════════════════

class TestGetAssetPrices:
    def test_usdc_always_one(self):
        agent = object.__new__(HydraAgent)
        agent.engines = {}
        prices = agent._get_asset_prices()
        assert prices["USDC"] == 1.0
        assert prices["USD"] == 1.0

    def test_prices_from_usdc_pairs(self):
        agent = object.__new__(HydraAgent)
        agent.engines = {}
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 84000.0)]:
            engine = object.__new__(HydraEngine)
            engine.prices = [price]
            agent.engines[pair] = engine
        prices = agent._get_asset_prices()
        assert prices["SOL"] == 130.0
        assert prices["BTC"] == 84000.0

    def test_btc_derived_from_sol_btc_when_no_direct_pair(self):
        """If BTC/USDC engine has no prices, derive BTC from SOL/USDC and SOL/BTC."""
        agent = object.__new__(HydraAgent)
        agent.engines = {}

        sol_usdc = object.__new__(HydraEngine)
        sol_usdc.prices = [130.0]
        agent.engines["SOL/USDC"] = sol_usdc

        sol_btc = object.__new__(HydraEngine)
        sol_btc.prices = [0.001547]  # 1 SOL = 0.001547 BTC
        agent.engines["SOL/BTC"] = sol_btc

        btc_usdc = object.__new__(HydraEngine)
        btc_usdc.prices = []  # No data yet
        agent.engines["BTC/USDC"] = btc_usdc

        prices = agent._get_asset_prices()
        # BTC = SOL_USD / SOL_BTC = 130 / 0.001547 ≈ 84034
        assert abs(prices["BTC"] - 130.0 / 0.001547) < 1.0

    def test_empty_engine_prices_skipped(self):
        agent = object.__new__(HydraAgent)
        engine = object.__new__(HydraEngine)
        engine.prices = []
        agent.engines = {"SOL/USDC": engine}
        prices = agent._get_asset_prices()
        assert "SOL" not in prices


# ═══════════════════════════════════════════════════════════════
# TEST: Engine balance initialization from exchange
# ═══════════════════════════════════════════════════════════════

class TestEngineBalanceInit:
    def test_engine_balance_overwritten_by_tradable_balance(self):
        """Simulates the startup flow: engines start with CLI default,
        then get overwritten with real exchange balance."""
        # Create engines with default $100 balance (as CLI arg would)
        engines = {}
        pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        for pair in pairs:
            engine = HydraEngine(initial_balance=33.33, asset=pair)
            engines[pair] = engine

        # Simulate what run() does: overwrite with real balance
        tradable_usd = 1500.0
        per_pair = tradable_usd / len(pairs)
        for pair in pairs:
            engine = engines[pair]
            engine.initial_balance = per_pair
            engine.balance = per_pair
            engine.peak_equity = per_pair

        # Verify all engines updated
        for pair in pairs:
            assert engines[pair].balance == 500.0
            assert engines[pair].initial_balance == 500.0
            assert engines[pair].peak_equity == 500.0

    def test_engine_position_sizing_uses_real_balance(self):
        """With real balance ($500), position sizer should produce tradeable sizes."""
        engine = HydraEngine(initial_balance=500.0, asset="SOL/USDC")
        # At confidence 0.7, Kelly edge = 0.4, quarter-Kelly = 0.10
        # Position value = 0.10 * 500 = $50, size = 50/130 ≈ 0.38 SOL
        size = engine.sizer.calculate(0.7, engine.balance, 130.0, "SOL/USDC")
        assert size > 0, "Real balance should produce tradeable position size"
        assert size >= 0.1, "Position should meet SOL minimum order size (0.1)"

    def test_tiny_balance_produces_zero_size(self):
        """With a very small balance, position sizer can't meet exchange minimums."""
        engine = HydraEngine(initial_balance=10.0, asset="SOL/USDC")
        # At confidence 0.7: edge=0.4, quarter-Kelly=0.10, value=$1.00,
        # size = 1.0/130 ≈ 0.008 — below SOL ordermin of 0.02
        size = engine.sizer.calculate(0.7, engine.balance, 130.0, "SOL/USDC")
        assert size == 0, "Tiny balance should fail to meet minimum order size"

    class _NullBalanceStream:
        """Stub BalanceStream for tests — declares unhealthy so
        _get_real_quote_balance falls through to _cached_balance."""
        healthy = False
        def latest_balances(self):  # pragma: no cover — never called
            return {}

    def _bare_agent_for_balance_tests(self, cached_balance: dict,
                                       pair_prices: dict,
                                       paper: bool = False) -> HydraAgent:
        """Construct a bare HydraAgent skeleton wired for _set_engine_balances.

        v2.11.0 routes non-USD-quoted pairs through the real-holding path in
        LIVE mode (paper=False) so we test the info-only transitions here.
        Paper mode preserves the pre-v2.11.0 USD→quote conversion path for
        backward compatibility with paper-only strategy simulations.
        """
        agent = object.__new__(HydraAgent)
        agent.paper = paper
        agent.balance_stream = self._NullBalanceStream()
        agent._cached_balance = cached_balance
        agent.pairs = list(pair_prices.keys())
        agent.engines = {}
        for pair, price in pair_prices.items():
            engine = HydraEngine(initial_balance=0.0, asset=pair)
            engine.prices = [price]
            agent.engines[pair] = engine
        return agent

    def test_btc_quoted_balance_uses_real_holding_when_btc_held(self, monkeypatch):
        monkeypatch.setenv("HYDRA_BRIDGE_TRADING", "1")  # legacy opt-in path
        """v2.11.0: SOL/BTC engine balance = real BTC holding, not a USD
        conversion. tradable=True when the holding exceeds costmin."""
        real_btc = 0.00300
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0, "XXBT": real_btc},
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent._set_engine_balances(per_pair_usd=100.0)

        # Per-quote pools (v2.28): the real 100 USDC pool splits across the
        # two USDC-quoted pairs — a uniform 100-each slice would let the
        # engines double-spend the account.
        assert agent.engines["SOL/USDC"].balance == 50.0
        assert agent.engines["SOL/USDC"].tradable is True
        assert agent.engines["BTC/USDC"].balance == 50.0
        assert agent.engines["BTC/USDC"].tradable is True

        # SOL/BTC: real BTC holding, not a USD-derived phantom
        sol_btc = agent.engines["SOL/BTC"]
        assert abs(sol_btc.balance - real_btc) < 1e-12, \
            f"SOL/BTC should hold real BTC amount {real_btc}, got {sol_btc.balance}"
        assert sol_btc.tradable is True

    def test_btc_quoted_is_informational_when_no_btc_held(self):
        """v2.11.0: zero BTC balance → tradable=False, balance=0. This is the
        core fix for the phantom-balance bug — the SOL/BTC engine no longer
        sizes orders it cannot fund."""
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0},  # no BTC at all
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent._set_engine_balances(per_pair_usd=100.0 / 3)

        sol_btc = agent.engines["SOL/BTC"]
        assert sol_btc.tradable is False
        assert sol_btc.balance == 0.0
        # USD-quoted pairs are unaffected
        assert agent.engines["SOL/USDC"].tradable is True
        assert agent.engines["BTC/USDC"].tradable is True

    def test_info_only_sol_btc_refuses_to_execute(self):
        """End-to-end: an info-only engine must return None from execute_signal
        regardless of how strong the signal. No Trade object is ever produced,
        so the placement path is never reached — the journaled
        PLACEMENT_FAILED(insufficient_BTC_balance) entries from the bug
        simply cannot happen anymore."""
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0},
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent._set_engine_balances(per_pair_usd=100.0 / 3)
        sol_btc = agent.engines["SOL/BTC"]
        # Seed warmup so execute_signal isn't filtered by the prices guard
        for i in range(60):
            price = 0.002167 + i * 1e-7
            sol_btc.ingest_candle({
                "open": price, "high": price, "low": price,
                "close": price, "volume": 100.0,
                "timestamp": float(1700000000 + i * 300),
            })
        trade = sol_btc.execute_signal("BUY", 0.85, "strong mean-reversion signal")
        assert trade is None, \
            "info-only engine must not produce a Trade for any signal strength"

    def test_refresh_tradable_activates_when_btc_arrives(self, monkeypatch):
        monkeypatch.setenv("HYDRA_BRIDGE_TRADING", "1")  # legacy opt-in path
        """v2.11.0: _refresh_tradable_flags re-seats the flag when real BTC
        appears mid-session (e.g., BTC/USDC fill or user deposit). The
        engine transitions False→True and the equity baseline resets."""
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0},
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent._set_engine_balances(per_pair_usd=100.0 / 3)
        sol_btc = agent.engines["SOL/BTC"]
        assert sol_btc.tradable is False

        # BTC balance arrives (simulate a fill or deposit)
        agent._cached_balance = {"USDC": 70.0, "XXBT": 0.0005}
        agent._refresh_tradable_flags()
        assert sol_btc.tradable is True, "engine must activate once BTC is held"
        assert abs(sol_btc.balance - 0.0005) < 1e-12
        # Equity baseline re-seated cleanly
        assert sol_btc.initial_balance == sol_btc.balance + sol_btc.position.size * 0.002167
        assert sol_btc.peak_equity == sol_btc.initial_balance
        assert sol_btc.max_drawdown == 0.0

    def test_refresh_tradable_deactivates_when_btc_depleted(self, monkeypatch):
        monkeypatch.setenv("HYDRA_BRIDGE_TRADING", "1")  # legacy opt-in path
        """Symmetric: if BTC is spent down below costmin, the engine flips
        back to info-only on the next refresh."""
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0, "XXBT": 0.0005},
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent._set_engine_balances(per_pair_usd=100.0 / 3)
        sol_btc = agent.engines["SOL/BTC"]
        assert sol_btc.tradable is True

        # BTC depleted to below costmin (0.00002 BTC)
        agent._cached_balance = {"USDC": 100.0, "XXBT": 0.0}
        agent._refresh_tradable_flags()
        assert sol_btc.tradable is False
        assert sol_btc.balance == 0.0

    def test_btc_quoted_resumed_with_position_pnl_sane(self):
        """Resumed SOL/BTC engine with existing position must not inflate P&L.
        initial_balance is set so that pnl_pct ≈ 0% at reset time, regardless
        of whether the engine is info-only or tradable."""
        # Info-only case: no BTC held, but a SOL position exists from before.
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0},
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
        )
        agent.engines["SOL/BTC"].position.size = 0.5
        agent.engines["SOL/BTC"].position.avg_entry = 0.002100
        agent._set_engine_balances(per_pair_usd=100.0 / 3)

        engine = agent.engines["SOL/BTC"]
        current_price = 0.002167
        equity = engine.balance + engine.position.size * current_price
        # When info-only: balance=0, initial_balance=position_value, pnl_pct=0.
        pnl_pct = ((equity - engine.initial_balance) / engine.initial_balance * 100) \
            if engine.initial_balance > 0 else 0.0
        assert abs(pnl_pct) < 5.0, \
            f"P&L should be near 0% after balance reset, got {pnl_pct:+.2f}%"

    def test_paper_mode_preserves_legacy_usd_conversion(self, monkeypatch):
        monkeypatch.setenv("HYDRA_BRIDGE_TRADING", "1")  # legacy opt-in path
        """Paper mode must NOT gate on real holdings — strategy simulations
        should work regardless of live-account composition. Preserves the
        pre-v2.11.0 USD→quote conversion so SOL/BTC remains tradable in paper."""
        agent = self._bare_agent_for_balance_tests(
            cached_balance={"USDC": 100.0},  # no BTC anywhere
            pair_prices={"SOL/USDC": 130.0, "BTC/USDC": 60000.0, "SOL/BTC": 0.002167},
            paper=True,
        )
        agent._set_engine_balances(per_pair_usd=33.33)

        sol_btc = agent.engines["SOL/BTC"]
        # Legacy behavior: USD/BTC_price phantom balance, always tradable.
        expected_btc = 33.33 / 60000.0
        assert sol_btc.tradable is True
        assert abs(sol_btc.balance - expected_btc) < 1e-10

    def test_equity_history_clean_after_balance_reset(self):
        """Engine that only had candles ingested (no ticks) should have empty equity history."""
        engine = HydraEngine(initial_balance=33.33, asset="SOL/USDC")
        # Simulate warmup: ingest candles without ticking
        for i in range(50):
            engine.ingest_candle({
                "open": 130 + i * 0.1, "high": 131 + i * 0.1,
                "low": 129 + i * 0.1, "close": 130.5 + i * 0.1,
                "volume": 1000, "time": 1000 + i * 300,
            })
        # Equity history should be empty (ingest_candle doesn't call tick)
        assert len(engine.equity_history) == 0
        # Now reset balance like startup does
        engine.initial_balance = 500.0
        engine.balance = 500.0
        engine.peak_equity = 500.0
        # First tick should use new balance
        state = engine.tick()
        assert state["portfolio"]["equity"] == 500.0 or state["portfolio"]["equity"] > 490


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestStakedAssets,
        TestNormalizeAsset,
        TestComputeBalanceUsd,
        TestGetAssetPrices,
        TestEngineBalanceInit,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  Balance Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
