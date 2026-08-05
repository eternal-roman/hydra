"""
HYDRA KrakenCLI Wrapper Test Suite
Validates argument construction, error passthrough, and response parsing for
the volume wrappers, plus the fee-tier extraction helper on HydraAgent. No
subprocess calls are made — all tests monkey-patch KrakenCLI._run with an
in-memory stub.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import HydraAgent
from hydra_kraken_cli import KrakenCLI


# ═══════════════════════════════════════════════════════════════
# Stub helper — temporarily replaces KrakenCLI._run with a recorder
# ═══════════════════════════════════════════════════════════════

class _StubRun:
    """Records calls to KrakenCLI._run and returns a preset response.
    Must be restored in a try/finally so sibling tests are not affected."""

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


def _with_stub(response, fn):
    """Run fn() with KrakenCLI._run stubbed. Returns (result, stub)."""
    stub = _StubRun(response)
    stub.install()
    try:
        result = fn()
    finally:
        stub.restore()
    return result, stub


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.volume — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestVolumeArgsAndParsing:
    def test_volume_no_args_calls_bare_command(self):
        _, stub = _with_stub({"volume": "1234.5"}, lambda: KrakenCLI.volume())
        assert stub.calls == [["volume"]]

    def test_volume_with_none_calls_bare_command(self):
        _, stub = _with_stub({"volume": "1234.5"}, lambda: KrakenCLI.volume(None))
        assert stub.calls == [["volume"]]

    def test_volume_with_single_pair_string(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.volume("SOL/USDC"))
        assert stub.calls == [["volume", "--pair", "SOL/USDC"]]

    def test_volume_with_pair_list(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.volume(["SOL/USDC", "BTC/USDC"]))
        # BTC/USDC resolves to BTC/USDC (canonical) via the PairRegistry
        assert stub.calls == [["volume", "--pair", "SOL/USDC,BTC/USDC"]]

    def test_volume_resolves_pair_map(self):
        # SOL/BTC should resolve to SOL/BTC
        _, stub = _with_stub({}, lambda: KrakenCLI.volume(["SOL/BTC"]))
        assert stub.calls == [["volume", "--pair", "SOL/BTC"]]

    def test_volume_returns_passthrough_on_success(self):
        payload = {"volume": "500.00", "fees": {"SOLUSDC": {"fee": "0.26"}}}
        result, _ = _with_stub(payload, lambda: KrakenCLI.volume())
        assert result == payload

    def test_volume_returns_error_dict_on_error(self):
        err = {"error": "EAPI:Invalid key"}
        result, _ = _with_stub(err, lambda: KrakenCLI.volume())
        assert result == err

    def test_volume_handles_timeout_payload(self):
        timeout = {"error": "Command timed out", "retryable": True}
        result, _ = _with_stub(timeout, lambda: KrakenCLI.volume(["SOL/USDC"]))
        assert result == timeout


# ═══════════════════════════════════════════════════════════════
# TEST: HF-001 — KrakenCLI._format_price pair-aware precision
# ═══════════════════════════════════════════════════════════════

class TestPriceFormat:
    """Regression tests for HF-001 (pair-aware price precision).

    Kraken rejects orders whose price has more meaningful decimals than
    the pair's native precision. _format_price rounds to the correct
    number of decimals per pair before the .8f format. The precision
    table now lives on the PairRegistry (`Pair.price_decimals`)."""

    def test_solusdc_rounds_to_2_decimals(self):
        # 80.4745 would fail live Kraken with "price can only be specified up to 2 decimals"
        assert KrakenCLI._format_price("SOL/USDC", 80.4745) == "80.47000000"

    def test_solusdc_exact_2dp_preserved(self):
        assert KrakenCLI._format_price("SOL/USDC", 84.71) == "84.71000000"

    def test_solusdc_rounds_up_unambiguous(self):
        # 80.476 is unambiguously above .475, avoids float-representation of 80.475
        # (which is actually ~80.4749999... in float, so banker's rounding goes down)
        assert KrakenCLI._format_price("SOL/USDC", 80.476) == "80.48000000"

    def test_btcusdc_rounds_to_1_decimal(self):
        assert KrakenCLI._format_price("BTC/USDC", 73031.94) == "73031.90000000"

    def test_btcusdc_exact_1dp_preserved(self):
        assert KrakenCLI._format_price("BTC/USDC", 72858.7) == "72858.70000000"

    def test_solbtc_rounds_to_7_decimals(self):
        # 0.00116523 has 8 meaningful decimals → must round to 7
        assert KrakenCLI._format_price("SOL/BTC", 0.00116523) == "0.00116520"

    def test_solbtc_exact_7dp_preserved(self):
        assert KrakenCLI._format_price("SOL/BTC", 0.0011629) == "0.00116290"

    def test_unknown_pair_falls_back_to_8dp(self):
        assert KrakenCLI._format_price("UNKNOWN/PAIR", 1.234567890123) == "1.23456789"

    def test_slashless_form_accepted(self):
        # "SOLUSDC" should resolve to the same 2-decimal precision as "SOL/USDC"
        assert KrakenCLI._format_price("SOLUSDC", 80.4745) == "80.47000000"

    def test_integer_price_preserved(self):
        assert KrakenCLI._format_price("SOL/USDC", 100) == "100.00000000"

    def test_zero_price_preserved(self):
        assert KrakenCLI._format_price("SOL/USDC", 0.0) == "0.00000000"

    def test_order_buy_uses_rounded_price(self):
        # Integration: order_buy on SOL/USDC with a 4-decimal price should
        # end up with 2-decimal precision in the --price arg.
        _, stub = _with_stub({"txid": ["ABC"]},
                              lambda: KrakenCLI.order_buy("SOL/USDC", 0.02, price=80.4745))
        call = stub.calls[0]
        assert "--price" in call
        price_idx = call.index("--price")
        assert call[price_idx + 1] == "80.47000000", f"got {call[price_idx+1]!r}"

    def test_order_sell_uses_rounded_price(self):
        _, stub = _with_stub({"txid": ["ABC"]},
                              lambda: KrakenCLI.order_sell("BTC/USDC", 0.00005, price=73031.94))
        call = stub.calls[0]
        price_idx = call.index("--price")
        assert call[price_idx + 1] == "73031.90000000"



# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.system_status — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.asset_pairs — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.query_orders — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestQueryOrders:
    def test_query_orders_txids(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.query_orders("TXID1", "TXID2"))
        assert stub.calls == [["query-orders", "TXID1", "TXID2"]]

    def test_query_orders_userref(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.query_orders(userref=12345))
        assert stub.calls == [["query-orders", "--userref", "12345"]]

    def test_query_orders_trades_flag(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.query_orders("TX1", trades=True))
        assert stub.calls == [["query-orders", "TX1", "--trades"]]

    def test_query_orders_combined(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.query_orders("TX1", userref=99, trades=True))
        assert stub.calls == [["query-orders", "TX1", "--userref", "99", "--trades"]]

    def test_query_orders_error_passthrough(self):
        err = {"error": "EOrder:Unknown order"}
        result, _ = _with_stub(err, lambda: KrakenCLI.query_orders("FAKE"))
        assert result == err


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.cancel_order — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestCancelOrder:
    def test_cancel_order_single_txid(self):
        _, stub = _with_stub({"count": 1}, lambda: KrakenCLI.cancel_order("TXID1"))
        assert stub.calls == [["order", "cancel", "TXID1", "--yes"]]

    def test_cancel_order_multiple_txids(self):
        _, stub = _with_stub({"count": 2}, lambda: KrakenCLI.cancel_order("TX1", "TX2"))
        assert stub.calls == [["order", "cancel", "TX1", "TX2", "--yes"]]

    def test_cancel_order_error_passthrough(self):
        err = {"error": "EOrder:Unknown order"}
        result, _ = _with_stub(err, lambda: KrakenCLI.cancel_order("FAKE"))
        assert result == err


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.trades_history — argument construction
# ═══════════════════════════════════════════════════════════════

class TestTradesHistory:
    def test_trades_history_no_args(self):
        _, stub = _with_stub({"trades": {}}, lambda: KrakenCLI.trades_history())
        assert stub.calls == [["trades-history"]]

    def test_trades_history_with_start(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.trades_history(start=1700000000))
        assert stub.calls == [["trades-history", "--start", "1700000000"]]

    def test_trades_history_with_start_and_end(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.trades_history(start=1700000000, end=1700100000))
        assert stub.calls == [["trades-history", "--start", "1700000000", "--end", "1700100000"]]

    def test_trades_history_error_passthrough(self):
        err = {"error": "EAPI:Rate limit"}
        result, _ = _with_stub(err, lambda: KrakenCLI.trades_history())
        assert result == err


class TestAssetPairs:
    def test_asset_pairs_no_filter(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.asset_pairs())
        assert stub.calls == [["pairs"]]

    def test_asset_pairs_with_pair_list(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.asset_pairs(["SOL/USDC", "SOL/BTC", "BTC/USDC"]))
        assert stub.calls == [["pairs", "--pair", "SOL/USDC,SOL/BTC,BTC/USDC"]]

    def test_asset_pairs_returns_payload(self):
        payload = {"SOL/USDC": {"pair_decimals": 2, "ordermin": "0.02"}}
        result, _ = _with_stub(payload, lambda: KrakenCLI.asset_pairs())
        assert result == payload

    def test_asset_pairs_error_passthrough(self):
        err = {"error": "EQuery:Unknown asset pair"}
        result, _ = _with_stub(err, lambda: KrakenCLI.asset_pairs(["FAKE/PAIR"]))
        assert result == err


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.system_status — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestSystemStatus:
    def test_system_status_calls_bare_command(self):
        _, stub = _with_stub({"status": "online", "timestamp": "2026-04-12T20:35:55Z"},
                              lambda: KrakenCLI.system_status())
        assert stub.calls == [["status"]]

    def test_system_status_returns_payload(self):
        payload = {"status": "online", "timestamp": "2026-04-12T20:35:55Z"}
        result, _ = _with_stub(payload, lambda: KrakenCLI.system_status())
        assert result == payload

    def test_system_status_maintenance_passthrough(self):
        payload = {"status": "maintenance", "timestamp": "2026-04-12T21:00:00Z"}
        result, _ = _with_stub(payload, lambda: KrakenCLI.system_status())
        assert result["status"] == "maintenance"

    def test_system_status_error_passthrough(self):
        err = {"error": "Command timed out", "retryable": True}
        result, _ = _with_stub(err, lambda: KrakenCLI.system_status())
        assert result == err


# ═══════════════════════════════════════════════════════════════
# TEST: HydraAgent._extract_fee_tier — defensive parsing
# ═══════════════════════════════════════════════════════════════

class TestFeeTierExtraction:
    def _make_agent(self, pairs=None):
        agent = object.__new__(HydraAgent)
        agent.pairs = pairs if pairs is not None else ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        return agent

    def test_extract_fee_tier_empty_response(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({})
        assert result == {"volume_30d_usd": None, "pair_fees": {}}

    def test_extract_fee_tier_non_dict_response(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier(["unexpected", "list"])
        assert result == {"volume_30d_usd": None, "pair_fees": {}}

    def test_extract_fee_tier_taker_only(self):
        agent = self._make_agent()
        response = {
            "volume": "100.0",
            "fees": {"SOLUSDC": {"fee": "0.26"}},
        }
        result = agent._extract_fee_tier(response)
        assert result["volume_30d_usd"] == 100.0
        # Slashless "SOLUSDC" must be mapped back to friendly "SOL/USDC"
        # (this is the path the dashboard uses to look up fees by pair key)
        assert "SOL/USDC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] == 0.26
        assert result["pair_fees"]["SOL/USDC"]["maker_pct"] is None

    def test_extract_fee_tier_maker_and_taker(self):
        agent = self._make_agent()
        response = {
            "volume": "250.5",
            "fees": {"XBTUSDC": {"fee": "0.26"}},
            "fees_maker": {"XBTUSDC": {"fee": "0.16"}},
        }
        result = agent._extract_fee_tier(response)
        # XBTUSDC reverse-maps back to BTC/USDC (first pair in list that resolves to XBTUSDC)
        assert "BTC/USDC" in result["pair_fees"]
        assert result["pair_fees"]["BTC/USDC"]["taker_pct"] == 0.26
        assert result["pair_fees"]["BTC/USDC"]["maker_pct"] == 0.16

    def test_extract_fee_tier_volume_parsed_float(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({"volume": "1234.567"})
        assert result["volume_30d_usd"] == 1234.567

    def test_extract_fee_tier_malformed_volume_is_none(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({"volume": "not-a-number"})
        assert result["volume_30d_usd"] is None

    def test_extract_fee_tier_malformed_fee_is_none(self):
        agent = self._make_agent()
        response = {"fees": {"SOLUSDC": {"fee": "garbage"}}}
        result = agent._extract_fee_tier(response)
        # After slashless fix, "SOLUSDC" maps back to "SOL/USDC"
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] is None

    def test_extract_fee_tier_reverse_maps_sol_btc(self):
        agent = self._make_agent()
        # SOLXBT resolved → SOL/BTC friendly
        response = {"fees": {"SOLXBT": {"fee": "0.20"}}}
        result = agent._extract_fee_tier(response)
        assert "SOL/BTC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/BTC"]["taker_pct"] == 0.20

    def test_extract_fee_tier_slashed_form_also_maps(self):
        """Kraken may also return keys in already-slashed form like 'SOL/USDC'."""
        agent = self._make_agent()
        response = {"fees": {"SOL/USDC": {"fee": "0.26"}}}
        result = agent._extract_fee_tier(response)
        assert "SOL/USDC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] == 0.26

    def test_extract_fee_tier_missing_pairs_attr(self):
        """Agent without `pairs` set should still return a valid dict.

        v2.19+ improvement: the registry-based resolution canonicalizes
        the key regardless of whether the agent's `pairs` attr is set.
        Pre-v2.19 the key passed through unchanged when pairs was missing,
        leaking Kraken's slashless dialect into downstream consumers.
        """
        agent = object.__new__(HydraAgent)
        # deliberately no pairs attr
        result = agent._extract_fee_tier({"fees": {"SOLUSDC": {"fee": "0.26"}}})
        # Canonicalized via registry — SOLUSDC is a known alias for SOL/USDC.
        assert "SOL/USDC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] == 0.26


# ═══════════════════════════════════════════════════════════════
# Shell-injection hardening (v2.15.0)
# ═══════════════════════════════════════════════════════════════

class TestShellInjection:
    """KrakenCLI._run builds a `bash -c` string. v2.15.0 runs every
    arg through shlex.quote so a crafted string cannot escape into
    the shell. These tests cover the injection boundary without
    actually invoking subprocess — they monkey-patch subprocess.run
    and inspect the cmd vector that would have been executed."""

    def _capture_cmd(self, args):
        import subprocess as _sp
        captured = {}

        class _FakeCompleted:
            stdout = '{"ok":1}'
            returncode = 0

        def fake_run(cmd, **_kw):
            captured["cmd"] = list(cmd)
            return _FakeCompleted()

        orig = _sp.run
        _sp.run = fake_run
        try:
            KrakenCLI._run(args)
        finally:
            _sp.run = orig
        return captured["cmd"]

    def test_metachar_args_are_quoted(self):
        payload = "SOL/USDC; rm -rf /tmp/pwn"
        cmd = self._capture_cmd(["add_order", "--pair", payload])
        bash_str = cmd[-1]
        # The injected payload must appear wrapped in single quotes
        # (shlex.quote form) so bash sees it as one literal token.
        assert f"'{payload}'" in bash_str
        # And the outer command still routes through `kraken`.
        assert " kraken " in bash_str

    def test_backtick_command_substitution_quoted(self):
        cmd = self._capture_cmd(["add_order", "--pair", "`id`"])
        bash_str = cmd[-1]
        # shlex.quote wraps backticks in single quotes so the shell
        # never evaluates them
        assert "'`id`'" in bash_str

    def test_dollar_paren_quoted(self):
        cmd = self._capture_cmd(["add_order", "--pair", "$(cat /etc/passwd)"])
        bash_str = cmd[-1]
        assert "'$(cat /etc/passwd)'" in bash_str


class TestPaperArgv:
    """v2.32: `kraken paper buy|sell` takes VOLUME positionally.

    Hydra previously sent `--volume <V>`, which the CLI rejects (unexpected
    argument + missing required positional), so every paper order failed at
    the CLI boundary. Nothing caught it because the whole suite — and the
    live harness — stubs `KrakenCLI._run`, i.e. above the argv construction.
    These tests assert on the argv itself, which is the contract that
    actually has to match upstream (agents/tool-catalog.json).
    """

    def test_volume_is_positional_not_a_flag(self):
        args = KrakenCLI._paper_args("buy", "BTC/USD", 0.01, "limit", 50000.0)
        assert "--volume" not in args, f"--volume is not an upstream flag: {args}"
        # pair then volume, both positional, immediately after the subcommand
        assert args[:3] == ["paper", "buy", "BTC/USD"], args
        assert float(args[3]) == 0.01, args

    def test_limit_order_carries_price(self):
        args = KrakenCLI._paper_args("sell", "BTC/USD", 0.5, "limit", 70000.0)
        assert "--type" in args and args[args.index("--type") + 1] == "limit"
        # upstream rejects --type limit with no --price
        assert "--price" in args, args
        assert float(args[args.index("--price") + 1]) == 70000.0

    def test_limit_without_price_degrades_to_market(self):
        # Rather than emit a guaranteed-invalid `--type limit` with no price.
        args = KrakenCLI._paper_args("buy", "BTC/USD", 0.01, "limit", None)
        assert args[args.index("--type") + 1] == "market"
        assert "--price" not in args, args

    def test_paper_buy_end_to_end_argv(self):
        _, stub = _with_stub({"status": "success"},
                             lambda: KrakenCLI.paper_buy("BTC/USD", 0.01,
                                                         price=50000.0))
        sent = stub.calls[0]
        assert "--volume" not in sent, sent
        assert sent[3] == f"{0.01:.8f}", sent


class TestErrorEnvelope:
    """kraken-cli returns {"error": "<category>", ...} where `error` is a
    CATEGORY SLUG (AGENTS.md), not a sentence. Hydra must be able to tell a
    retryable transport blip from a hard validation rejection."""

    def test_upstream_category_marked_retryable(self):
        from hydra_kraken_cli import _normalize_error
        out = _normalize_error({"error": "rate_limit", "message": "slow down"})
        assert out["error"] == "rate_limit"          # unchanged for old callers
        assert out["error_category"] == "rate_limit"
        assert out["retryable"] is True

    def test_validation_is_not_retryable(self):
        from hydra_kraken_cli import _normalize_error
        out = _normalize_error({"error": "validation", "message": "bad price"})
        assert out["retryable"] is False

    def test_explicit_retryable_wins_over_category_default(self):
        from hydra_kraken_cli import _normalize_error
        out = _normalize_error({"error": "network", "retryable": False})
        assert out["retryable"] is False

    def test_describe_error_keeps_the_actionable_message(self):
        from hydra_kraken_cli import _normalize_error, describe_error
        text = describe_error(_normalize_error(
            {"error": "validation", "message": "price has too many decimals"}))
        assert "validation" in text and "too many decimals" in text

    def test_order_placement_is_never_auto_retried(self):
        # An ambiguous timeout on a placement can double-place; only
        # read-only commands may be retried.
        assert KrakenCLI._is_retry_safe(["order", "buy", "BTC/USD", "1"]) is False
        assert KrakenCLI._is_retry_safe(["order", "cancel", "TX1"]) is False
        assert KrakenCLI._is_retry_safe(["paper", "buy", "BTC/USD", "1"]) is False
        assert KrakenCLI._is_retry_safe(["ticker", "BTC/USD"]) is True
        assert KrakenCLI._is_retry_safe(["balance"]) is True
        assert KrakenCLI._is_retry_safe(["futures", "tickers"]) is True


class TestFreeBalance:
    """Gross balance counts funds locked behind our own resting post-only
    orders; sizing against it re-spends committed money."""

    def test_held_is_netted_out(self):
        data = {"USD": {"balance": "100.0", "hold_trade": "40.0"}}
        _, _ = _with_stub(data, lambda: None)
        assert KrakenCLI._extract_free(data["USD"]) == 60.0

    def test_unknown_shape_fails_open_to_none(self):
        assert KrakenCLI._extract_free({"credit": "5"}) is None
        assert KrakenCLI._extract_free(None) is None

    def test_plain_scalar_balance_still_parses(self):
        assert KrakenCLI._extract_free("12.5") == 12.5

    def test_hold_exceeding_balance_floors_at_zero(self):
        assert KrakenCLI._extract_free({"balance": "1.0", "hold_trade": "5.0"}) == 0.0

    def test_free_balance_returns_empty_on_cli_error(self):
        result, _ = _with_stub({"error": "auth"}, KrakenCLI.free_balance)
        assert result == {}


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestVolumeArgsAndParsing,
        TestPriceFormat,
        TestQueryOrders,
        TestCancelOrder,
        TestTradesHistory,
        TestAssetPairs,
        TestSystemStatus,
        TestFeeTierExtraction,
        TestShellInjection,
        TestPaperArgv,
        TestErrorEnvelope,
        TestFreeBalance,
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
    print(f"  Kraken CLI Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
