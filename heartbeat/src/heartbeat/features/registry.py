"""Feature registration: name, tier, inputs, lookback, hypothesis, fn.

Every feature is a pure function of a `FeatureContext` (forming candle +
closed-candle history + frozen ATR), returns Optional[float] — the RAW
feature value. `None` means "insufficient data"; the posterior treats it
as zero evidence (log-odds neutral), never as an error.

Config controls which features feed the posterior:
  features.enabled_tiers: [0]         -> all tier-0 features
  features.overrides: {ofi: false}    -> per-feature veto/force
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..engine.candle import ClosedCandle, FormingCandle


@dataclass(frozen=True)
class FeatureContext:
    """Everything a feature may see at a heartbeat. All of it is derived
    from trades with ts <= now — nothing else is reachable from here."""

    forming: FormingCandle
    closed: Sequence[ClosedCandle]   # oldest .. newest
    atr: Optional[float]             # robust ATR frozen at candle open
    config: dict


FeatureFn = Callable[[FeatureContext], Optional[float]]


@dataclass(frozen=True)
class Feature:
    name: str
    tier: int
    inputs: str          # human-readable input description
    lookback: int        # closed candles required (0 = forming only)
    hypothesis: str      # one-line falsifiable hypothesis
    fn: FeatureFn


_REGISTRY: dict[str, Feature] = {}


def register(name: str, tier: int, inputs: str, lookback: int,
             hypothesis: str) -> Callable[[FeatureFn], FeatureFn]:
    def deco(fn: FeatureFn) -> FeatureFn:
        if name in _REGISTRY:
            raise ValueError(f"duplicate feature {name!r}")
        _REGISTRY[name] = Feature(name, tier, inputs, lookback, hypothesis, fn)
        return fn
    return deco


def all_features() -> dict[str, Feature]:
    _ensure_loaded()
    return dict(_REGISTRY)


def enabled_features(config: dict) -> list[Feature]:
    """Deterministic (name-sorted) list of features the posterior uses."""
    _ensure_loaded()
    fcfg = config.get("features", {})
    tiers = set(fcfg.get("enabled_tiers", [0]))
    overrides: dict[str, bool] = fcfg.get("overrides") or {}
    out = []
    for name in sorted(_REGISTRY):
        f = _REGISTRY[name]
        on = f.tier in tiers
        if name in overrides:
            on = bool(overrides[name])
        if on:
            out.append(f)
    return out


def _ensure_loaded() -> None:
    # Import tier modules exactly once so their @register calls run.
    from . import tier0 as _t0  # noqa: F401
    from . import tier1 as _t1  # noqa: F401
    from . import tier2 as _t2  # noqa: F401
