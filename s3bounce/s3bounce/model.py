"""Frozen logistic scorer + model artifact loading.

The artifact is the single source of truth for what the algorithm may
trade: an asset absent from `models` is structurally untradable (ZEC is
absent by design — classifier gate FAIL). Weights are the 2026
walk-forward fold of the promoted bakeoff; `threshold` is that fold's
train-p75 gate. Retraining is an operator action via
heartbeat/tools/export_s3_model.py — this package never fits anything.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .features import FEATURES

STALE_AFTER_DAYS = 400
_DEFAULT_PATH = Path(__file__).with_name("model_artifact.json")


class ArtifactError(ValueError):
    """Malformed or incomplete model artifact."""


@dataclass(frozen=True)
class AssetModel:
    asset: str
    intercept: float
    weights: dict[str, float]
    means: dict[str, float]
    stds: dict[str, float]
    threshold: float
    exit_policy: str
    shadow_arms: tuple[str, ...]


@dataclass(frozen=True)
class Artifact:
    models: dict[str, AssetModel]
    trained_through: str          # ISO date
    breadth_universe: tuple[str, ...]
    basis: dict
    raw: dict

    def stale(self, now_ts: float) -> bool:
        trained = _dt.datetime.fromisoformat(self.trained_through) \
            .replace(tzinfo=_dt.timezone.utc).timestamp()
        return now_ts - trained > STALE_AFTER_DAYS * 86400


def load_artifact(path: Optional[str] = None) -> Artifact:
    p = Path(path) if path else _DEFAULT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ArtifactError(f"cannot read model artifact {p}: {e}") from e
    try:
        models = {}
        for asset, m in raw["models"].items():
            for block in ("weights_std_space", "feature_means", "feature_stds"):
                missing = set(FEATURES) - set(m[block])
                if missing:
                    raise ArtifactError(
                        f"{asset} {block} missing features {sorted(missing)}")
            if any(m["feature_stds"][f] <= 0 for f in FEATURES):
                raise ArtifactError(f"{asset} has non-positive feature std")
            models[asset] = AssetModel(
                asset=asset, intercept=float(m["intercept"]),
                weights={f: float(m["weights_std_space"][f]) for f in FEATURES},
                means={f: float(m["feature_means"][f]) for f in FEATURES},
                stds={f: float(m["feature_stds"][f]) for f in FEATURES},
                threshold=float(m["threshold"]),
                exit_policy=str(m["exit_policy"]),
                shadow_arms=tuple(m.get("shadow_arms", ())))
        return Artifact(models=models,
                        trained_through=str(raw["trained_through"]),
                        breadth_universe=tuple(raw["breadth_universe"]),
                        basis=dict(raw.get("basis", {})), raw=raw)
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, ArtifactError):
            raise
        raise ArtifactError(f"malformed model artifact {p}: {e}") from e


def score(model: AssetModel, x: dict[str, float]) -> float:
    """Sigmoid of the standardized linear score (overflow-clamped)."""
    z = model.intercept
    for f in FEATURES:
        z += model.weights[f] * (x[f] - model.means[f]) / model.stds[f]
    z = max(-60.0, min(60.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def gate(model: AssetModel, x: dict[str, float]) -> bool:
    return score(model, x) >= model.threshold
