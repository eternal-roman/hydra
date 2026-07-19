"""Artifact loading, frozen scoring, staleness."""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3bounce.features import FEATURES  # noqa: E402
from s3bounce.model import (  # noqa: E402
    Artifact, ArtifactError, gate, load_artifact, score)


def test_default_artifact_shape():
    a = load_artifact()
    assert set(a.models) == {"BTC/USD", "ETH/USD"}        # ZEC structurally absent
    assert a.breadth_universe == ("BTC/USD", "ETH/USD", "ZEC/USD")
    assert all(m.exit_policy == "x1_close_stop" for m in a.models.values())
    assert "hold_k60_stop" in a.models["ETH/USD"].shadow_arms
    assert "hold_k60_stop" not in a.models["BTC/USD"].shadow_arms


def test_score_matches_hand_sigmoid():
    a = load_artifact()
    m = a.models["BTC/USD"]
    x = dict(m.means)                                      # z = intercept
    expected = 1.0 / (1.0 + math.exp(-m.intercept))
    assert abs(score(m, x) - expected) < 1e-12
    x2 = {f: m.means[f] + m.stds[f] for f in FEATURES}     # z = b + sum(w)
    z = m.intercept + sum(m.weights.values())
    assert abs(score(m, x2) - 1.0 / (1.0 + math.exp(-z))) < 1e-12
    assert gate(m, x2) == (score(m, x2) >= m.threshold)


def test_malformed_artifact_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ArtifactError):
        load_artifact(str(p))
    p.write_text(json.dumps({"models": {"X": {"intercept": 0}},
                             "trained_through": "2026-01-01",
                             "breadth_universe": []}))
    with pytest.raises(ArtifactError):
        load_artifact(str(p))


def test_staleness_400_days():
    a = load_artifact()
    trained = dt.datetime.fromisoformat(a.trained_through) \
        .replace(tzinfo=dt.UTC).timestamp()
    assert not a.stale(trained + 399 * 86400)
    assert a.stale(trained + 401 * 86400)
