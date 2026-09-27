"""
HYDRA Cross-Pair Coordinator Test Suite
Validates regime correlation detection, override generation, coordinated swap
signals, and edge cases. All tests use deterministic synthetic state dicts.
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_engine import CrossPairCoordinator, HydraEngine, Candle


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

PAIRS = ["SOL/USDC", "SOL/BTC", "BTC/USDC"]


def make_state(regime, signal_action="HOLD", confidence=0.5, position_size=0.0, price=100.0):
    """Build a minimal engine state dict for testing."""
    return {
        "regime": regime,
        "signal": {"action": signal_action, "confidence": confidence, "reason": "test"},
        "position": {"size": position_size, "avg_entry": price, "unrealized_pnl": 0.0},
        "price": price,
        "portfolio": {"balance": 100.0, "equity": 100.0, "pnl_pct": 0.0},
    }


# ═══════════════════════════════════════════════════════════════
# 1. INITIALIZATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestInit:
    def test_creates_history_for_all_pairs(self):
        coord = CrossPairCoordinator(PAIRS)
        assert set(coord.regime_history.keys()) == set(PAIRS)
        for history in coord.regime_history.values():
            assert history == []

    def test_stores_pairs(self):
        coord = CrossPairCoordinator(PAIRS)
        assert coord.pairs == PAIRS

    def test_legacy_list_api_derives_triangle(self):
        """Legacy List[str] API should auto-derive a TradingTriangle when
        the list contains a complete triangle. Audit P7-8."""
        coord = CrossPairCoordinator(PAIRS)
        assert coord.triangle is not None
        assert coord.triangle.stable_sol.cli_format == "SOL/USDC"
        assert coord.triangle.stable_btc.cli_format == "BTC/USDC"
        assert coord.triangle.bridge.cli_format == "SOL/BTC"
        assert coord.triangle.quote == "USDC"

    def test_usd_pair_list_derives_usd_triangle(self):
        """USD-default pair list should derive a USD triangle."""
        coord = CrossPairCoordinator(["SOL/USD", "SOL/BTC", "BTC/USD"])
        assert coord.triangle is not None
        assert coord.triangle.quote == "USD"

    def test_incomplete_triangle_returns_none(self):
        """Single-pair construction (resume reconciliation) yields no
        triangle. Audit P7-1, P7-3."""
        coord = CrossPairCoordinator(["SOL/USDC"])
        assert coord.triangle is None

    def test_get_overrides_with_none_triangle_is_noop(self):
        """When triangle is None (incomplete pair list), get_overrides
        must return {} without crashing on missing role attributes.
        Audit P7-3."""
        coord = CrossPairCoordinator(["SOL/USDC"])
        states = {
            "SOL/USDC": make_state("TREND_DOWN", signal_action="SELL", position_size=1.0),
        }
        # Even with state that would normally trigger Rule 3, no
        # overrides emit because the bridge/btc roles are unbound.
        overrides = coord.get_overrides(states)
        assert overrides == {}

    def test_get_overrides_empty_pairs_is_noop(self):
        """Pathological: empty pair list. Triangle None, overrides empty."""
        coord = CrossPairCoordinator([])
        assert coord.triangle is None
        assert coord.get_overrides({}) == {}

    def test_mismatched_quotes_in_pair_list_yields_no_triangle(self):
        """Pair list with mixed stable quotes (SOL/USD + BTC/USDC) cannot
        form a coherent triangle — the two stable legs must share quote."""
        coord = CrossPairCoordinator(["SOL/USD", "BTC/USDC", "SOL/BTC"])
        assert coord.triangle is None


# ═══════════════════════════════════════════════════════════════
# 2. REGIME HISTORY TRACKING
# ═══════════════════════════════════════════════════════════════

class TestRegimeHistory:
    def test_update_appends_regime(self):
        coord = CrossPairCoordinator(PAIRS)
        coord.update("SOL/USDC", "TREND_UP")
        coord.update("SOL/USDC", "RANGING")
        assert coord.regime_history["SOL/USDC"] == ["TREND_UP", "RANGING"]

    def test_history_bounded_to_10(self):
        coord = CrossPairCoordinator(PAIRS)
        for i in range(15):
            coord.update("SOL/USDC", f"R{i}")
        assert len(coord.regime_history["SOL/USDC"]) == 10
        assert coord.regime_history["SOL/USDC"][0] == "R5"
        assert coord.regime_history["SOL/USDC"][-1] == "R14"

    def test_update_unknown_pair_creates_entry(self):
        coord = CrossPairCoordinator(PAIRS)
        coord.update("ETH/USDC", "TREND_UP")
        assert coord.regime_history["ETH/USDC"] == ["TREND_UP"]


# ═══════════════════════════════════════════════════════════════
# 3. RULE 1: BTC LEADS SOL DOWN
# ═══════════════════════════════════════════════════════════════

class TestRule1BtcLeadsDown:
    def test_btc_down_sol_up_triggers_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.8

    def test_btc_down_sol_ranging_triggers_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("RANGING"),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["signal"] == "SELL"

    def test_btc_down_sol_also_down_no_override(self):
        """If SOL is already trending down, no override needed."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_DOWN"),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        # Rule 1 should NOT fire (SOL already down), Rule 3 won't fire (no position)
        assert "SOL/USDC" not in overrides


