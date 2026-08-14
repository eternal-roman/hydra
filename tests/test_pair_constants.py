"""
HYDRA Dynamic Pair Constants Test Suite

Validates load_pair_constants, apply_pair_constants (registry overlay),
and PositionSizer.apply_pair_limits. v2.19+: KrakenCLI delegates pair
metadata to hydra_pair_registry.PairRegistry; the legacy class-level
PRICE_DECIMALS dict is gone. Tests now assert against the registry.
"""

import sys
import os
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_kraken_cli import KrakenCLI
from hydra_pair_registry import default_registry
from hydra_engine import PositionSizer


# ═══════════════════════════════════════════════════════════════
# Stub helper
# ═══════════════════════════════════════════════════════════════

class _StubRun:
    def __init__(self, response):
        self._response = response
        self.calls = []
        self._original = None

    def install(self):
        self._original = KrakenCLI._run
        outer = self

        def fake(args, timeout=20):
            outer.calls.append(list(args))
            return outer._response

        KrakenCLI._run = staticmethod(fake)

    def restore(self):
        if self._original is not None:
            KrakenCLI._run = staticmethod(self._original)
            self._original = None


# Real Kraken response shape for SOL/USDC, BTC/USDC (Kraken key: XBTUSDC), SOL/BTC (Kraken key: SOL/BTC)
REAL_KRAKEN_RESPONSE = {
    "SOL/USDC": {
        "pair_decimals": 2,
        "ordermin": "0.02",
        "costmin": "0.5",
        "base": "SOL",
        "quote": "USDC",
        "lot_decimals": 8,
        "tick_size": "0.01",
        "wsname": "SOL/USDC",
        "altname": "SOLUSDC",
        "status": "online",
    },
    "XBTUSDC": {
        "pair_decimals": 2,
        "ordermin": "0.00005",
        "costmin": "0.5",
        "base": "XXBT",
        "quote": "USDC",
        "lot_decimals": 8,
        "tick_size": "0.01",
        "wsname": "XBT/USDC",
        "altname": "XBTUSDC",
        "status": "online",
    },
    "SOL/BTC": {
        "pair_decimals": 7,
        "ordermin": "0.02",
        "costmin": "0.00002",
        "base": "SOL",
        "quote": "XXBT",
        "lot_decimals": 8,
        "tick_size": "0.0000001",
        "wsname": "SOL/XBT",
        "altname": "SOLXBT",
        "status": "online",
    },
}

FRIENDLY_PAIRS = ["SOL/USDC", "SOL/BTC", "BTC/USDC"]


# ═══════════════════════════════════════════════════════════════
# Helpers — reset KrakenCLI.registry between tests for isolation
# ═══════════════════════════════════════════════════════════════

def _reset_registry():
    """Snapshot a fresh default registry into the class-level slot."""
    KrakenCLI.set_registry(default_registry())


def _snapshot_sizer():
    return (dict(PositionSizer.MIN_ORDER_SIZE), dict(PositionSizer.MIN_COST))


def _restore_sizer(snap):
    PositionSizer.MIN_ORDER_SIZE.clear()
    PositionSizer.MIN_ORDER_SIZE.update(snap[0])
    PositionSizer.MIN_COST.clear()
    PositionSizer.MIN_COST.update(snap[1])


# ═══════════════════════════════════════════════════════════════
# TESTS: load_pair_constants
# ═══════════════════════════════════════════════════════════════

