"""Config loading: default.yaml deep-merged with an optional user file,
then CLI overrides. The merged dict is what every component receives —
no component reads files or env on its own in the math path.

Default.yaml resolution (installed package + monorepo):
  1. ``src/heartbeat/resources/default.yaml`` (shipped in the wheel)
  2. monorepo ``heartbeat/config/default.yaml`` (editable checkout fallback)
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import yaml

_PKG_DIR = Path(__file__).resolve().parent
_PACKAGED_DEFAULT = _PKG_DIR / "resources" / "default.yaml"
# Layout: heartbeat/src/heartbeat/config.py → parents[2] == heartbeat/
_MONOREPO_DEFAULT = _PKG_DIR.parents[2] / "config" / "default.yaml"


def default_config_path() -> Path:
    """Resolve the shipped default.yaml for installed or monorepo layouts."""
    if _PACKAGED_DEFAULT.is_file():
        return _PACKAGED_DEFAULT
    if _MONOREPO_DEFAULT.is_file():
        return _MONOREPO_DEFAULT
    raise FileNotFoundError(
        "heartbeat default.yaml not found; expected package "
        f"resources/default.yaml ({_PACKAGED_DEFAULT}) or monorepo "
        f"config/default.yaml ({_MONOREPO_DEFAULT})"
    )


# Backward-compatible module attribute (resolved at import; re-evaluate via
# default_config_path() if the packaged file appears after import).
DEFAULT_PATH = (
    _PACKAGED_DEFAULT if _PACKAGED_DEFAULT.is_file() else _MONOREPO_DEFAULT
)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Optional[str] = None,
                overrides: Optional[dict[str, Any]] = None) -> dict:
    with open(default_config_path()) as f:
        cfg = yaml.safe_load(f)
    if path:
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        cfg = deep_merge(cfg, user)
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg
