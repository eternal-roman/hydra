"""Tests for hydra_pair_registry — single source of truth for pair metadata.

Coverage:
  - Pair value-object equality / hashing / immutability
  - Stable-quote membership: USD, USDC, USDT (true); BTC (false)
  - Resolution by every known form: slashed, slashless, lowercased, alias
  - Legacy aliases: XBT ↔ BTC, ZUSD ↔ USD, ZUSDC ↔ USDC
  - Asset normalization: staked suffixes (.B/.S/.M/.F), Z-prefix fiat,
    XX-prefix crypto, USDC.F earn-flex
  - pairs_by_quote filtering
  - bootstrap_from_kraken merges precision without dropping fields
  - resolve() raises on unknown; get() returns None
"""
import pytest

from hydra_pair_registry import (
    Pair,
    PairRegistry,
    STABLE_QUOTES,
    default_registry,
    normalize_asset,
)


# ─── Pair value object ───

def test_pair_is_frozen():
    p = Pair(
        cli_format="SOL/USD", api_format="SOLUSD", ws_format="SOL/USD",
        base="SOL", quote="USD", price_decimals=2, ordermin=0.02,
        costmin=0.5, lot_decimals=8, tick_size=None,
    )
    with pytest.raises((AttributeError, Exception)):
        p.quote = "USDC"  # frozen dataclass


def test_pair_equality_by_value():
    a = Pair("SOL/USD", "SOLUSD", "SOL/USD", "SOL", "USD", 2, 0.02, 0.5, 8, None)
    b = Pair("SOL/USD", "SOLUSD", "SOL/USD", "SOL", "USD", 2, 0.02, 0.5, 8, None)
    assert a == b
    assert hash(a) == hash(b)


def test_pair_is_stable_quoted():
    sol_usd = Pair("SOL/USD", "SOLUSD", "SOL/USD", "SOL", "USD", 2, 0.02, 0.5, 8, None)
    sol_usdc = Pair("SOL/USDC", "SOLUSDC", "SOL/USDC", "SOL", "USDC", 2, 0.02, 0.5, 8, None)
    sol_usdt = Pair("SOL/USDT", "SOLUSDT", "SOL/USDT", "SOL", "USDT", 2, 0.02, 0.5, 8, None)
    sol_btc = Pair("SOL/BTC", "SOLBTC", "SOL/BTC", "SOL", "BTC", 7, 0.02, 0.0001, 8, None)
    assert sol_usd.is_stable_quoted is True
    assert sol_usdc.is_stable_quoted is True
    assert sol_usdt.is_stable_quoted is True
    assert sol_btc.is_stable_quoted is False


def test_stable_quotes_membership():
    assert "USD" in STABLE_QUOTES
    assert "USDC" in STABLE_QUOTES
    assert "USDT" in STABLE_QUOTES
    assert "BTC" not in STABLE_QUOTES
    assert "EUR" not in STABLE_QUOTES  # explicit: only USD-family stables


# ─── Registry resolution ───

def test_default_registry_includes_core_pairs():
    reg = default_registry()
    for sym in ("SOL/USD", "SOL/USDC", "SOL/USDT", "BTC/USD", "BTC/USDC",
                "BTC/USDT", "ETH/USD", "ETH/USDC", "ETH/USDT", "ZEC/USD",
                "ZEC/USDC", "SOL/BTC"):
        assert reg.get(sym) is not None, f"missing {sym}"


def test_resolve_by_cli_format():
    reg = default_registry()
    p = reg.resolve("SOL/USD")
    assert p.cli_format == "SOL/USD"
    assert p.base == "SOL"
    assert p.quote == "USD"


def test_resolve_by_api_format_slashless():
    reg = default_registry()
    p = reg.resolve("SOLUSD")
    assert p.cli_format == "SOL/USD"


def test_resolve_case_insensitive():
    reg = default_registry()
    assert reg.resolve("sol/usd").cli_format == "SOL/USD"
    assert reg.resolve("SOL/usd").cli_format == "SOL/USD"
    assert reg.resolve("solusd").cli_format == "SOL/USD"


