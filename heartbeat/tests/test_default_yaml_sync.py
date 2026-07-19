"""Guard: monorepo config/default.yaml must match packaged resources copy.

Drift between the two paths breaks install-layout vs checkout-layout
behavior. Both must be bit-identical text.
"""

from __future__ import annotations

from pathlib import Path

_HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
_MONOREPO_DEFAULT = _HEARTBEAT_ROOT / "config" / "default.yaml"
_PACKAGED_DEFAULT = (
    _HEARTBEAT_ROOT / "src" / "heartbeat" / "resources" / "default.yaml"
)


def test_default_yaml_files_exist():
    assert _MONOREPO_DEFAULT.is_file(), f"missing {_MONOREPO_DEFAULT}"
    assert _PACKAGED_DEFAULT.is_file(), f"missing {_PACKAGED_DEFAULT}"


def test_default_yaml_monorepo_matches_packaged_resources():
    """config/default.yaml and src/heartbeat/resources/default.yaml must match."""
    mono = _MONOREPO_DEFAULT.read_text(encoding="utf-8")
    pkg = _PACKAGED_DEFAULT.read_text(encoding="utf-8")
    assert mono == pkg, (
        "default.yaml drift: monorepo config/default.yaml != "
        "src/heartbeat/resources/default.yaml — copy one onto the other "
        "so install and checkout layouts stay in sync"
    )