# ═══════════════════════════════════════════════════════════════
# 4. RULE 2: BTC RECOVERY BOOST
# ═══════════════════════════════════════════════════════════════

class TestRule2BtcRecovery:
    def test_btc_up_sol_down_boosts_confidence(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_DOWN", confidence=0.4),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "ADJUST"
        assert overrides["SOL/USDC"]["signal"] == "HOLD"
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.55  # 0.4 + 0.15, action unchanged

    def test_confidence_capped_at_095(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_DOWN", confidence=0.9),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.95

    def test_btc_up_sol_up_no_boost(self):
        """No boost needed if SOL is already trending up."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" not in overrides


# ═══════════════════════════════════════════════════════════════
# 5. RULE 3: COORDINATED SWAP
# ═══════════════════════════════════════════════════════════════

class TestRule3CoordinatedSwap:
    def test_sol_down_solbtc_up_with_position_triggers_swap(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/BTC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        assert "swap" in overrides["SOL/USDC"]
        swap = overrides["SOL/USDC"]["swap"]
        assert swap["sell_pair"] == "SOL/USDC"
        assert swap["buy_pair"] == "SOL/BTC"

    def test_no_swap_without_position(self):
        """Don't suggest a swap if we don't hold any SOL via SOL/USDC."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=0.0),
            "SOL/BTC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        # Without position, rule 3 shouldn't fire. Rule 1 also doesn't apply
        # (BTC isn't down). So no overrides.
        assert "SOL/USDC" not in overrides

    def test_sol_down_solbtc_also_down_no_swap(self):
        """No swap if SOL/BTC is also trending down."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/BTC": make_state("TREND_DOWN"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" not in overrides or "swap" not in overrides.get("SOL/USDC", {})


# ═══════════════════════════════════════════════════════════════
# 6. NO OVERRIDE BASELINES
# ═══════════════════════════════════════════════════════════════

class TestNoOverride:
    def test_all_ranging_no_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("RANGING"),
            "SOL/BTC": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert len(overrides) == 0

    def test_all_trending_up_no_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/BTC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert len(overrides) == 0

    def test_missing_pair_graceful(self):
        """Should not crash if a pair is missing from states."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "SOL/USDC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert isinstance(overrides, dict)

    def test_empty_states_no_crash(self):
        coord = CrossPairCoordinator(PAIRS)
        overrides = coord.get_overrides({})
        assert overrides == {}


# ═══════════════════════════════════════════════════════════════
# 7. RULE PRIORITY (Rule 1 vs Rule 3 conflict)
# ═══════════════════════════════════════════════════════════════

class TestRulePriority:
    def test_rule1_fires_when_sol_up_and_btc_down(self):
        """Rule 1 fires when BTC is down and SOL is still up. Rule 3 cannot fire
        simultaneously because it requires SOL/USDC TREND_DOWN."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_UP", position_size=5.0),
            "SOL/BTC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        # Rule 3 requires SOL/USDC TREND_DOWN, so no swap here
        assert "swap" not in overrides["SOL/USDC"]

    def test_rule3_fires_independently_of_rule1(self):
        """Rule 3 fires when SOL/USDC is TREND_DOWN (Rule 1 doesn't apply)."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state("RANGING"),  # Not TREND_DOWN, so Rule 1 doesn't fire
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/BTC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert "swap" in overrides["SOL/USDC"]


# ═══════════════════════════════════════════════════════════════
# 7b. RULE 4 — SIGNAL CONFLUENCE (v2.11.0)
# ═══════════════════════════════════════════════════════════════

def _co_move_prices(n=80, drift=0.0005, shared_noise_amp=0.015, rng_seed=11):
    """Two tightly co-moving price series (ρ ≈ 1 across log-returns).
    drift gives both a small upward trend; shared_noise_amp injects the
    SAME random shock on both series so the log-return variance is
    non-zero AND identical, driving ρ to exactly 1.
    """
    import random
    rng = random.Random(rng_seed)
    a = [100.0]
    b = [0.0012]
    for _ in range(n):
        shock = rng.uniform(-1, 1) * shared_noise_amp
        a.append(a[-1] * math.exp(drift + shock))
        b.append(b[-1] * math.exp(drift + shock))
    return a, b


def _independent_prices(n=80, rng_seed_a=7, rng_seed_b=8):
    """Two independent walks — ρ should be near zero."""
    import random
    rng_a = random.Random(rng_seed_a)
    rng_b = random.Random(rng_seed_b)
    a = [100.0]
    b = [0.0012]
    for _ in range(n):
        a.append(a[-1] * math.exp(rng_a.gauss(0, 0.008)))
        b.append(b[-1] * math.exp(rng_b.gauss(0, 0.008)))
    return a, b


def make_state_with_signal(regime, action="HOLD", confidence=0.5, position_size=0.0, price=100.0):
    """Variant of make_state that lets us set a specific signal action."""
    return {
        "regime": regime,
        "signal": {"action": action, "confidence": confidence, "reason": "test"},
        "position": {"size": position_size, "avg_entry": price, "unrealized_pnl": 0.0},
        "price": price,
        "portfolio": {"balance": 100.0, "equity": 100.0, "pnl_pct": 0.0},
    }


class TestRule4Confluence:
    """Rule 4: SOL/BTC + SOL/USDC same-direction signal confluence."""

    def test_buy_confluence_boosts_when_rho_high(self):
        """Both BUY + high ρ → SOL/USDC gets a bounded confidence boost."""
        coord = CrossPairCoordinator(PAIRS)
        # Same-direction BUY; Rule 3 won't trigger because SOL/USDC is RANGING.
        states = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("RANGING", action="BUY", confidence=0.68),
            "SOL/BTC":  make_state_with_signal("RANGING", action="BUY", confidence=0.72),
        }
        prices_usdc, prices_btc = _co_move_prices()
        overrides = coord.get_overrides(
            states, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        assert "SOL/USDC" in overrides
        ov = overrides["SOL/USDC"]
        assert ov["action"] == "ADJUST"
        assert ov["signal"] == "BUY"
        assert ov["confidence_adj"] > 0.68, "confidence must be boosted above baseline"
        assert ov["confidence_adj"] <= 0.95, "boost must respect the 0.95 ceiling"
        src = ov["confluence_source"]
        assert src["source_pair"] == "SOL/BTC"
        assert src["rho"] > 0.5
        assert 0.0 < src["bonus"] <= 0.10

    def test_buy_confluence_skipped_when_rho_low(self):
        """Below threshold ρ → no confluence override."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("RANGING", action="BUY", confidence=0.68),
            "SOL/BTC":  make_state_with_signal("RANGING", action="BUY", confidence=0.72),
        }
        prices_usdc, prices_btc = _independent_prices()
        overrides = coord.get_overrides(
            states, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        # Either no override at all, or an override that did NOT come from Rule 4.
        ov = overrides.get("SOL/USDC")
        if ov is not None:
            assert "confluence_source" not in ov, (
                "low-ρ pairs must not produce a Rule 4 confluence_source"
            )

    def test_mixed_actions_no_confluence(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("RANGING", action="BUY", confidence=0.70),
            "SOL/BTC":  make_state_with_signal("RANGING", action="SELL", confidence=0.70),
        }
        prices_usdc, prices_btc = _co_move_prices()
        overrides = coord.get_overrides(
            states, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        ov = overrides.get("SOL/USDC")
        assert ov is None or "confluence_source" not in ov

    def test_sell_confluence_requires_sol_position(self):
        """Sell confluence only fires when we hold SOL via SOL/USDC."""
        coord = CrossPairCoordinator(PAIRS)
        states_no_pos = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("RANGING", action="SELL", confidence=0.70, position_size=0.0),
            "SOL/BTC":  make_state_with_signal("RANGING", action="SELL", confidence=0.70),
        }
        prices_usdc, prices_btc = _co_move_prices()
        overrides_no = coord.get_overrides(
            states_no_pos, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        ov_no = overrides_no.get("SOL/USDC")
        assert ov_no is None or "confluence_source" not in ov_no

        coord2 = CrossPairCoordinator(PAIRS)
        states_pos = dict(states_no_pos)
        states_pos["SOL/USDC"] = make_state_with_signal("RANGING", action="SELL",
                                                        confidence=0.70, position_size=5.0)
        overrides_pos = coord2.get_overrides(
            states_pos, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        ov_pos = overrides_pos.get("SOL/USDC")
        assert ov_pos is not None
        assert ov_pos.get("confluence_source") is not None
        assert ov_pos["signal"] == "SELL"

    def test_no_price_series_is_safe_noop(self):
        """Coordinator must not crash when no price_series is supplied; Rule 4 simply doesn't fire."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("RANGING", action="BUY", confidence=0.70),
            "SOL/BTC":  make_state_with_signal("RANGING", action="BUY", confidence=0.70),
        }
        # No price_series argument at all (legacy callers).
        overrides = coord.get_overrides(states)
        ov = overrides.get("SOL/USDC")
        assert ov is None or "confluence_source" not in ov

    def test_rule3_suppresses_rule4_on_same_pair(self):
        """When Rule 3 emits an OVERRIDE swap, Rule 4 must not clobber it."""
        coord = CrossPairCoordinator(PAIRS)
        # Rule 3 conditions: SOL/USDC TREND_DOWN + SOL/BTC TREND_UP + holds SOL.
        states = {
            "BTC/USDC": make_state_with_signal("RANGING"),
            "SOL/USDC": make_state_with_signal("TREND_DOWN", action="SELL", confidence=0.70,
                                                position_size=5.0),
            "SOL/BTC":  make_state_with_signal("TREND_UP", action="BUY", confidence=0.70),
        }
        prices_usdc, prices_btc = _co_move_prices()
        overrides = coord.get_overrides(
            states, price_series={"SOL/USDC": prices_usdc, "SOL/BTC": prices_btc},
        )
        assert "SOL/USDC" in overrides
        # Rule 3 signature: OVERRIDE + swap dict.
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert "swap" in overrides["SOL/USDC"]


# ═══════════════════════════════════════════════════════════════
# 8. SHARPE ANNUALIZATION FIX
# ═══════════════════════════════════════════════════════════════

class TestSharpeAnnualization:
    def test_5min_candle_different_from_1min(self):
        """Sharpe should differ based on candle interval."""
        engine_1m = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=1)
        engine_5m = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=5)

        # Feed identical equity history
        equity = [10000 + i * 10 for i in range(60)]
        engine_1m.equity_history = list(equity)
        engine_5m.equity_history = list(equity)

        sharpe_1m = engine_1m._calc_sharpe()
        sharpe_5m = engine_5m._calc_sharpe()

        # 1-min should have higher annualized Sharpe (more periods per year)
        assert sharpe_1m > sharpe_5m
        assert sharpe_5m > 0

    def test_1min_candle_uses_full_annualization(self):
        """With 1-min candles, periods_per_year = 525600."""
        import math
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=1)
        engine.equity_history = [10000 + i for i in range(60)]
        sharpe = engine._calc_sharpe()
        assert sharpe > 0


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    test_classes = [
        TestInit,
        TestRegimeHistory,
        TestRule1BtcLeadsDown,
        TestRule2BtcRecovery,
        TestRule3CoordinatedSwap,
        TestNoOverride,
        TestRulePriority,
        TestRule4Confluence,
        TestSharpeAnnualization,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  ERROR {cls.__name__}.{method_name}: {e}")

    print(f"\n  {'='*60}")
    print(f"  Cross-Pair Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  FAILURES:")
        for cls_name, method_name, err in errors:
            print(f"    {cls_name}.{method_name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
