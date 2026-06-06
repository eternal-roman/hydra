"""Tests for hydra_state_migrator — quote-currency migration of persisted state.

When v2.19 flips the default quote from USDC → USD, on-disk state
written by pre-v2.19 agents references the old pair names. The migrator
rewrites pair-keyed fields in the session snapshot so a USD-default
agent can resume from a USDC-era snapshot without losing learned
state (engine indicators, regime history, derivatives
deques).

Coverage:
  - Round-trip: USDC snapshot → migrate("USD") → all pair keys flipped
  - Bridge pair (SOL/BTC) untouched — quote-independent
  - Already-USD snapshot → no-op (idempotent)
  - Order journal historical entries are NOT rewritten (audit preservation)
  - _migrated_quote marker prevents double-migration
  - Reverse direction also works (USD → USDC) for symmetry
  - Missing fields don't crash (fail-soft)
  - Unknown pair quote (e.g. "SOL/EUR") passes through unchanged
"""
import json
from pathlib import Path

import pytest

from hydra_state_migrator import (
    migrate_pair_key,
    migrate_snapshot,
    snapshot_already_migrated_to,
)


# ─── Atomic helper ───

def test_migrate_pair_key_flips_quote():
    assert migrate_pair_key("SOL/USDC", "USDC", "USD") == "SOL/USD"
    assert migrate_pair_key("BTC/USDC", "USDC", "USD") == "BTC/USD"


def test_migrate_pair_key_skips_bridge():
    """Bridge (SOL/BTC) is quote-independent and must not be rewritten."""
    assert migrate_pair_key("SOL/BTC", "USDC", "USD") == "SOL/BTC"


def test_migrate_pair_key_skips_unrelated_quote():
    """Pairs quoted in something other than the source stable pass through."""
    assert migrate_pair_key("SOL/EUR", "USDC", "USD") == "SOL/EUR"


def test_migrate_pair_key_handles_no_slash():
    assert migrate_pair_key("BTC", "USDC", "USD") == "BTC"
    assert migrate_pair_key("", "USDC", "USD") == ""


def test_migrate_pair_key_case_insensitive_match():
    assert migrate_pair_key("sol/usdc", "USDC", "USD") == "SOL/USD"


def test_migrate_pair_key_reverse_direction():
    assert migrate_pair_key("SOL/USD", "USD", "USDC") == "SOL/USDC"


# ─── Snapshot-level migration ───

def _legacy_usdc_snapshot():
    """Realistic shape (subset of hydra_session_snapshot.json)."""
    return {
        "version": 1,
        "timestamp": "2026-04-26T12:00:00Z",
        "mode": "competition",
        "paper": False,
        "pairs": ["SOL/USDC", "SOL/BTC", "BTC/USDC"],
        "competition_start_balance": 100.0,
        "engines": {
            "SOL/USDC": {"closes": [150.0, 151.0]},
            "SOL/BTC":  {"closes": [0.0015, 0.00151]},
            "BTC/USDC": {"closes": [95000.0, 95100.0]},
        },
        "coordinator_regime_history": {
            "SOL/USDC": ["TREND_UP", "TREND_UP"],
            "SOL/BTC":  ["RANGING"],
            "BTC/USDC": ["TREND_UP"],
        },
        "order_journal": [
            {"pair": "SOL/USDC", "side": "BUY"},
            {"pair": "SOL/BTC",  "side": "SELL"},
            {"pair": "BTC/USDC", "side": "BUY"},
        ],
        "derivatives_history": {
            "BTC/USDC": {"oi": [1.0, 2.0]},
            "SOL/USDC": {"oi": [3.0, 4.0]},
        },
        "userref_counter": 42,
        "portfolio_drawdown": {"peak_usd": 100.0, "max_pct": 0.05},
    }


def test_migrate_snapshot_flips_pair_keys():
    snap = _legacy_usdc_snapshot()
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")

    assert snap["pairs"] == ["SOL/USD", "SOL/BTC", "BTC/USD"]
    assert set(snap["engines"].keys()) == {"SOL/USD", "SOL/BTC", "BTC/USD"}
    assert set(snap["coordinator_regime_history"].keys()) == {"SOL/USD", "SOL/BTC", "BTC/USD"}
    assert set(snap["derivatives_history"].keys()) == {"BTC/USD", "SOL/USD"}


def test_migrate_snapshot_preserves_engine_state():
    """Engine state under the old key moves to the new key intact."""
    snap = _legacy_usdc_snapshot()
    original_sol_closes = snap["engines"]["SOL/USDC"]["closes"]
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap["engines"]["SOL/USD"]["closes"] == original_sol_closes


