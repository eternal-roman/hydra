"""Integration test: HydraAgent --resume across the v2.19 quote flip.

The state migrator unit tests cover migrate_snapshot in isolation. This
file verifies the AGENT WIRE-UP at `_load_snapshot`: a USDC-era snapshot
on disk, when loaded by a USD-default agent, must:
  1. Trigger migration
  2. Restore engine state under the new (USD) pair keys
  3. Reconcile the snapshot on disk so subsequent boots are no-ops

Tests use object.__new__(HydraAgent) to bypass the heavyweight __init__
(streams, kraken-cli probes, AI clients, dashboard server). Only the
attributes _load_snapshot reads are stubbed. This is the standard
pattern in tests/test_balance.py and tests/test_resume_reconcile.py.

Audit P7-2.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import HydraAgent
from hydra_engine import HydraEngine, CrossPairCoordinator
from hydra_config import HydraConfig


def _build_test_agent(tmp_path, quote: str) -> HydraAgent:
    """Construct a minimal agent shell sufficient for _load_snapshot."""
    cfg = HydraConfig.from_quote(quote)
    agent = object.__new__(HydraAgent)
    agent.pairs = list(cfg.pair_symbols())
    agent.triangle = cfg.triangle
    agent._snapshot_dir = str(tmp_path)
    # Engines keyed by the active triangle's pair names.
    agent.engines = {
        p: HydraEngine(initial_balance=100.0, asset=p) for p in agent.pairs
    }
    agent.coordinator = CrossPairCoordinator(agent.pairs)
    agent.order_journal = []
    agent._competition_start_balance = None
    agent._portfolio_peak_usd = 0.0
    agent._portfolio_max_drawdown_pct = 0.0
    agent._userref_counter = 0
    return agent


def _write_legacy_usdc_snapshot(path: str) -> dict:
    """Write a realistic USDC-era snapshot to disk and return what we wrote."""
    snap = {
        "version": 1,
        "timestamp": "2026-04-26T12:00:00Z",
        "mode": "competition",
        "paper": False,
        "pairs": ["SOL/USDC", "SOL/BTC", "BTC/USDC"],
        "competition_start_balance": 100.0,
        "engines": {
            "SOL/USDC": {
                "candles": [{"open": 150, "high": 151, "low": 149, "close": 150, "volume": 1000, "timestamp": 1.0}],
                "trades": [],
                "win_count": 0, "loss_count": 0,
            },
            "SOL/BTC": {
                "candles": [{"open": 0.0015, "high": 0.0016, "low": 0.0014, "close": 0.00155, "volume": 5, "timestamp": 1.0}],
                "trades": [],
                "win_count": 0, "loss_count": 0,
            },
            "BTC/USDC": {
                "candles": [{"open": 95000, "high": 95100, "low": 94900, "close": 95050, "volume": 0.5, "timestamp": 1.0}],
                "trades": [],
                "win_count": 0, "loss_count": 0,
            },
        },
        "coordinator_regime_history": {
            "SOL/USDC": ["TREND_UP", "TREND_UP"],
            "SOL/BTC":  ["RANGING"],
            "BTC/USDC": ["TREND_UP"],
        },
        "order_journal": [
            {"pair": "SOL/USDC", "side": "BUY", "lifecycle": {"state": "FILLED"}},
        ],
        "userref_counter": 42,
        "portfolio_drawdown": {"peak_usd": 110.0, "max_pct": 0.04},
    }
    with open(path, "w") as f:
        json.dump(snap, f)
    return snap


def test_load_snapshot_migrates_usdc_to_usd_in_place(tmp_path):
    """USD-default agent + USDC-era snapshot → migration runs, disk reconciled."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()

    # Disk should now have USD-keyed pairs
    on_disk = json.loads(open(snap_path).read())
    assert set(on_disk["pairs"]) == {"SOL/USD", "SOL/BTC", "BTC/USD"}
    assert set(on_disk["engines"].keys()) == {"SOL/USD", "SOL/BTC", "BTC/USD"}
    assert set(on_disk["coordinator_regime_history"].keys()) == {"SOL/USD", "SOL/BTC", "BTC/USD"}
    assert on_disk.get("_migrated_quote") == "USD"


