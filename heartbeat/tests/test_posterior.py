"""Posterior recursion invariants (Phase 2 gate)."""

from heartbeat.engine.candle import ClosedCandle
from heartbeat.engine.posterior import PosteriorEngine, RobustScaler, sigmoid
from heartbeat.features.registry import Feature, FeatureContext
from helpers import base_config, mk_candle


def _const_feature(name: str, value: float) -> Feature:
    return Feature(name=name, tier=0, inputs="test", lookback=0,
                   hypothesis="test", fn=lambda ctx: value)


class _IdScaler(RobustScaler):
    """scale(x) == clip(x): isolates the recursion from scaler dynamics
    (the real scaler's rolling median would drift as pushed values change,
    which is correct behavior but noise for these invariant tests)."""

    def freeze(self):
        pass

    def scale(self, x):
        return max(-1.0, min(1.0, x))


def _identity_engine(cfg=None, value=1.0, weight=1.0):
    cfg = cfg or base_config()
    feat = _const_feature("konst", value)
    eng = PosteriorEngine(cfg, features=[feat])
    eng.weights["konst"] = weight
    eng.scalers["konst"] = _IdScaler()
    return eng, feat


def _ctx(cfg, forming_trades=1):
    from heartbeat.engine.candle import FormingCandle
    f = FormingCandle(open_ts=0.0, tf_s=3600)
    f.trade_count = forming_trades
    f.open = f.high = f.low = f.close = 100.0
    return FeatureContext(forming=f, closed=(), atr=None, config=cfg)


def test_sigmoid():
    assert sigmoid(0.0) == 0.5
    assert 0.999 < sigmoid(10) < 1.0
    assert sigmoid(-800) >= 0.0  # no overflow
    assert sigmoid(800) <= 1.0


def test_robust_scaler_hand():
    s = RobustScaler(window=10, clip_mads=3.0, min_history=5)
    for v in [1, 2, 3, 4, 5]:
        s.push(v)
    s.freeze()
    # median 3, MAD 1 -> z(3) = 0, z(3 + 3*1.4826) = 1 (clip boundary)
    assert s.scale(3.0) == 0.0
    assert abs(s.scale(3.0 + 3 * 1.4826) - 1.0) < 1e-12
    assert s.scale(100.0) == 1.0    # clipped
    assert s.scale(-100.0) == -1.0
    s2 = RobustScaler(min_history=5)
    s2.push(1.0)
    s2.freeze()
    assert s2.scale(1.0) is None    # warming


def test_scaler_serialization_roundtrip():
    s = RobustScaler(window=10, clip_mads=3.0, min_history=5)
    for v in [1, 2, 3, 4, 5, 6]:
        s.push(v)
    s2 = RobustScaler.from_dict(s.to_dict())
    s.freeze(); s2.freeze()
    assert s.scale(4.2) == s2.scale(4.2)


def test_candle_unit_memory():
    """Constant evidence z=1, w=1: sampled at candle closes the recursion
    must match the CANDLE-level recursion L <- lam*L + 1 regardless of the
    number of heartbeats per candle (this is what the 1/h evidence scaling
    plus lambda_hb = lam**(1/h) buys)."""
    cfg = base_config()
    n = cfg["decay"]["memory_candles"]
    lam = 1 - 1 / n
    for h in (1, 10, 60):
        eng, _ = _identity_engine(cfg, value=1.0, weight=1.0)
        eng.default_h = float(h)
        L_ref = 0.0
        candles_run = 200
        for candle_i in range(candles_run):
            eng.on_candle_open()
            for _ in range(h):
                out = eng.heartbeat(_ctx(cfg), ts=candle_i * 3600.0)
            eng.on_candle_close(mk_candle(open_ts=candle_i * 3600))
            L_ref = lam * L_ref + 1.0
            # geometric-vs-discrete decay differs slightly WITHIN a candle;
            # at closes the two agree to within a few percent.
            assert abs(eng.L - L_ref) / max(L_ref, 1e-9) < 0.05
        # after 200 candles the finite-horizon reference n*(1-lam^t) is
        # within 0.2% of the fixed point N = memory_candles
        assert abs(eng.L - n * (1 - lam ** candles_run)) / n < 0.05


def test_L_equals_weighted_S():
    cfg = base_config()
    eng, _ = _identity_engine(cfg, value=0.7, weight=0.42)
    eng.on_candle_open()
    for i in range(25):
        out = eng.heartbeat(_ctx(cfg), ts=float(i))
    assert abs(eng.L - 0.42 * eng.S["konst"]) < 1e-12
    assert out.p_up == sigmoid(eng.L)


def test_empty_candle_decays_one_unit():
    cfg = base_config()
    eng, _ = _identity_engine(cfg)
    eng.on_candle_open()
    eng.heartbeat(_ctx(cfg), ts=0.0)
    L0 = eng.L
    empty = ClosedCandle(3600, 7200, 100, 100, 100, 100, 0, 0, 0, 0, 100)
    eng.on_candle_close(empty)
    assert abs(eng.L - L0 * eng.lambda_candle) < 1e-12


def test_none_feature_contributes_zero():
    cfg = base_config()
    feat = Feature("maybe", 0, "t", 0, "t", fn=lambda ctx: None)
    eng = PosteriorEngine(cfg, features=[feat])
    eng.on_candle_open()
    out = eng.heartbeat(_ctx(cfg), ts=0.0)
    assert out.L == 0.0 and out.p_up == 0.5
    assert out.raw["maybe"] is None and out.z["maybe"] is None


def test_h_frozen_at_open_from_closed_counts():
    cfg = base_config()
    eng, _ = _identity_engine(cfg)
    for c in [10, 20, 30]:
        eng._hb_counts.append(c)
    eng.on_candle_open()
    assert eng._h == 20.0  # median
    assert abs(eng._lambda_hb - eng.lambda_candle ** (1 / 20.0)) < 1e-15
