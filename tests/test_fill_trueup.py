"""PR-C execution truth: fill price true-up + dust write-off."""
from __future__ import annotations

import pytest

from hydra_engine import HydraEngine


def _seed(eng: HydraEngine, n: int = 40, px: float = 100.0) -> None:
    for i in range(n):
        p = px + i * 0.01
        eng.ingest_candle({
            "open": p, "high": p + 0.2, "low": p - 0.2,
            "close": p, "volume": 50.0,
            "timestamp": float(1_700_000_000 + i * 300),
        })


class TestFillTrueUp:
    def test_true_up_buy_sets_avg_entry_to_fill_price(self, monkeypatch):
        monkeypatch.setenv("HYDRA_FRICTION_GATE_DISABLED", "1")
        eng = HydraEngine(initial_balance=1000.0, asset="SOL/USD")
        _seed(eng, px=100.0)
        snap = eng.snapshot_position()
        eng.execute_signal("BUY", 0.90, "opt", "MOMENTUM")
        filled_amt = eng.position.size
        assert filled_amt > 0
        assert eng.true_up_fill(
            side="BUY", amount=filled_amt, fill_price=99.50,
            pre_trade_snapshot=snap, reason="test",
        )
        assert eng.position.avg_entry == pytest.approx(99.50, rel=1e-9)
        assert eng.position.size == pytest.approx(filled_amt, rel=1e-9)

    def test_true_up_requires_snapshot(self):
        eng = HydraEngine(initial_balance=1000.0, asset="SOL/USD")
        _seed(eng)
        assert eng.true_up_fill("BUY", 1.0, 100.0, pre_trade_snapshot=None) is False


class TestDustWriteOff:
    def test_write_off_below_ordermin(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
        eng.position.size = 0.01  # SOL ordermin 0.02
        eng.position.avg_entry = 100.0
        written = eng.write_off_dust()
        assert written == pytest.approx(0.01)
        assert eng.position.size == 0.0
        assert eng.position.avg_entry == 0.0

    def test_sell_below_ordermin_writes_off(self):
        eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
        _seed(eng)
        eng.position.size = 0.015
        eng.position.avg_entry = 100.0
        t = eng.execute_signal("SELL", 0.80, "dust", "DEFENSIVE")
        assert t is None
        assert eng.position.size == 0.0