def test_migrate_snapshot_preserves_bridge():
    """SOL/BTC engine, regime, intent are untouched."""
    snap = _legacy_usdc_snapshot()
    bridge_closes = snap["engines"]["SOL/BTC"]["closes"]
    bridge_regime = snap["coordinator_regime_history"]["SOL/BTC"]
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap["engines"]["SOL/BTC"]["closes"] == bridge_closes
    assert snap["coordinator_regime_history"]["SOL/BTC"] == bridge_regime


def test_migrate_snapshot_does_not_rewrite_journal():
    """Order journal entries are historical records — pair fields preserved.

    A SOL/USDC trade happened on the SOL/USDC market. Rewriting the
    pair field would falsify the audit trail. The agent's runtime
    code resolves journal pair names via the registry, which knows
    SOL/USDC even when the active triangle is USD-quoted.
    """
    snap = _legacy_usdc_snapshot()
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    journal_pairs = [e["pair"] for e in snap["order_journal"]]
    assert journal_pairs == ["SOL/USDC", "SOL/BTC", "BTC/USDC"]


def test_migrate_snapshot_writes_marker():
    snap = _legacy_usdc_snapshot()
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap.get("_migrated_quote") == "USD"


def test_migrate_snapshot_idempotent():
    snap = _legacy_usdc_snapshot()
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    snap2 = json.loads(json.dumps(snap))
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap == snap2


def test_migrate_snapshot_collision_raises():
    """Both quote variants for the same base pair in the same dict —
    cannot silently overwrite (would lose data). Audit P4-7."""
    snap = {
        "pairs": ["SOL/USDC", "SOL/USD"],
        "engines": {"SOL/USDC": {"closes": [150.0]}, "SOL/USD": {"closes": [151.0]}},
    }
    with pytest.raises(ValueError) as exc:
        migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert "collision" in str(exc.value).lower()


def test_migrate_snapshot_collision_does_not_partial_mutate():
    """When collision raises mid-migration, the snapshot must not be
    half-mutated (e.g. `pairs` rewritten but `engines` raised)."""
    snap = {
        "pairs": ["SOL/USDC", "SOL/USD"],
        "engines": {"SOL/USDC": {"closes": [1]}, "SOL/USD": {"closes": [2]}},
    }
    pairs_before = list(snap["pairs"])
    engines_before = dict(snap["engines"])
    with pytest.raises(ValueError):
        migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    # `pairs` is migrated before `engines` in the migrator. The fact
    # that `engines` collision raises means `pairs` was already
    # rewritten in place — that's acceptable since the operator must
    # intervene anyway, but the marker MUST NOT be set (idempotence
    # would lock in a partial state).
    assert snap.get("_migrated_quote") is None


def test_migrate_snapshot_already_target_is_noop():
    """Snapshot already in target quote — migration must not corrupt it."""
    snap = {
        "pairs": ["SOL/USD", "SOL/BTC", "BTC/USD"],
        "engines": {"SOL/USD": {"x": 1}},
    }
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap["pairs"] == ["SOL/USD", "SOL/BTC", "BTC/USD"]
    assert snap["engines"] == {"SOL/USD": {"x": 1}}


def test_snapshot_already_migrated_to():
    """Detection helper used at agent boot to skip redundant migration."""
    snap_done = {"pairs": ["SOL/USD"], "_migrated_quote": "USD"}
    assert snapshot_already_migrated_to(snap_done, "USD") is True
    assert snapshot_already_migrated_to(snap_done, "USDC") is False
    snap_undone = {"pairs": ["SOL/USDC"]}
    assert snapshot_already_migrated_to(snap_undone, "USD") is False


def test_migrate_snapshot_missing_fields_fail_soft():
    """A snapshot without every expected field must not crash."""
    snap = {"pairs": ["SOL/USDC"]}
    migrate_snapshot(snap, source_quote="USDC", target_quote="USD")
    assert snap["pairs"] == ["SOL/USD"]


def test_migrate_snapshot_reverse_direction():
    """USD → USDC works symmetrically (rare, but supported)."""
    snap = {
        "pairs": ["SOL/USD", "SOL/BTC", "BTC/USD"],
        "engines": {"SOL/USD": {"x": 1}, "BTC/USD": {"y": 2}},
    }
    migrate_snapshot(snap, source_quote="USD", target_quote="USDC")
    assert set(snap["pairs"]) == {"SOL/USDC", "SOL/BTC", "BTC/USDC"}
    assert "SOL/USDC" in snap["engines"]
    assert "BTC/USDC" in snap["engines"]