class TestLoadPairConstants:

    def setup_method(self):
        _reset_registry()

    def test_parses_real_kraken_shape(self):
        """Feed the real Kraken response shape and verify all fields extracted."""
        stub = _StubRun(REAL_KRAKEN_RESPONSE)
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()

        assert "SOL/USDC" in result
        sol = result["SOL/USDC"]
        assert sol["price_decimals"] == 2
        assert sol["ordermin"] == 0.02
        assert sol["costmin"] == 0.5
        assert sol["base"] == "SOL"
        assert sol["quote"] == "USDC"
        assert sol["lot_decimals"] == 8
        assert sol["tick_size"] == "0.01"

    def test_maps_xbtusdc_to_friendly(self):
        """Kraken returns 'XBTUSDC' key with wsname='XBT/USDC' → registry resolves XBT alias to friendly 'BTC/USDC'."""
        stub = _StubRun(REAL_KRAKEN_RESPONSE)
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()

        assert "BTC/USDC" in result
        btc = result["BTC/USDC"]
        assert btc["price_decimals"] == 2
        assert btc["ordermin"] == 0.00005
        assert btc["base"] == "BTC"  # normalized from XXBT

    def test_maps_sol_btc_to_friendly(self):
        """Kraken returns 'SOL/BTC' key with wsname='SOL/XBT' → maps to friendly 'SOL/BTC'."""
        stub = _StubRun(REAL_KRAKEN_RESPONSE)
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()

        assert "SOL/BTC" in result
        sol_btc = result["SOL/BTC"]
        assert sol_btc["price_decimals"] == 7
        assert sol_btc["costmin"] == 0.00002
        assert sol_btc["quote"] == "BTC"  # normalized from XXBT

    def test_error_response_returns_empty(self):
        stub = _StubRun({"error": "EQuery:Unknown asset pair"})
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()
        assert result == {}

    def test_non_dict_response_returns_empty(self):
        stub = _StubRun("not a dict")
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()
        assert result == {}

    def test_empty_response_returns_empty(self):
        stub = _StubRun({})
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()
        assert result == {}

    def test_unknown_pair_in_response_passes_through(self):
        """Kraken extra pairs that the registry cannot resolve are dropped
        unless their slashless altname matches a requested form. NIGHT/USD
        is not in the fallback catalog and is not in FRIENDLY_PAIRS."""
        resp = {"NIGHT/USD": {"pair_decimals": 4, "ordermin": "1", "costmin": "0.5",
                              "base": "NIGHT", "quote": "USD", "lot_decimals": 8,
                              "wsname": "NIGHT/USD", "altname": "NIGHTUSD"}}
        stub = _StubRun(resp)
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()
        assert result == {}

    def test_non_dict_pair_entry_skipped(self):
        """If a pair entry is not a dict, skip it gracefully."""
        resp = {"SOL/USDC": "not a dict"}
        stub = _StubRun(resp)
        stub.install()
        try:
            result = KrakenCLI.load_pair_constants(FRIENDLY_PAIRS)
        finally:
            stub.restore()
        assert result == {}


# ═══════════════════════════════════════════════════════════════
# TESTS: apply_pair_constants — now overlays the registry
# ═══════════════════════════════════════════════════════════════

class TestApplyPairConstants:

    def setup_method(self):
        _reset_registry()

    def test_overlays_precision_in_registry(self):
        """apply_pair_constants should overlay precision in the registry."""
        # Static fallback has SOL/USDC at 2 decimals; override to 3.
        loaded = {"SOL/USDC": {
            "price_decimals": 3, "ordermin": 0.02, "costmin": 0.5,
            "base": "SOL", "quote": "USDC", "lot_decimals": 8, "tick_size": None,
        }}
        KrakenCLI.apply_pair_constants(loaded)
        assert KrakenCLI.registry.resolve("SOL/USDC").price_decimals == 3
        # The registry resolves all alias forms to the same canonical pair,
        # so all forms reflect the new precision.
        assert KrakenCLI.registry.resolve("SOLUSDC").price_decimals == 3

    def test_overlays_btc_pair(self):
        """BTC/USDC overlay reflected in registry resolution."""
        loaded = {"BTC/USDC": {
            "price_decimals": 2, "ordermin": 0.0001, "costmin": 0.5,
            "base": "BTC", "quote": "USDC", "lot_decimals": 8, "tick_size": None,
        }}
        KrakenCLI.apply_pair_constants(loaded)
        assert KrakenCLI.registry.resolve("BTC/USDC").price_decimals == 2
        assert KrakenCLI.registry.resolve("BTCUSDC").price_decimals == 2
        # XBT alias still resolves correctly after overlay.
        assert KrakenCLI.registry.resolve("XBT/USDC").price_decimals == 2

    def test_preserves_unaffected_entries(self):
        """Existing entries for other pairs should survive."""
        original_sol_btc = KrakenCLI.registry.resolve("SOL/BTC").price_decimals
        loaded = {"SOL/USDC": {
            "price_decimals": 3, "ordermin": 0.02, "costmin": 0.5,
            "base": "SOL", "quote": "USDC", "lot_decimals": 8, "tick_size": None,
        }}
        KrakenCLI.apply_pair_constants(loaded)
        assert KrakenCLI.registry.resolve("SOL/BTC").price_decimals == original_sol_btc

    def test_dynamic_overrides_in_format_price(self):
        """After apply, _format_price should use the new precision."""
        loaded = {"SOL/USDC": {
            "price_decimals": 3, "ordermin": 0.02, "costmin": 0.5,
            "base": "SOL", "quote": "USDC", "lot_decimals": 8, "tick_size": None,
        }}
        KrakenCLI.apply_pair_constants(loaded)
        # With 2 decimals, 80.4745 → 80.47; with 3 decimals → 80.475
        # round(80.4745, 3) → 80.475 (Python banker's rounding to nearest tied even
        # would give 80.474 but float repr of 80.4745 is 80.474500000…01 so it
        # rounds up).
        assert KrakenCLI._format_price("SOL/USDC", 80.4745) == "80.47500000"
        # 80.1234 with 3 decimals → 80.123
        assert KrakenCLI._format_price("SOL/USDC", 80.1234) == "80.12300000"


