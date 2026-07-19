"""The heart: recursive Bayesian log-odds posterior over order flow.

Per heartbeat t:
    L_t = lambda_hb * L_{t-1} + (1/h) * sum_i w_i * z_{i,t}
    P(up) = sigmoid(L_t)

Memory is defined in CANDLE units: lambda_candle = 1 - 1/N (N=30 default).
Conversion to per-heartbeat decay uses h = heartbeats-per-candle
(rolling median of actual heartbeat counts of recent closed candles,
frozen at candle open):

    lambda_hb = lambda_candle ** (1/h)

The evidence term is scaled by 1/h for the same reason the decay is
converted: with constant evidence z over a candle, the accumulated
per-candle contribution is then ~ w*z regardless of trade rate, i.e. the
recursion sampled at candle closes matches the candle-level recursion
L <- lambda_candle * L + w*z. (test_posterior.py::test_candle_unit_memory
asserts this.) Without the 1/h factor, L would scale linearly with tick
count and "memory in candle units" would be a lie.

Empty candles (no trades -> no heartbeats) decay L by one full candle
unit at their close, so quiet tape forgets at the same rate as busy tape.

Determinism: everything here is a pure function of (config, tape prefix).
Scaler medians/MADs and h are FROZEN at candle open and only updated from
closed-candle values, so no intra-candle feedback loops exist.

Calibration hook: the engine tracks per-feature decayed evidence sums
    S_i(t) = lambda_hb * S_i(t-1) + z_i(t) / h
so that L(t) = sum_i w_i * S_i(t) EXACTLY. A logistic regression fit on
snapshot S vectors is therefore fitting the live posterior's weights in
its true functional form — no approximation gap between calibration and
production (test_posterior.py::test_L_equals_weighted_S asserts this).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Callable, Optional, Sequence

from ..features.registry import Feature, FeatureContext, enabled_features
from .candle import ClosedCandle, FormingCandle

MAD_CONSISTENCY = 1.4826  # MAD -> sigma for a normal distribution


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 700.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -700.0))
    return z / (1.0 + z)


class RobustScaler:
    """Rolling median/MAD scaler over trailing candle-CLOSE raw values.

    Parameters are frozen at candle open (`freeze`) and used for every
    heartbeat of the forming candle; new raw values are pushed only at
    candle close. z = clip((x - med) / (clip_mads * 1.4826 * MAD), -1, 1).
    Returns None (no evidence) until `min_history` samples are seen or
    when MAD degenerates to 0 with x == med.
    """

    def __init__(self, window: int = 500, clip_mads: float = 3.0,
                 min_history: int = 30) -> None:
        self.window = window
        self.clip_mads = clip_mads
        self.min_history = min_history
        self.values: deque[float] = deque(maxlen=window)
        self._med: Optional[float] = None
        self._mad: Optional[float] = None

    def push(self, x: float) -> None:
        self.values.append(x)

    def freeze(self) -> None:
        if len(self.values) < self.min_history:
            self._med = None
            self._mad = None
            return
        vals = list(self.values)
        self._med = median(vals)
        self._mad = median(abs(v - self._med) for v in vals)

    def scale(self, x: float) -> Optional[float]:
        if self._med is None:
            return None
        delta = x - self._med
        denom = self.clip_mads * MAD_CONSISTENCY * (self._mad or 0.0)
        if denom <= 0:
            if delta == 0:
                return 0.0
            return 1.0 if delta > 0 else -1.0
        return max(-1.0, min(1.0, delta / denom))

    def to_dict(self) -> dict:
        return {"window": self.window, "clip_mads": self.clip_mads,
                "min_history": self.min_history, "values": list(self.values)}

    @classmethod
    def from_dict(cls, d: dict) -> "RobustScaler":
        s = cls(d["window"], d["clip_mads"], d["min_history"])
        s.values.extend(d["values"])
        return s


@dataclass(frozen=True, slots=True)
class HeartbeatOutput:
    ts: float
    L: float
    p_up: float
    raw: dict[str, Optional[float]]
    z: dict[str, Optional[float]]
    tainted: bool


class PosteriorEngine:
    """Owns L, the scalers, decay conversion, and the evidence sum."""

    H_WINDOW = 20  # closed candles used to estimate heartbeats-per-candle

    def __init__(self, config: dict,
                 features: Optional[list[Feature]] = None,
                 lambda_modulator: Optional[Callable[[], float]] = None) -> None:
        self.config = config
        self.features = features if features is not None else enabled_features(config)
        if not self.features:
            raise ValueError("no features enabled — posterior would be constant")
        fcfg = config.get("features", {})
        default_w = float(fcfg.get("default_weight", 0.5))
        weights_cfg = fcfg.get("weights") or {}
        self.weights = {f.name: float(weights_cfg.get(f.name, default_w))
                        for f in self.features}
        scfg = config.get("scaling", {})
        self.scalers = {f.name: RobustScaler(
            int(scfg.get("window_candles", 500)),
            float(scfg.get("clip_mads", 3.0)),
            int(scfg.get("min_history", 30)),
        ) for f in self.features}
        n = int(config.get("decay", {}).get("memory_candles", 30))
        if n < 2:
            raise ValueError("decay.memory_candles must be >= 2")
        self.lambda_candle = 1.0 - 1.0 / n
        self.default_h = float(config.get("heartbeat", {})
                               .get("default_heartbeats_per_candle", 60))
        self.lambda_modulator = lambda_modulator

        self.L: float = 0.0
        self.S: dict[str, float] = {f.name: 0.0 for f in self.features}
        self._hb_counts: deque[int] = deque(maxlen=self.H_WINDOW)
        self._hb_in_candle: int = 0
        self._h: float = self.default_h
        self._lambda_hb: float = self.lambda_candle ** (1.0 / self.default_h)
        self._last_raw: dict[str, Optional[float]] = {}
        self._last_z: dict[str, Optional[float]] = {}

    # -- candle lifecycle ----------------------------------------------------

    def on_candle_open(self) -> None:
        """Freeze scalers, h, and per-heartbeat lambda for the new candle."""
        counts = [c for c in self._hb_counts if c > 0]
        self._h = float(median(counts)) if counts else self.default_h
        self._h = max(self._h, 1.0)
        lam = self.lambda_candle
        if self.lambda_modulator is not None:
            lam = min(0.999999, max(0.0, lam * self.lambda_modulator()))
        self._lambda_hb = lam ** (1.0 / self._h)
        for s in self.scalers.values():
            s.freeze()
        self._hb_in_candle = 0
        self._last_raw = {}
        self._last_z = {}

    def on_candle_close(self, candle: ClosedCandle) -> dict:
        """Push candle-close raw values into scalers; return snapshot row."""
        if candle.trade_count == 0:
            # no heartbeats happened: decay one full candle unit
            for name in self.S:
                self.S[name] *= self.lambda_candle
            self.L = sum(self.weights[n] * self.S[n] for n in self.S)
        for name, raw in self._last_raw.items():
            if raw is not None:
                self.scalers[name].push(raw)
        self._hb_counts.append(self._hb_in_candle)
        return {
            "L": self.L,
            "p_up": sigmoid(self.L),
            "features": {name: {"raw": self._last_raw.get(name),
                                "z": self._last_z.get(name),
                                "S": self.S[name]}
                         for name in self.weights},
        }

    # -- heartbeat -------------------------------------------------------------

    def heartbeat(self, ctx: FeatureContext, ts: float,
                  tainted: bool = False) -> HeartbeatOutput:
        raw: dict[str, Optional[float]] = {}
        z: dict[str, Optional[float]] = {}
        for f in self.features:
            r = f.fn(ctx)
            raw[f.name] = r
            zz = self.scalers[f.name].scale(r) if r is not None else None
            z[f.name] = zz
            self.S[f.name] = (self._lambda_hb * self.S[f.name]
                              + (zz or 0.0) / self._h)
        self.L = sum(self.weights[n] * self.S[n] for n in self.S)
        self._hb_in_candle += 1
        self._last_raw = raw
        self._last_z = z
        return HeartbeatOutput(ts=ts, L=self.L, p_up=sigmoid(self.L),
                               raw=raw, z=z, tainted=tainted)

    # -- warmup / persistence ----------------------------------------------------

    def warm_scalers_from_candles(self, candles: Sequence[ClosedCandle]) -> int:
        """Seed scalers from historical CLOSED candles (REST OHLC bootstrap
        or backfilled tape). Only candle-level features are computable from
        pure OHLCV history; flow features need per-candle flow fields, which
        candles built from trade backfill do have. Returns #values pushed."""
        pushed = 0
        cfg = self.config
        for i in range(1, len(candles)):
            closed = candles[:i]
            cur = candles[i]
            forming = FormingCandle(open_ts=cur.open_ts,
                                    tf_s=int(cur.close_ts - cur.open_ts))
            _replay_candle_into_forming(cur, forming)
            from ..features.tier0 import robust_atr
            atr = robust_atr(closed,
                             int(cfg.get("atr", {}).get("period", 14)),
                             float(cfg.get("atr", {}).get("outlier_mult", 3.0)))
            ctx = FeatureContext(forming=forming, closed=closed, atr=atr,
                                 config=cfg)
            for f in self.features:
                r = f.fn(ctx)
                if r is not None:
                    self.scalers[f.name].push(r)
                    pushed += 1
        return pushed

    def scaler_state(self) -> dict:
        return {name: s.to_dict() for name, s in self.scalers.items()}

    def load_scaler_state(self, state: dict) -> None:
        for name, d in state.items():
            if name in self.scalers:
                self.scalers[name] = RobustScaler.from_dict(d)


def _replay_candle_into_forming(c: ClosedCandle, f: FormingCandle) -> None:
    """Map a closed candle's aggregates onto a forming-candle view so
    candle-level feature functions can run on historical candles."""
    f.open = c.open
    f.high = c.high
    f.low = c.low
    f.close = c.close
    f.volume = c.volume
    f.buy_vol = c.buy_vol
    f.sell_vol = c.sell_vol
    f.trade_count = max(c.trade_count, 1)
    f.vwap_num = c.vwap * c.volume
    f.buy_size_sum = c.buy_size_sum
    f.buy_count = c.buy_count
    f.sell_size_sum = c.sell_size_sum
    f.sell_count = c.sell_count
    f.max_buy_streak = c.max_buy_streak
    f.max_sell_streak = c.max_sell_streak
    f.vol_bottom_third = c.vol_bottom_third
    f._last_ts = c.close_ts  # full candle: progress = 1
