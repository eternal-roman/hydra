"""Brain prompt advisory: heartbeat/S3 surfaces are advisory-only."""
from hydra_brain import HydraBrain


def test_format_includes_heartbeat_ok_and_no_force_language():
    state = {"quant_indicators": {
        "funding_bps_8h": 1.0,
        "oi_delta_1h_pct": 0.1,
        "oi_price_regime": "neutral",
        "basis_apr_pct": 5.0,
        "cvd_divergence_sigma": 0.0,
        "staleness_s": 1.0,
        "heartbeat": {
            "status": "ok", "p_up": 0.72, "L": 0.9,
            "candle_progress": 0.4,
        },
        "s3": {
            "active": True, "stage": "entryable_b1",
            "score": 0.8, "gated": True, "degraded": False,
        },
    }}
    block = HydraBrain._format_quant_indicators(state)
    assert "heartbeat: status=ok p_up=0.72" in block
    assert "ADVISORY only" in block
    assert "never force_hold from p_up alone" in block
    assert "s3: active" in block
    assert "NO order path" in block


def test_format_heartbeat_no_opinion_not_half():
    state = {"quant_indicators": {
        "funding_bps_8h": 1.0,
        "oi_delta_1h_pct": 0.0,
        "oi_price_regime": "neutral",
        "basis_apr_pct": 1.0,
        "cvd_divergence_sigma": 0.0,
        "staleness_s": 1.0,
        "heartbeat": {"status": "no_opinion", "why": "stale", "p_up": None},
    }}
    block = HydraBrain._format_quant_indicators(state)
    assert "no_opinion" in block
    assert "do not invent p_up=0.5" in block
    assert "p_up=0.5" not in block.replace("do not invent p_up=0.5", "")


def test_format_flow_gate_fail_flag():
    state = {"quant_indicators": {
        "derivatives_covered": False,
        "cvd_divergence_sigma": 0.0,
        "heartbeat": {
            "status": "ok", "p_up": 0.9, "flow_gate_fail": True,
        },
    }}
    block = HydraBrain._format_quant_indicators(state)
    assert "flow_gate_fail=true" in block
