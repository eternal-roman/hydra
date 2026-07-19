"""Calibrated weight loader — uncalibrated p_up is near-coin (H3)."""

from pathlib import Path

from heartbeat.weights_io import (
    apply_weights_to_config,
    find_weights,
    load_weights_file,
    weights_filename,
)


def test_weights_filename():
    assert weights_filename("BTC/USD", "1h") == "weights_BTC_USD_1h.json"


def test_load_weights_nested_and_flat(tmp_path):
    p = tmp_path / "weights_BTC_USD_1h.json"
    p.write_text('{"pair":"BTC/USD","weights":{"clv":0.4,"ofi":0.3}}')
    w = load_weights_file(p)
    assert w["clv"] == 0.4 and w["ofi"] == 0.3
    p2 = tmp_path / "flat.json"
    p2.write_text('{"clv": 1.0, "ofi": 0.5}')
    assert load_weights_file(p2)["clv"] == 1.0


def test_find_weights_real_tape_committed():
    """Committed real-tape weights must resolve for BTC/ETH."""
    pkg = Path(__file__).resolve().parents[1]
    hit = find_weights("BTC/USD", "1h", store_root=pkg / "data",
                       package_root=pkg)
    assert hit is not None
    w, path = hit
    assert "clv" in w or "ofi_momentum" in w
    assert path.name == "weights_BTC_USD_1h.json"


def test_apply_weights_to_config():
    cfg = {"features": {"default_weight": 0.5, "weights": {}}}
    apply_weights_to_config(cfg, {"clv": 0.9})
    assert cfg["features"]["weights"]["clv"] == 0.9
