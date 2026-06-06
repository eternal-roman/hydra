"""Hydra state migrator — quote-currency migration of persisted snapshots.

WHY THIS MODULE EXISTS
──────────────────────
v2.19 flips the default stable quote from USDC → USD. On-disk state
written by pre-v2.19 agents (session snapshot, derivatives history)
is keyed by old pair names. Without migration, a
USD-default agent booting `--resume` would build engines under new
keys (SOL/USD, BTC/USD) and silently abandon the learned engine
state, regime history, and OI deques captured under the old keys
(SOL/USDC, BTC/USDC).

This migrator rewrites the affected fields in place, idempotently,
preserving the engine learnings while letting the agent boot under
its new default.

WHAT IS MIGRATED, WHAT IS NOT
──────────────────────────────
Migrated (active runtime state under stale keys):
  - `pairs`                    — list of active pair symbols
  - `engines`                  — dict keyed by pair (engine snapshots)
  - `coordinator_regime_history` — dict keyed by pair
  - `derivatives_history`      — dict keyed by pair (OI / mark-price deques)

Preserved (historical record):
  - `order_journal[*].pair`    — A SOL/USDC trade was placed on the
                                 SOL/USDC market. Rewriting the field
                                 would falsify the audit trail.

The bridge pair (`SOL/BTC`) is quote-independent and never rewritten.
Pairs quoted in something other than the source stable (e.g. SOL/EUR)
pass through unchanged.

INVARIANTS
──────────
- Idempotent: running the migrator twice on the same input produces
  the same output. The `_migrated_quote` marker tracks state.
- Fail-soft on file/JSON errors — corrupt or missing snapshot leaves
  disk untouched and returns False; the agent's regular load path
  decides what to do (typically: log + continue with empty state).
- Symmetric: works in both directions (USDC→USD and USD→USDC).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Union


_PathLike = Union[str, os.PathLike, Path]


# ═══════════════════════════════════════════════════════════════════
# Atomic helper
# ═══════════════════════════════════════════════════════════════════

def migrate_pair_key(pair: str, source_quote: str, target_quote: str) -> str:
    """Rewrite a pair symbol's quote currency.

    Returns the input unchanged when:
      - the input has no slash (single asset, not a pair)
      - the quote portion doesn't match the source quote
      - the input is empty / falsy

    The base portion is preserved verbatim. This means migrating
    SOL/USDC → USD yields SOL/USD; SOL/BTC stays SOL/BTC; SOL/EUR
    stays SOL/EUR.
    """
    if not pair or "/" not in pair:
        return pair
    base, quote = pair.split("/", 1)
    src = (source_quote or "").upper()
    tgt = (target_quote or "").upper()
    if quote.upper() == src:
        return f"{base.upper()}/{tgt}"
    return pair


# ═══════════════════════════════════════════════════════════════════
# Snapshot-level migration (in-memory dict)
# ═══════════════════════════════════════════════════════════════════

def _remap_keys(d: Dict[str, Any], src: str, tgt: str) -> Dict[str, Any]:
    """Return a new dict with pair keys rewritten. Preserves insertion order.

    Raises ValueError on a *real* collision — i.e. two distinct input
    keys map to the same output key. This means the snapshot contains
    both quote variants for the same base pair (e.g. both `SOL/USDC`
    AND `SOL/USD`); silently letting the second overwrite the first
    would lose data. Such a collision can only happen via manual edit
    of the snapshot file.

    Implementation: track provenance (output key → first input key
    that produced it). A second input key landing on a known output
    key with a different provenance is the collision signal. Same
    input key reused (impossible in a Python dict, but guarded for
    safety) is not a collision.
    """
    out: Dict[str, Any] = {}
    provenance: Dict[str, Any] = {}
    collisions: list = []
    for k, v in d.items():
        new_k = migrate_pair_key(k, src, tgt) if isinstance(k, str) else k
        if new_k in out and provenance.get(new_k) != k:
            collisions.append((provenance[new_k], k, new_k))
        out[new_k] = v
        provenance[new_k] = k
    if collisions:
        raise ValueError(
            f"Pair-key migration collision: cannot remap "
            f"{collisions} — both source and target keys present in "
            f"the same dict. Manual snapshot edit required."
        )
    return out


def migrate_snapshot(
    snapshot: Dict[str, Any],
    source_quote: str,
    target_quote: str,
) -> None:
    """Migrate a snapshot dict in place.

    Idempotent: if the snapshot is already marked as migrated to
    `target_quote`, this is a no-op.
    """
    if snapshot_already_migrated_to(snapshot, target_quote):
        return

    src = (source_quote or "").upper()
    tgt = (target_quote or "").upper()
    if not src or not tgt or src == tgt:
        # Marker still set so callers can rely on _migrated_quote
        # being authoritative going forward.
        snapshot["_migrated_quote"] = tgt or src
        return

    # Top-level pairs list (e.g. ["SOL/USDC", "SOL/BTC", "BTC/USDC"]).
    pairs = snapshot.get("pairs")
    if isinstance(pairs, list):
        snapshot["pairs"] = [migrate_pair_key(p, src, tgt) for p in pairs]

    # Pair-keyed dicts.
    for field in ("engines", "coordinator_regime_history", "derivatives_history"):
        val = snapshot.get(field)
        if isinstance(val, dict):
            snapshot[field] = _remap_keys(val, src, tgt)

    # NOTE: order_journal entries are deliberately NOT migrated.
    # See module docstring for rationale.

    snapshot["_migrated_quote"] = tgt


def snapshot_already_migrated_to(
    snapshot: Dict[str, Any],
    target_quote: str,
) -> bool:
    """Detect whether a snapshot has already been migrated to a quote.

    Used at agent boot to skip redundant migration when --resume reads
    a snapshot that already reflects the active quote.
    """
    marker = snapshot.get("_migrated_quote")
    if isinstance(marker, str) and marker.upper() == (target_quote or "").upper():
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# File-level migration
# ═══════════════════════════════════════════════════════════════════

def migrate_snapshot_file(
    path: _PathLike,
    source_quote: str,
    target_quote: str,
) -> bool:
    """Migrate a snapshot file in place. Returns True iff state changed.

    Atomic write (.tmp + os.replace) so a crash mid-write doesn't
    leave a half-rewritten snapshot. Fail-soft on missing path or
    corrupt JSON — returns False without touching disk.
    """
    p = Path(path)
    if not p.exists():
        return False
    try:
        snapshot = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(snapshot, dict):
        return False

    if snapshot_already_migrated_to(snapshot, target_quote):
        return False

    # Capture pre-state to decide whether anything actually changed.
    before = json.dumps(snapshot, sort_keys=True)
    migrate_snapshot(snapshot, source_quote=source_quote, target_quote=target_quote)
    after = json.dumps(snapshot, sort_keys=True)
    if before == after:
        return False

    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(snapshot, default=str))
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    return True
