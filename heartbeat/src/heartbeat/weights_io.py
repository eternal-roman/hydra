"""Load calibrated posterior weights for live `heartbeat run`.

Uncalibrated default weights → p_up ≈ coin flip (HONEST_FINDINGS / H3).
Search order is explicit and deterministic; first hit wins.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def weights_filename(pair: str, tf: str) -> str:
    return f"weights_{pair.replace('/', '_')}_{tf}.json"


def default_search_roots(store_root: str | Path,
                         package_root: Optional[Path] = None) -> List[Path]:
    """Roots checked for weights_{PAIR}_{tf}.json (first hit wins)."""
    store_root = Path(store_root)
    pkg = package_root or Path(__file__).resolve().parents[2]  # heartbeat/
    return [
        store_root / "reports",
        pkg / "data" / "reports",
        pkg / "evidence" / "real_tape",
        store_root,
        pkg / "data",
    ]


def load_weights_file(path: Path) -> Optional[Dict[str, float]]:
    """Parse a weights JSON; accept {weights:{...}} or flat {feature: w}."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    block = raw.get("weights") if isinstance(raw.get("weights"), dict) else raw
    out: Dict[str, float] = {}
    for k, v in block.items():
        if k in ("pair", "tf", "weights", "generated_at", "notes"):
            continue
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out or None


def find_weights(pair: str, tf: str,
                 store_root: str | Path = "data",
                 package_root: Optional[Path] = None,
                 extra_roots: Optional[List[Path]] = None,
                 ) -> Optional[tuple[Dict[str, float], Path]]:
    """Return (weights, path) or None if no calibrated file found."""
    name = weights_filename(pair, tf)
    roots = list(default_search_roots(store_root, package_root))
    if extra_roots:
        roots = list(extra_roots) + roots
    for root in roots:
        path = Path(root) / name
        if not path.is_file():
            continue
        w = load_weights_file(path)
        if w:
            return w, path
    return None


def apply_weights_to_config(cfg: dict, weights: Dict[str, float]) -> dict:
    """Mutate cfg['features']['weights'] and return cfg (same object)."""
    feats = cfg.setdefault("features", {})
    feats["weights"] = dict(weights)
    return cfg
