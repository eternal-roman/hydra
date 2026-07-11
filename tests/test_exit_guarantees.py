"""PR-A exit path hard guarantees (remediation plan G1).

A1: halted engine allows risk-reducing SELL, blocks BUY
A2: SELL does not require min_confidence (entries still do)
A3: DEFENSIVE SELL at RSI~41 is executable when position open
A4: R2 never force_holds spot SELL; may block BUY into capitulation
A5: QFE fee-aware 1.0% floor; LLM crowded_short alone does not veto
"""
from __future__ import annotations

import pytest

from hydra_engine import (
    HydraEngine,
    Indicators,
    Signal,
    SignalAction,
    SignalGenerator,
    Strategy,
)
from hydra_quant_rules import (
    FUNDING_EXTREME_BPS,
    QFE_MIN_PROFIT_PCT,
    apply_rules,
    evaluate_qfe,
)


FRESH = {
    "funding_bps_8h": 10.0,
    "oi_delta_1h_pct": 0.5,
    "basis_apr_pct": 8.0,
    "oi_price_regime": "balanced",
    "staleness_s": 5.0,
    "cvd_divergence_sigma": 0.1,
}


def _seeded(balance: float = 100.0, asset: str = "SOL/USD") -> HydraEngine:
    eng = HydraEngine(initial_balance=balance, asset=asset)
    for i in range(60):
        px = 100.0 + (i % 5) * 0.1
        eng.ingest_candle({
            "open": px, "high": px + 0.5, "low": px - 0.5,
            "close": px, "volume": 100.0,
            "timestamp": float(1_700_000_000 + i * 300),
        })
    return eng


# ─── A1: halt allows SELL, blocks BUY ─────────────────────────


class TestHaltedAllowsSellBlocksBuy:
    def test_halted_blocks_buy(self):
        eng = _seeded()
        eng.halted = True
        eng.halt_reason = "CIRCUIT BREAKER: test"
        t = eng.execute_signal("BUY", 0.90, "probe", "MOMENTUM")
        assert t is None
        assert eng.position.size == 0.0

    def test_halted_allows_sell_with_position(self):
        eng = _seeded()
        eng.position.size = 0.5
        eng.position.avg_entry = 100.0
        eng.halted = True
        eng.halt_reason = "CIRCUIT BREAKER: test"
        t = eng.execute_signal("SELL", 0.55, "flatten", "DEFENSIVE")
        assert t is not None, "halted engine must still allow risk-reducing SELL"
        assert t.action == "SELL"
        assert eng.position.size == 0.0
        assert eng.balance > 100.0  # received proceeds

    def test_halted_sell_without_position_is_noop(self):
        eng = _seeded()
        eng.halted = True
        t = eng.execute_signal("SELL", 0.90, "no pos", "DEFENSIVE")
        assert t is None


# ─── A2: SELL ignores min_confidence ──────────────────────────


class TestSellIgnoresMinConfidence:
    def test_soft_sell_executes_below_min_confidence(self):
        eng = _seeded()
        eng.position.size = 0.5
        eng.position.avg_entry = 100.0
        assert eng.sizer.min_confidence == 0.65
        t = eng.execute_signal("SELL", 0.51, "defensive soft", "DEFENSIVE")
        assert t is not None
        assert eng.position.size == 0.0

    def test_buy_still_requires_min_confidence(self):
        eng = _seeded()
        t = eng.execute_signal("BUY", 0.60, "weak", "MOMENTUM")
        assert t is None
        assert eng.position.size == 0.0

    def test_buy_at_min_confidence_still_works(self):
        eng = _seeded()
        t = eng.execute_signal("BUY", 0.65, "threshold", "MOMENTUM")
        assert t is not None
        assert eng.position.size > 0


# ─── A3: DEFENSIVE conf curve executable near RSI 40 ──────────


class TestDefensiveSellCurve:
    def test_defensive_sell_conf_at_rsi_41_at_least_min_or_executable(self):
        """Either conf ≥ 0.65 at RSI 41, or engine executes soft SELL (A2)."""
        # Synthetic indicators path: call _defensive directly
        indicators = {"rsi": 41.0, "price": 100.0}
        ctx = type("C", (), {"volume_ratio": 1.0})()
        sig = SignalGenerator._defensive(41.0, 100.0, indicators, ctx)
        assert sig.action == SignalAction.SELL
        # With A2, conf may stay soft; execution must still work
        eng = _seeded()
        eng.position.size = 0.5
        eng.position.avg_entry = 100.0
        t = eng.execute_signal(
            "SELL", sig.confidence, sig.reason, "DEFENSIVE",
        )
        assert t is not None, (
            f"DEFENSIVE SELL at RSI 41 conf={sig.confidence} must execute"
        )


# ─── A4: R2 does not block spot SELL ──────────────────────────


class TestR2SpotLongExit:
    def test_r2_does_not_force_hold_sell_on_extreme_neg_funding(self):
        qi = dict(FRESH, funding_bps_8h=-(FUNDING_EXTREME_BPS + 10))
        r = apply_rules("SELL", {"positioning_bias": "crowded_short"}, qi)
        assert r.force_hold is False, (
            f"R2 must not trap spot long exits: {r.force_hold_reason}"
        )
        assert "R2" not in [f.rule_id for f in r.triggered if f.effect == "force_hold"]

    def test_r2_blocks_buy_into_capitulation(self):
        """Repurposed R2: extreme negative funding + BUY = bounce-chase guard."""
        qi = dict(FRESH, funding_bps_8h=-(FUNDING_EXTREME_BPS + 10))
        r = apply_rules("BUY", {}, qi)
        assert r.force_hold is True
        assert any(f.rule_id == "R2" for f in r.triggered)


# ─── A5: QFE floor + crowded_short alone ──────────────────────


class TestQfeFeeAwareFloor:
    def test_qfe_min_profit_is_at_least_one_pct(self):
        assert QFE_MIN_PROFIT_PCT >= 1.0

    def test_qfe_does_not_fire_at_0_6_pct(self):
        r = evaluate_qfe(
            position_size=1.0,
            unrealized_pnl_pct=0.6,
            quant_indicators=FRESH,
            positioning_bias="balanced",
        )
        assert r.force_exit is False

    def test_qfe_fires_at_1_2_pct(self):
        r = evaluate_qfe(
            position_size=1.0,
            unrealized_pnl_pct=1.2,
            quant_indicators=FRESH,
            positioning_bias="balanced",
        )
        assert r.force_exit is True

    def test_crowded_short_alone_does_not_veto_qfe(self):
        """LLM bias alone must not hard-block; need deterministic OI squeeze."""
        r = evaluate_qfe(
            position_size=1.0,
            unrealized_pnl_pct=5.0,
            quant_indicators=FRESH,  # oi_price_regime balanced
            positioning_bias="crowded_short",
        )
        assert r.force_exit is True

    def test_crowded_short_plus_squeeze_regime_still_blocks(self):
        qi = dict(FRESH, oi_price_regime="short_squeeze")
        r = evaluate_qfe(
            position_size=1.0,
            unrealized_pnl_pct=5.0,
            quant_indicators=qi,
            positioning_bias="crowded_short",
        )
        assert r.force_exit is False