def test_resolve_xbt_alias():
    reg = default_registry()
    # XBT is Kraken's legacy code for BTC; both must resolve to canonical BTC pair.
    assert reg.resolve("XBT/USD").cli_format == "BTC/USD"
    assert reg.resolve("XBTUSD").cli_format == "BTC/USD"
    assert reg.resolve("XBT/USDC").cli_format == "BTC/USDC"
    assert reg.resolve("SOL/XBT").cli_format == "SOL/BTC"


def test_resolve_zusd_alias():
    """ZUSD is Kraken's Z-prefix internal code for USD fiat. Volume
    endpoint emits this in the BASE-QUOTE concatenation for fiat pairs.
    Regression: v2.19.0 missed this dialect; SOL/ZUSD didn't resolve."""
    reg = default_registry()
    assert reg.resolve("SOL/ZUSD").cli_format == "SOL/USD"
    assert reg.resolve("SOLZUSD").cli_format == "SOL/USD"
    assert reg.resolve("BTC/ZUSD").cli_format == "BTC/USD"
    assert reg.resolve("BTCZUSD").cli_format == "BTC/USD"


def test_resolve_xxbt_double_prefix_alias():
    """XXBT is Kraken's 4-letter legacy code for BTC. Combined with
    ZUSD, it produces XXBTZUSD — the form `kraken volume` actually
    emits for BTC fiat pairs. Smoking-gun regression from v2.19.0."""
    reg = default_registry()
    assert reg.resolve("XXBTZUSD").cli_format == "BTC/USD"
    assert reg.resolve("XXBT/ZUSD").cli_format == "BTC/USD"
    # Single-prefix combinations also resolve.
    assert reg.resolve("XXBTUSD").cli_format == "BTC/USD"
    assert reg.resolve("XBTZUSD").cli_format == "BTC/USD"
    # XXBT against USDC (less common but consistent with the rule).
    assert reg.resolve("XXBTUSDC").cli_format == "BTC/USDC"


def test_resolve_zusdc_alias():
    """ZUSDC is in ASSET_ALIASES → USDC; rare in production but the
    cross-product generator must still register it. Regression
    completeness check."""
    reg = default_registry()
    assert reg.resolve("SOLZUSDC").cli_format == "SOL/USDC"
    assert reg.resolve("XXBTZUSDC").cli_format == "BTC/USDC"


def test_alias_variants_data_driven():
    """The cross-product generator must derive variants from
    ASSET_ALIASES, not from a hardcoded base/quote table. Adding a new
    alias to ASSET_ALIASES at runtime should extend every pair's
    resolution surface."""
    from hydra_pair_registry import _alias_variants
    btc_variants = _alias_variants("BTC")
    assert "BTC" in btc_variants
    assert "XBT" in btc_variants
    assert "XXBT" in btc_variants
    usd_variants = _alias_variants("USD")
    assert "USD" in usd_variants
    assert "ZUSD" in usd_variants


def test_alias_variants_unknown_canonical():
    """An asset with no aliases returns just itself."""
    from hydra_pair_registry import _alias_variants
    assert _alias_variants("DOGE") == {"DOGE"}
    assert _alias_variants("") == set()


def test_resolve_unknown_raises():
    reg = default_registry()
    with pytest.raises(KeyError):
        reg.resolve("DOGE/USD")


def test_get_unknown_returns_none():
    reg = default_registry()
    assert reg.get("DOGE/USD") is None


def test_get_accepts_none_safely():
    reg = default_registry()
    assert reg.get(None) is None
    assert reg.get("") is None


# ─── Quote filtering ───

def test_pairs_by_quote_usd():
    reg = default_registry()
    usd_pairs = reg.pairs_by_quote("USD")
    symbols = {p.cli_format for p in usd_pairs}
    assert "SOL/USD" in symbols
    assert "BTC/USD" in symbols
    assert "SOL/USDC" not in symbols  # USDC is a separate quote


def test_pairs_by_quote_usdc():
    reg = default_registry()
    usdc_pairs = reg.pairs_by_quote("USDC")
    symbols = {p.cli_format for p in usdc_pairs}
    assert "SOL/USDC" in symbols
    assert "BTC/USDC" in symbols
    assert "SOL/USD" not in symbols


def test_pairs_by_quote_btc():
    reg = default_registry()
    btc_pairs = reg.pairs_by_quote("BTC")
    symbols = {p.cli_format for p in btc_pairs}
    assert "SOL/BTC" in symbols


# ─── Asset normalization ───