def test_load_snapshot_preserves_engine_state_across_flip(tmp_path):
    """Engine internal state (regime history, candles) survives the migration."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()

    # SOL/USD engine inherited the SOL/USDC engine's state
    sol_engine = agent.engines["SOL/USD"]
    assert len(sol_engine.candles) == 1
    assert sol_engine.candles[0].close == 150

    # Coordinator regime history rewritten to USD keys
    assert agent.coordinator.regime_history["SOL/USD"] == ["TREND_UP", "TREND_UP"]
    assert agent.coordinator.regime_history["BTC/USD"] == ["TREND_UP"]
    assert agent.coordinator.regime_history["SOL/BTC"] == ["RANGING"]


def test_load_snapshot_preserves_journal_audit_trail(tmp_path):
    """Order journal pair fields preserved (audit trail)."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()
    # Journal entry's pair field is NOT rewritten — it represents a real
    # historical trade on the SOL/USDC market.
    assert agent.order_journal[0]["pair"] == "SOL/USDC"


def test_load_snapshot_preserves_drawdown_and_userref(tmp_path):
    """Non-pair-keyed fields survive intact."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()
    assert agent._portfolio_peak_usd == 110.0
    assert agent._portfolio_max_drawdown_pct == 0.04
    assert agent._userref_counter == 42
    assert agent._competition_start_balance == 100.0


def test_load_snapshot_idempotent_across_reboots(tmp_path):
    """Second boot of a USD agent reads the already-migrated snapshot
    and runs no migration (no log spam, no double-migration)."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()  # First boot — migrates
    on_disk_first = json.loads(open(snap_path).read())

    # Second boot, fresh agent shell
    agent2 = _build_test_agent(tmp_path, quote="USD")
    agent2._load_snapshot()
    on_disk_second = json.loads(open(snap_path).read())

    # Disk content unchanged after second boot (idempotent)
    assert on_disk_first == on_disk_second
    assert on_disk_second.get("_migrated_quote") == "USD"


def test_load_snapshot_no_migration_when_quote_matches(tmp_path):
    """USDC-default agent + USDC snapshot — no migration. No marker set."""
    agent = _build_test_agent(tmp_path, quote="USDC")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    snap_before = _write_legacy_usdc_snapshot(snap_path)

    agent._load_snapshot()

    on_disk = json.loads(open(snap_path).read())
    # No migration → disk untouched (no marker, original pair keys preserved)
    assert on_disk.get("_migrated_quote") is None
    assert "SOL/USDC" in on_disk["engines"]


def test_load_snapshot_missing_file_starts_fresh(tmp_path):
    """No snapshot file → no migration, no crash, agent starts fresh."""
    agent = _build_test_agent(tmp_path, quote="USD")
    # No file written
    agent._load_snapshot()
    # Engines retain their initial (empty) state — no exception.
    for engine in agent.engines.values():
        assert engine.candles == []


def test_load_snapshot_corrupt_file_starts_fresh(tmp_path):
    """Corrupt JSON → log + start fresh, no crash."""
    agent = _build_test_agent(tmp_path, quote="USD")
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    with open(snap_path, "w") as f:
        f.write("not json {{{{")
    agent._load_snapshot()
    # Engines retain their initial state — no crash.
    for engine in agent.engines.values():
        assert engine.candles == []


def _build_core_agent(tmp_path, pairs):
    """v2.29 three-core shell: no TradingTriangle, mixed leftover quotes."""
    agent = object.__new__(HydraAgent)
    agent.pairs = list(pairs)
    agent.triangle = None
    agent.candle_interval = 60
    agent._snapshot_dir = str(tmp_path)
    agent.engines = {
        p: HydraEngine(initial_balance=100.0, asset=p) for p in pairs
    }
    agent.coordinator = CrossPairCoordinator(pairs)
    agent.order_journal = []
    agent._competition_start_balance = None
    agent._portfolio_peak_usd = 0.0
    agent._portfolio_max_drawdown_pct = 0.0
    agent._portfolio_buy_halted = False
    agent._userref_counter = 0
    return agent


