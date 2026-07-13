"""Daily trend-ensemble overlay: score math, rails gating, conviction
sizing, vol targeting, persistence, and the fail-open contract.

Evidence gates (real 1h tape, fees-on realistic fills, 1y/2y/3y):
overlay ON beat OFF in all six windows (.hydra-flywheel/trend_overlay_gate.json);
conviction sizing ON beat OFF in all three (.hydra-flywheel/conviction_sizing_gate.json);
the daily-entry path FAILED its gate (whipsaw against 1h flattens) and was
removed (.hydra-flywheel/trend_entry_gate.json).
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import (
    HydraEngine, Regime, Signal, SignalAction, Strategy, SIZING_COMPETITION,
)

DAY = 86400


def _daily(closes, start_day=20000):
    return [{"timestamp": (start_day + i) * DAY, "close": c}
            for i, c in enumerate(closes)]


def _rising(n, start=100.0, step=0.3):
    return [start + i * step for i in range(n)]


def _falling(n, start=300.0, step=0.3):
    return [start - i * step for i in range(n)]


def _engine(**kw) -> HydraEngine:
    return HydraEngine(initial_balance=10000.0, asset="SOL/USD", **kw)


# ─── warmup / fail-open ────────────────────────────────────────

def test_score_none_below_warmup():
    eng = _engine()
    eng.seed_daily_closes(_daily(_rising(100)))
    assert eng.daily_trend_score() is None
    assert eng.daily_trend_long() is None


def test_fail_open_rails_unchanged_when_unavailable():
    """No daily history → rails behave exactly as pre-overlay."""
    eng = _engine(hold_through=True)
    sig = Signal(SignalAction.BUY, 0.9, "MOM", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.BUY  # not blocked by a None overlay


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("HYDRA_TREND_OVERLAY", "0")
    eng = _engine()
    eng.seed_daily_closes(_daily(_rising(300)))
    assert eng.daily_trend_long() is None  # disabled reports unavailable


# ─── ensemble math ─────────────────────────────────────────────

def test_rising_tape_scores_long():
    eng = _engine()
    eng.seed_daily_closes(_daily(_rising(300)))
    score = eng.daily_trend_score()
    assert score is not None and score >= eng.TREND_SCORE_LONG
    assert eng.daily_trend_long() is True
    assert eng._don_state == 1  # steady breakout regime


def test_falling_tape_scores_flat():
    eng = _engine()
    eng.seed_daily_closes(_daily(_falling(300)))
    assert eng.daily_trend_long() is False
    assert eng._don_state == 0


def test_donchian_exit_on_breakdown():
    """Long regime entered on a 55d breakout must exit on a 20d breakdown."""
    closes = _rising(280)
    closes += [closes[-1] - 2.0 * (i + 1) for i in range(25)]  # sharp break
    eng = _engine()
    eng.seed_daily_closes(_daily(closes))
    assert eng._don_state == 0


def test_seed_idempotent():
    eng = _engine()
    data = _daily(_rising(300))
    eng.seed_daily_closes(data)
    first = (list(eng._daily_closes), eng._don_state, eng.daily_trend_score())
    eng.seed_daily_closes(data)
    assert (list(eng._daily_closes), eng._don_state,
            eng.daily_trend_score()) == first


# ─── rails gating ──────────────────────────────────────────────

def test_buy_gated_when_daily_flat():
    eng = _engine(hold_through=True)
    eng.seed_daily_closes(_daily(_falling(300)))
    sig = Signal(SignalAction.BUY, 0.9, "MOM", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "daily_trend_flat" in out.reason


def test_flatten_on_daily_flip():
    eng = _engine(hold_through=True)
    eng.seed_daily_closes(_daily(_falling(300)))
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "idle", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.RANGING, sig)
    assert out.action == SignalAction.SELL
    assert "daily_trend_exit" in out.reason


def test_buy_allowed_when_daily_long():
    eng = _engine(hold_through=True)
    eng.seed_daily_closes(_daily(_rising(300)))
    sig = Signal(SignalAction.BUY, 0.9, "MOM", Strategy.MOMENTUM)
    out = eng._apply_hold_through(Regime.TREND_UP, sig)
    assert out.action == SignalAction.BUY


# ─── vol targeting + conviction sizing ─────────────────────────

def test_vol_multiplier_derisk_and_cap():
    eng = _engine()
    # Violent tape → annualized vol far above the 30% target → mult < 1
    wild = []
    px = 100.0
    for i in range(320):
        px *= 1.08 if i % 2 else 0.93
        wild.append(px)
    eng.seed_daily_closes(_daily(wild))
    assert 0.2 <= eng._trend_vol_multiplier() < 1.0
    # Calm tape → mult capped at 1.0 (spot cannot lever)
    calm = [100.0 + 0.01 * i for i in range(320)]
    eng2 = _engine()
    eng2.seed_daily_closes(_daily(calm))
    assert eng2._trend_vol_multiplier() == 1.0
    # Unavailable → 1.0 (fail open)
    assert _engine()._trend_vol_multiplier() == 1.0


def _uptrend_intraday(eng, n=60, px=100.0):
    for i in range(n):
        px *= 1.004
        eng.ingest_candle({
            "open": px * 0.999, "high": px * 1.002, "low": px * 0.997,
            "close": px, "volume": 100.0,
            "timestamp": 1700000000 + i * 3600,
        })


def test_conviction_sizing_floors_kelly(monkeypatch):
    """Overlay-long entries allocate the vol-targeted fraction of the
    position cap instead of the Kelly crumb (evidence: 3.3x 3y return at
    <2% maxDD in the conviction gate)."""
    monkeypatch.delenv("HYDRA_TREND_CONVICTION_SIZING", raising=False)
    monkeypatch.setenv("HYDRA_FRICTION_GATE_DISABLED", "1")
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/USD",
                      sizing=dict(SIZING_COMPETITION), hold_through=False)
    # Calm rising dailies → overlay long, vol mult 1.0
    eng.seed_daily_closes(_daily([100.0 + 0.05 * i for i in range(320)]))
    assert eng.daily_trend_long() is True
    _uptrend_intraday(eng)
    trade = eng.execute_signal("BUY", 0.66, "entry", "MOMENTUM")
    assert trade is not None
    # Kelly at conf 0.66 would be ~5.5% of balance; conviction floors at
    # max_position_pct (40%) × vol_mult (1.0).
    assert trade.value >= 0.9 * 10000.0 * 0.40


def test_conviction_kill_switch(monkeypatch):
    monkeypatch.setenv("HYDRA_TREND_CONVICTION_SIZING", "0")
    monkeypatch.setenv("HYDRA_FRICTION_GATE_DISABLED", "1")
    eng = HydraEngine(initial_balance=10000.0, asset="SOL/USD",
                      sizing=dict(SIZING_COMPETITION), hold_through=False)
    eng.seed_daily_closes(_daily([100.0 + 0.05 * i for i in range(320)]))
    _uptrend_intraday(eng)
    trade = eng.execute_signal("BUY", 0.66, "entry", "MOMENTUM")
    assert trade is not None
    assert trade.value < 10000.0 * 0.10  # Kelly crumb, no conviction floor


# ─── persistence ───────────────────────────────────────────────

def test_snapshot_roundtrip_preserves_overlay_state():
    eng = _engine()
    eng.seed_daily_closes(_daily(_rising(300)))
    snap = eng.snapshot_runtime()
    fresh = _engine()
    fresh.restore_runtime(snap)
    assert fresh._daily_closes == eng._daily_closes
    assert fresh._don_state == eng._don_state
    assert fresh.daily_trend_score() == eng.daily_trend_score()


def test_ingest_extends_daily_series():
    eng = _engine()
    eng.seed_daily_closes(_daily(_rising(300)))
    last_day = eng._daily_closes[-1][0]
    # Intraday candles on a NEW day append; same-day updates refresh in place
    ts0 = (last_day + 1) * DAY + 3600
    eng.ingest_candle({"open": 1, "high": 1, "low": 1, "close": 500.0,
                       "volume": 1, "timestamp": ts0})
    assert eng._daily_closes[-1] == (last_day + 1, 500.0)
    eng.ingest_candle({"open": 1, "high": 1, "low": 1, "close": 501.0,
                       "volume": 1, "timestamp": ts0 + 3600})
    assert eng._daily_closes[-1] == (last_day + 1, 501.0)
    assert len([d for d, _ in eng._daily_closes if d == last_day + 1]) == 1