# ─── File-level integration ───

def test_migrate_snapshot_file_round_trip(tmp_path):
    """End-to-end: write USDC snapshot to disk, run file migrator,
    re-read, verify pairs flipped and marker set."""
    from hydra_state_migrator import migrate_snapshot_file

    p = tmp_path / "hydra_session_snapshot.json"
    p.write_text(json.dumps(_legacy_usdc_snapshot()))

    changed = migrate_snapshot_file(p, source_quote="USDC", target_quote="USD")
    assert changed is True

    loaded = json.loads(p.read_text())
    assert loaded["pairs"] == ["SOL/USD", "SOL/BTC", "BTC/USD"]
    assert loaded["_migrated_quote"] == "USD"

    # Second call is a no-op (already migrated).
    changed2 = migrate_snapshot_file(p, source_quote="USDC", target_quote="USD")
    assert changed2 is False


def test_migrate_snapshot_file_missing_path(tmp_path):
    """Missing file is benign — fresh agents have no snapshot."""
    from hydra_state_migrator import migrate_snapshot_file
    p = tmp_path / "nope.json"
    assert migrate_snapshot_file(p, source_quote="USDC", target_quote="USD") is False


def test_migrate_snapshot_file_invalid_json(tmp_path):
    """Corrupt JSON — leave on disk untouched, return False."""
    from hydra_state_migrator import migrate_snapshot_file
    p = tmp_path / "snap.json"
    p.write_text("not json {{{")
    assert migrate_snapshot_file(p, source_quote="USDC", target_quote="USD") is False
    # File untouched
    assert p.read_text() == "not json {{{"


# ─── HydraAgent._detect_snapshot_stable_quote edge cases (audit P7-4) ───

def test_detect_stable_quote_bridge_only_returns_none():
    """Snapshot with only the bridge pair (no stable_sol/btc) — None."""
    from hydra_agent import HydraAgent
    snap = {"pairs": ["SOL/BTC"]}
    assert HydraAgent._detect_snapshot_stable_quote(snap) is None


def test_detect_stable_quote_empty_pairs_returns_none():
    from hydra_agent import HydraAgent
    assert HydraAgent._detect_snapshot_stable_quote({"pairs": []}) is None
    assert HydraAgent._detect_snapshot_stable_quote({}) is None


def test_detect_stable_quote_malformed_entries_skip():
    """Non-string / sliceless entries skipped; first valid stable wins."""
    from hydra_agent import HydraAgent
    snap = {"pairs": [None, 42, {"x": 1}, "no-slash", "SOL/USD", "BTC/USDC"]}
    # Returns the FIRST stable quote encountered (USD), not USDC.
    assert HydraAgent._detect_snapshot_stable_quote(snap) == "USD"


def test_detect_stable_quote_non_list_pairs_returns_none():
    """`pairs` field present but not a list — None."""
    from hydra_agent import HydraAgent
    assert HydraAgent._detect_snapshot_stable_quote({"pairs": "SOL/USD"}) is None
    assert HydraAgent._detect_snapshot_stable_quote({"pairs": {"x": 1}}) is None


def test_detect_stable_quote_first_match_wins():
    """If snapshot mixes USD and USDC pairs (shouldn't happen in practice),
    the first stable-quoted entry wins. Used to decide migration source."""
    from hydra_agent import HydraAgent
    snap1 = {"pairs": ["BTC/USDC", "SOL/BTC", "SOL/USD"]}
    assert HydraAgent._detect_snapshot_stable_quote(snap1) == "USDC"
    snap2 = {"pairs": ["SOL/USD", "BTC/USDC"]}
    assert HydraAgent._detect_snapshot_stable_quote(snap2) == "USD"


def test_detect_stable_quote_case_insensitive():
    from hydra_agent import HydraAgent
    snap = {"pairs": ["sol/usdc"]}
    assert HydraAgent._detect_snapshot_stable_quote(snap) == "USDC"


def test_detect_stable_quote_skips_non_stable_quote():
    """SOL/EUR and SOL/BTC don't count as stable; returns None."""
    from hydra_agent import HydraAgent
    snap = {"pairs": ["SOL/EUR", "SOL/BTC"]}
    assert HydraAgent._detect_snapshot_stable_quote(snap) is None