def test_load_snapshot_same_base_remap_when_triangle_is_none(tmp_path):
    """`--pairs auto` on a USDC-only book runs BTC/USDC+ETH/USDC+ZEC/USD
    with triangle=None. A previous-session USD snapshot must still
    restore BTC/ETH engines (positions, peak, halt). A global USD→USDC
    rewrite would rename ZEC/USD → ZEC/USDC and drop that engine."""
    pairs = ["BTC/USDC", "ETH/USDC", "ZEC/USD"]
    agent = _build_core_agent(tmp_path, pairs)
    snap_path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    snap = {
        "version": 1,
        "timestamp": "2026-08-14T12:00:00Z",
        "pairs": ["BTC/USD", "ETH/USD", "ZEC/USD"],
        "engines": {
            "BTC/USD": {
                "candles": [{"open": 95000, "high": 95100, "low": 94900,
                             "close": 95050, "volume": 0.5, "timestamp": 1.0}],
                "trades": [],
                "balance": 13534.0,
                "peak_equity": 14000.0,
                "max_drawdown": 0.03,
                "position": {"asset": "BTC/USD", "size": 0.085,
                             "avg_entry": 94000.0, "unrealized_pnl": 0.0,
                             "realized_pnl": 12.0},
                "win_count": 2, "loss_count": 1, "halted": False,
            },
            "ETH/USD": {
                "candles": [{"open": 3200, "high": 3210, "low": 3190,
                             "close": 3205, "volume": 2.0, "timestamp": 1.0}],
                "trades": [],
                "balance": 13534.0,
                "win_count": 0, "loss_count": 0,
            },
            "ZEC/USD": {
                "candles": [{"open": 40, "high": 41, "low": 39,
                             "close": 40.5, "volume": 10.0, "timestamp": 1.0}],
                "trades": [],
                "win_count": 0, "loss_count": 0,
            },
        },
        "coordinator_regime_history": {
            "BTC/USD": ["TREND_UP"],
            "ETH/USD": ["RANGING"],
            "ZEC/USD": ["VOLATILE"],
        },
        "order_journal": [
            {"pair": "BTC/USD", "side": "BUY",
             "lifecycle": {"state": "FILLED"}},
        ],
        "portfolio_drawdown": {"peak_usd": 28000.0, "max_pct": 0.03},
    }
    with open(snap_path, "w") as f:
        json.dump(snap, f)

    agent._load_snapshot()

    btc = agent.engines["BTC/USDC"]
    assert len(btc.candles) == 1
    assert btc.candles[0].close == 95050
    assert btc.balance == 13534.0
    assert btc.peak_equity == 14000.0
    assert btc.position.size == 0.085
    assert btc.position.avg_entry == 94000.0
    assert btc.position.asset == "BTC/USDC"
    eth = agent.engines["ETH/USDC"]
    assert len(eth.candles) == 1
    assert eth.candles[0].close == 3205
    zec = agent.engines["ZEC/USD"]
    assert len(zec.candles) == 1
    assert zec.candles[0].close == 40.5
    assert agent.coordinator.regime_history["BTC/USDC"] == ["TREND_UP"]
    assert agent.coordinator.regime_history["ETH/USDC"] == ["RANGING"]
    assert agent.coordinator.regime_history["ZEC/USD"] == ["VOLATILE"]
    # Journal stays on the market it actually traded.
    assert agent.order_journal[0]["pair"] == "BTC/USD"
    # Disk is not globally rewritten (that would invent ZEC/USDC).
    on_disk = json.loads(open(snap_path).read())
    assert "ZEC/USD" in on_disk["engines"]
    assert "ZEC/USDC" not in on_disk["engines"]


def test_same_base_stable_key_exact_wins_and_ambiguous_skips():
    assert HydraAgent._same_base_stable_key(
        {"BTC/USDC": 1}, "BTC/USDC") == "BTC/USDC"
    assert HydraAgent._same_base_stable_key(
        {"BTC/USD": 1}, "BTC/USDC") == "BTC/USD"
    assert HydraAgent._same_base_stable_key(
        {"BTC/USD": 1, "BTC/USDT": 1}, "BTC/USDC") is None
    assert HydraAgent._same_base_stable_key(
        {"SOL/BTC": 1}, "SOL/USDC") is None
    assert HydraAgent._same_base_stable_key(
        {"SOL/BTC": 1}, "SOL/BTC") == "SOL/BTC"
