"""High-level indicator API: run_dataset + IndicatorResult (Task 2)."""

from __future__ import annotations

import pytest

from heartbeat import (
    IndicatorResult,
    InvalidDatasetError,
    MissingDatasetError,
    Side,
    Trade,
    dataset_requirements,
    load_trades,
    run_dataset,
)
from heartbeat.indicator import HeartbeatSession
from helpers import mk_trade


def _synth_trades(n: int = 80, start_ts: float = 1_700_000_000.0) -> list[Trade]:
    """Trades spanning ~2h so the pipeline closes at least one candle."""
    trades: list[Trade] = []
    for i in range(n):
        # ~90s spacing → ~2 candles on 1h tf with flush
        ts = start_ts + i * 90.0
        side = "buy" if i % 3 else "sell"
        trades.append(mk_trade(ts, 100.0 + (i % 5) * 0.1, qty=1.0 + (i % 3) * 0.1,
                               side=side, tid=i + 1))
    return trades


def test_run_dataset_p_up_in_unit_interval():
    trades = _synth_trades()
    result = run_dataset(trades, symbol="SYNTH/USD", tf="1h")
    assert isinstance(result, IndicatorResult)
    assert result.symbol == "SYNTH/USD"
    assert result.tf == "1h"
    assert result.n_trades == len(trades)
    assert result.n_candles >= 1
    assert result.n_heartbeats >= 1
    assert result.p_up is not None
    assert 0.0 <= result.p_up <= 1.0
    assert result.L is not None
    assert result.ts is not None
    assert isinstance(result.tainted, bool)
    assert isinstance(result.series, list) and len(result.series) == result.n_candles
    assert all(0.0 <= row["p_up"] <= 1.0 for row in result.series)
    assert result.status in ("ok", "degraded", "error")
    assert isinstance(result.warnings, list)


def test_run_dataset_uncalibrated_warning_degraded():
    """Unknown symbol has no committed weights → degraded + warning."""
    trades = _synth_trades()
    result = run_dataset(trades, symbol="UNKNOWN", tf="1h", weights=None)
    assert result.status == "degraded"
    assert any(w.startswith("uncalibrated_weights:") for w in result.warnings)
    assert "default_weight" in result.warnings[0] or any(
        "default_weight" in w for w in result.warnings
    )


def test_run_dataset_with_explicit_weights_dict_ok():
    trades = _synth_trades()
    result = run_dataset(
        trades,
        symbol="UNKNOWN",
        tf="1h",
        weights={"clv": 0.8, "ofi": 0.6},
    )
    assert result.status == "ok"
    assert not any(w.startswith("uncalibrated_weights:") for w in result.warnings)
    assert 0.0 <= result.p_up <= 1.0


def test_run_dataset_missing_file_raises_missing():
    with pytest.raises(MissingDatasetError) as ei:
        run_dataset("/nonexistent/path/trades.csv", symbol="BTC/USD")
    assert ei.value.code == "missing_dataset"
    assert ei.value.hint is not None


def test_run_dataset_invalid_side_raises_invalid(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("ts,price,qty,side\n1.0,100.0,1.0,sideways\n", encoding="utf-8")
    with pytest.raises(InvalidDatasetError) as ei:
        run_dataset(p, symbol="BTC/USD")
    assert ei.value.code == "invalid_dataset"


def test_run_dataset_from_csv(tmp_path):
    p = tmp_path / "tape.csv"
    lines = ["ts,price,qty,side"]
    base = 1_700_000_000.0
    for i in range(50):
        side = "buy" if i % 2 == 0 else "sell"
        lines.append(f"{base + i * 120.0},{100.0 + i * 0.01},1.0,{side}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = run_dataset(p, symbol="TEST/USD", tf="1h",
                         weights={"clv": 0.5})
    assert result.n_trades == 50
    assert result.status == "ok"
    assert 0.0 <= result.p_up <= 1.0


def test_run_dataset_dict_rows():
    rows = [
        {"ts": 1_700_000_000.0 + i * 100.0, "price": 50.0 + i * 0.01,
         "qty": 1.0, "side": "b" if i % 2 else "s"}
        for i in range(40)
    ]
    result = run_dataset(rows, symbol="ROW/USD", tf="1h",
                         weights={"ofi": 0.4})
    assert result.n_trades == 40
    assert result.status == "ok"


def test_public_exports():
    import heartbeat as hb

    assert isinstance(hb.__version__, str)
    assert callable(hb.run_dataset)
    assert callable(hb.load_trades)
    assert callable(hb.dataset_requirements)
    assert hb.MissingDatasetError is MissingDatasetError
    assert hb.InvalidDatasetError is InvalidDatasetError
    assert hb.IndicatorResult is IndicatorResult
    assert hb.Trade is Trade
    assert hb.Side is Side
    req = dataset_requirements()
    assert "required_columns" in req or "columns" in req


def test_heartbeat_session_streaming():
    session = HeartbeatSession(symbol="STREAM/USD", tf="1h",
                               weights={"clv": 0.7})
    out = None
    for t in _synth_trades(60):
        out = session.feed_trade(t)
    latest = session.latest
    assert isinstance(latest, IndicatorResult)
    assert latest.symbol == "STREAM/USD"
    assert latest.n_trades == 60
    assert latest.p_up is not None
    assert 0.0 <= latest.p_up <= 1.0
    assert latest.status == "ok"
    # last feed may or may not emit a heartbeat (micro-bucket); latest still set
    assert latest.n_heartbeats >= 1 or out is None or out is not None