# ═══════════════════════════════════════════════════════════════
# TESTS: PositionSizer.apply_pair_limits
# ═══════════════════════════════════════════════════════════════

class TestApplyPairLimits:

    def test_updates_min_order_size(self):
        snap = _snapshot_sizer()
        try:
            sizer = PositionSizer()
            loaded = {"SOL/USDC": {"base": "SOL", "quote": "USDC", "ordermin": 0.05, "costmin": 1.0}}
            sizer.apply_pair_limits(loaded)
            assert PositionSizer.MIN_ORDER_SIZE["SOL"] == 0.05
            assert PositionSizer.MIN_COST["USDC"] == 1.0
        finally:
            _restore_sizer(snap)

    def test_updates_btc_limits(self):
        snap = _snapshot_sizer()
        try:
            sizer = PositionSizer()
            loaded = {"BTC/USDC": {"base": "BTC", "quote": "USDC", "ordermin": 0.0001, "costmin": 0.75}}
            sizer.apply_pair_limits(loaded)
            assert PositionSizer.MIN_ORDER_SIZE["BTC"] == 0.0001
            assert PositionSizer.MIN_COST["USDC"] == 0.75
        finally:
            _restore_sizer(snap)

    def test_preserves_other_assets(self):
        snap = _snapshot_sizer()
        try:
            original_eth = PositionSizer.MIN_ORDER_SIZE.get("ETH")
            sizer = PositionSizer()
            loaded = {"SOL/USDC": {"base": "SOL", "quote": "USDC", "ordermin": 0.05, "costmin": 1.0}}
            sizer.apply_pair_limits(loaded)
            assert PositionSizer.MIN_ORDER_SIZE.get("ETH") == original_eth
        finally:
            _restore_sizer(snap)

    def test_empty_loaded_is_noop(self):
        snap = _snapshot_sizer()
        try:
            sizer = PositionSizer()
            sizer.apply_pair_limits({})
            assert PositionSizer.MIN_ORDER_SIZE == snap[0]
            assert PositionSizer.MIN_COST == snap[1]
        finally:
            _restore_sizer(snap)

    def test_missing_base_skipped(self):
        snap = _snapshot_sizer()
        try:
            sizer = PositionSizer()
            loaded = {"SOL/USDC": {"base": "", "quote": "USDC", "ordermin": 0.05, "costmin": 1.0}}
            sizer.apply_pair_limits(loaded)
            # SOL should NOT be updated (base is empty)
            assert PositionSizer.MIN_ORDER_SIZE["SOL"] == snap[0]["SOL"]
            # But USDC should be updated (quote is present)
            assert PositionSizer.MIN_COST["USDC"] == 1.0
        finally:
            _restore_sizer(snap)


# ═══════════════════════════════════════════════════════════════
# TESTS: Fallback behavior
# ═══════════════════════════════════════════════════════════════

class TestFallbackBehavior:

    def setup_method(self):
        _reset_registry()

    def test_format_price_works_without_dynamic_load(self):
        """Static fallback values work even if load_pair_constants was never called."""
        # SOL/USDC fallback has 2 decimals
        assert KrakenCLI._format_price("SOL/USDC", 80.4745) == "80.47000000"
        # SOL/USD fallback has 2 decimals
        assert KrakenCLI._format_price("SOL/USD", 80.4745) == "80.47000000"

    def test_sizer_works_without_dynamic_load(self):
        """Hardcoded MIN_ORDER_SIZE/MIN_COST work without apply_pair_limits."""
        sizer = PositionSizer(kelly_multiplier=0.5, min_confidence=0.5)
        # Large enough balance and confidence to trigger a trade
        size = sizer.calculate(0.8, 1000.0, 100.0, "SOL/USDC")
        assert size > 0


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestLoadPairConstants,
        TestApplyPairConstants,
        TestApplyPairLimits,
        TestFallbackBehavior,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                if hasattr(instance, "setup_method"):
                    instance.setup_method()
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
    print(f"  Pair Constants Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
