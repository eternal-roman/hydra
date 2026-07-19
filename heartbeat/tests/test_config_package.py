"""Config packaging: default.yaml resolves for installed-package layout."""

from __future__ import annotations

from pathlib import Path

from heartbeat import config as hb_config
from heartbeat.config import default_config_path, load_config


def test_packaged_default_exists_under_package():
    packaged = Path(hb_config.__file__).resolve().parent / "resources" / "default.yaml"
    assert packaged.is_file(), (
        "src/heartbeat/resources/default.yaml must ship with the package "
        "so installed wheels can load_config() without monorepo config/"
    )


def test_default_config_path_prefers_package_resources():
    path = default_config_path()
    assert path.is_file()
    # When resources copy is present, it must win over monorepo path
    packaged = Path(hb_config.__file__).resolve().parent / "resources" / "default.yaml"
    if packaged.is_file():
        assert path.resolve() == packaged.resolve()


def test_load_config_returns_expected_keys():
    cfg = load_config()
    for key in ("pair", "timeframe", "decay", "heartbeat", "features",
                "scaling", "store", "feed", "api"):
        assert key in cfg
