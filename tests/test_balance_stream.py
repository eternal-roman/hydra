"""
HYDRA BalanceStream Test Suite
Validates WS balance message dispatch, asset normalization, currency filtering,
and latest_balances storage.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_streams import BalanceStream


def _make_stream(paper=False):
    return BalanceStream(paper=paper)


class TestBalanceStreamDispatch:

    def test_heartbeat_bumps_timestamp(self):
        bs = _make_stream()
        bs._last_heartbeat = 0.0
        bs._on_message({"channel": "heartbeat"})
        assert bs._last_heartbeat > time.monotonic() - 1.0

    def test_snapshot_stores_nonzero_balances(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances",
            "type": "snapshot",
            "data": [
                {"asset": "SOL", "balance": 1.44, "asset_class": "currency"},
                {"asset": "USDC", "balance": 432.70, "asset_class": "currency"},
                {"asset": "ETH", "balance": 0.0, "asset_class": "currency"},
            ],
        })
        bal = bs.latest_balances()
        assert bal["SOL"] == 1.44
        assert bal["USDC"] == 432.70
        assert "ETH" not in bal  # zero balance excluded

    def test_btc_stays_btc(self):
        """WS returns 'BTC' which is now the canonical form — no normalization needed."""
        bs = _make_stream()
        bs._on_message({
            "channel": "balances",
            "type": "snapshot",
            "data": [{"asset": "BTC", "balance": 0.003, "asset_class": "currency"}],
        })
        bal = bs.latest_balances()
        assert "BTC" in bal
        assert bal["BTC"] == 0.003

    def test_equities_filtered_out(self):
        """Equity/ETF assets should not appear in balances."""
        bs = _make_stream()
        bs._on_message({
            "channel": "balances",
            "type": "snapshot",
            "data": [
                {"asset": "USDC", "balance": 100.0, "asset_class": "currency"},
                {"asset": "AAPL", "balance": 5.0, "asset_class": "equity"},
                {"asset": "YBTC", "balance": 2.0, "asset_class": "equity"},
            ],
        })
        bal = bs.latest_balances()
        assert "USDC" in bal
        assert "AAPL" not in bal
        assert "YBTC" not in bal

    def test_update_overwrites_balance(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"asset": "USDC", "balance": 100.0, "asset_class": "currency"}],
        })
        bs._on_message({
            "channel": "balances", "type": "update",
            "data": [{"asset": "USDC", "balance": 200.0, "asset_class": "currency"}],
        })
        assert bs.latest_balances()["USDC"] == 200.0

    def test_balance_drops_to_zero_removed(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"asset": "SOL", "balance": 1.0, "asset_class": "currency"}],
        })
        assert "SOL" in bs.latest_balances()
        bs._on_message({
            "channel": "balances", "type": "update",
            "data": [{"asset": "SOL", "balance": 0.0, "asset_class": "currency"}],
        })
        assert "SOL" not in bs.latest_balances()

    def test_status_message_ignored(self):
        bs = _make_stream()
        bs._last_heartbeat = 12345.0
        bs._on_message({"channel": "status", "data": [{"system": "online"}]})
        assert bs._last_heartbeat == 12345.0

    def test_non_dict_data_entries_skipped(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": ["not a dict", None, 42],
        })
        assert bs.latest_balances() == {}

    def test_missing_asset_skipped(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"balance": 100.0, "asset_class": "currency"}],  # no "asset"
        })
        assert bs.latest_balances() == {}

    def test_latest_balances_returns_empty_before_data(self):
        bs = _make_stream()
        assert bs.latest_balances() == {}

    def test_balances_channel_bumps_heartbeat(self):
        bs = _make_stream()
        bs._last_heartbeat = 0.0
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"asset": "USDC", "balance": 100.0, "asset_class": "currency"}],
        })
        assert bs._last_heartbeat > time.monotonic() - 1.0

    def test_default_asset_class_is_currency(self):
        """If asset_class is missing, default to 'currency' (include it)."""
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"asset": "USDC", "balance": 50.0}],  # no asset_class
        })
        assert bs.latest_balances()["USDC"] == 50.0

    def test_multiple_assets_independent(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [
                {"asset": "SOL", "balance": 1.0, "asset_class": "currency"},
                {"asset": "BTC", "balance": 0.003, "asset_class": "currency"},
                {"asset": "USDC", "balance": 500.0, "asset_class": "currency"},
            ],
        })
        bal = bs.latest_balances()
        assert bal["SOL"] == 1.0
        assert bal["BTC"] == 0.003
        assert bal["USDC"] == 500.0

    def test_free_unknown_before_snapshot(self):
        bs = _make_stream()
        assert bs.latest_free_balances() is None

    def test_full_hold_free_is_zero_gross_remains(self):
        """Hold covers gross. Free stays 0 in the map; gross is unchanged."""
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{
                "asset": "USD", "balance": 100.0, "hold_trade": 100.0,
                "asset_class": "currency",
            }],
        })
        assert bs.latest_balances()["USD"] == 100.0
        assert bs.latest_free_balances() == {"USD": 0.0}

    def test_absent_hold_free_equals_gross(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{"asset": "USD", "balance": 12.5, "asset_class": "currency"}],
        })
        assert bs.latest_free_balances()["USD"] == 12.5
        assert bs.latest_balances()["USD"] == 12.5

    def test_partial_hold_nets_free(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [{
                "asset": "USD", "balance": 100.0, "hold_trade": 40.0,
                "asset_class": "currency",
            }],
        })
        assert bs.latest_balances()["USD"] == 100.0
        assert bs.latest_free_balances()["USD"] == 60.0

    def test_update_before_snapshot_is_partial(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "balances", "type": "update",
            "data": [{
                "asset": "USD", "balance": 100.0, "hold_trade": 100.0,
                "asset_class": "currency",
            }],
        })
        assert bs.free_view_is_complete() is False
        assert bs.latest_free_balances()["USD"] == 0.0
        bs._on_message({
            "channel": "balances", "type": "snapshot",
            "data": [
                {
                    "asset": "USD", "balance": 100.0, "hold_trade": 100.0,
                    "asset_class": "currency",
                },
                {"asset": "BTC", "balance": 0.01, "asset_class": "currency"},
            ],
        })
        assert bs.free_view_is_complete() is True
        free = bs.latest_free_balances()
        assert free["USD"] == 0.0
        assert free["BTC"] == 0.01
        assert bs.latest_balances()["USD"] == 100.0

    def test_empty_snapshot_is_known_empty_free(self):
        bs = _make_stream()
        bs._on_message({"channel": "balances", "type": "snapshot", "data": []})
        assert bs.latest_free_balances() == {}
        assert bs.latest_balances() == {}


class TestBalanceStreamBuildCmd:

    def test_build_cmd(self):
        bs = _make_stream()
        cmd = bs._build_cmd()
        assert "ws balances" in cmd
        assert "-o json" in cmd
        assert "--snapshot true" in cmd
        assert cmd.startswith("exec ")


class TestBalanceStreamPaper:

    def test_paper_mode_healthy(self):
        bs = _make_stream(paper=True)
        bs.start()
        assert bs.healthy

    def test_paper_start_returns_true(self):
        bs = _make_stream(paper=True)
        assert bs.start() is True

    def test_paper_latest_balances_empty(self):
        bs = _make_stream(paper=True)
        bs.start()
        assert bs.latest_balances() == {}


class TestBalanceStreamLabel:

    def test_label(self):
        bs = _make_stream()
        assert bs._stream_label() == "BALANCE_WS"


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ���══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestBalanceStreamDispatch,
        TestBalanceStreamBuildCmd,
        TestBalanceStreamPaper,
        TestBalanceStreamLabel,
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
    print(f"  BalanceStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)


def test_reconnect_snapshot_drops_assets_absent_from_it():
    """An asset sold to zero while the stream was down is absent from the
    reconnect snapshot. Merging kept its stale balance (inflated equity and
    the sticky portfolio peak, phantom base for the SELL preflight)."""
    from hydra_streams import BalanceStream
    s = BalanceStream(paper=True)
    s._on_message({"channel": "balances", "type": "snapshot", "data": [
        {"asset": "USD", "balance": 1000.0}, {"asset": "BTC", "balance": 0.05}]})
    assert s.latest_balances() == {"USD": 1000.0, "BTC": 0.05}
    s._on_message({"channel": "balances", "type": "snapshot", "data": [
        {"asset": "USD", "balance": 3100.0}]})
    assert s.latest_balances() == {"USD": 3100.0}
    assert s.latest_free_balances() == {"USD": 3100.0}


def test_update_still_merges_into_the_last_snapshot():
    from hydra_streams import BalanceStream
    s = BalanceStream(paper=True)
    s._on_message({"channel": "balances", "type": "snapshot", "data": [
        {"asset": "USD", "balance": 1000.0}, {"asset": "BTC", "balance": 0.05}]})
    s._on_message({"channel": "balances", "type": "update", "data": [
        {"asset": "BTC", "balance": 0.07}]})
    assert s.latest_balances() == {"USD": 1000.0, "BTC": 0.07}