def test_normalize_asset_xxbt_to_btc():
    assert normalize_asset("XXBT") == "BTC"
    assert normalize_asset("XBTC") == "BTC"
    assert normalize_asset("XBT") == "BTC"


def test_normalize_asset_zusd_to_usd():
    assert normalize_asset("ZUSD") == "USD"
    assert normalize_asset("ZUSDC") == "USDC"


def test_normalize_asset_strips_staked_suffix():
    # .B = bonded, .S = staked, .M = margin, .F = earn-flex
    assert normalize_asset("BTC.B") == "BTC"
    assert normalize_asset("BTC.S") == "BTC"
    assert normalize_asset("BTC.M") == "BTC"
    # USDC.F → USDC (this was the v2.16.2 dashboard $0 valuation bug)
    assert normalize_asset("USDC.F") == "USDC"


def test_normalize_asset_handles_z_prefix_then_suffix():
    # Kraken can return ZUSD.F (earn-parked USD)
    assert normalize_asset("ZUSD.F") == "USD"
    assert normalize_asset("ZUSDC.F") == "USDC"


def test_normalize_asset_passthrough_unknown():
    # Unknown asset codes pass through unchanged.
    assert normalize_asset("ETH") == "ETH"
    assert normalize_asset("DOGE") == "DOGE"


# ─── Bootstrap from kraken pairs ───

def test_bootstrap_updates_precision_in_place():
    reg = default_registry()
    before = reg.resolve("SOL/USD")
    # Simulate kraken pairs returning different precision (rare but possible).
    loaded = {
        "SOL/USD": {
            "price_decimals": 4,  # changed from 2
            "ordermin": 0.05,     # changed
            "costmin": 0.6,
            "base": "SOL",
            "quote": "USD",
            "lot_decimals": 8,
            "tick_size": "0.0001",
        }
    }
    reg.bootstrap_from_kraken(loaded)
    after = reg.resolve("SOL/USD")
    assert after.price_decimals == 4
    assert after.ordermin == 0.05
    assert after.costmin == 0.6
    assert after.tick_size == "0.0001"
    # Other fields preserved.
    assert after.base == before.base
    assert after.cli_format == before.cli_format


def test_bootstrap_unknown_pair_is_added():
    """If kraken pairs returns a pair we didn't pre-seed, we add it.
    This lets the agent discover new pairs without code changes."""
    reg = default_registry()
    loaded = {
        "ETH/USD": {
            "price_decimals": 2,
            "ordermin": 0.01,
            "costmin": 0.5,
            "base": "ETH",
            "quote": "USD",
            "lot_decimals": 8,
            "tick_size": None,
        }
    }
    reg.bootstrap_from_kraken(loaded)
    p = reg.resolve("ETH/USD")
    assert p.base == "ETH"
    assert p.quote == "USD"


def test_bootstrap_idempotent():
    reg = default_registry()
    loaded = {
        "SOL/USD": {
            "price_decimals": 2, "ordermin": 0.02, "costmin": 0.5,
            "base": "SOL", "quote": "USD", "lot_decimals": 8, "tick_size": None,
        }
    }
    reg.bootstrap_from_kraken(loaded)
    reg.bootstrap_from_kraken(loaded)
    p = reg.resolve("SOL/USD")
    assert p.price_decimals == 2  # unchanged after second apply


def test_bootstrap_empty_dict_is_noop():
    reg = default_registry()
    before = reg.resolve("SOL/USD")
    reg.bootstrap_from_kraken({})
    assert reg.resolve("SOL/USD") == before


# ─── Format helpers ───

def test_pair_format_price_at_native_precision():
    reg = default_registry()
    sol_usd = reg.resolve("SOL/USD")
    # SOL/USD precision = 2; rounding behavior must match _format_price
    assert sol_usd.format_price(150.123456) == "150.12000000"
    sol_btc = reg.resolve("SOL/BTC")
    # SOL/BTC precision = 7; round(0.001234567, 7) = 0.0012346 → "0.00123460"
    assert sol_btc.format_price(0.001234567) == "0.00123460"
    # Verify fewer-decimal input is preserved through round + 8dp formatting
    assert sol_btc.format_price(0.0012345) == "0.00123450"


def test_resolve_strip_whitespace():
    reg = default_registry()
    assert reg.resolve("  SOL/USD  ").cli_format == "SOL/USD"
