"""Dataset IO: load_trades + dataset_requirements (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartbeat.dataset import dataset_requirements, load_trades
from heartbeat.errors import InvalidDatasetError, MissingDatasetError
from heartbeat.feed.tape import Side, Trade
from helpers import mk_trade


# ---------------------------------------------------------------------------
# missing / empty
# ---------------------------------------------------------------------------

def test_missing_file_raises_missing_dataset(tmp_path: Path):
    path = tmp_path / "nope.csv"
    with pytest.raises(MissingDatasetError) as ei:
        load_trades(path)
    assert ei.value.code == "missing_dataset"
    assert ei.value.hint
    assert "side" in ei.value.hint.lower() or "aggressor" in ei.value.hint.lower()


def test_empty_string_path_raises_missing_dataset():
    with pytest.raises(MissingDatasetError) as ei:
        load_trades("")
    assert ei.value.code == "missing_dataset"


def test_empty_csv_raises_invalid_dataset(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_text("ts,price,qty,side\n", encoding="utf-8")
    with pytest.raises(InvalidDatasetError) as ei:
        load_trades(path)
    assert ei.value.code == "invalid_dataset"


def test_empty_list_raises_invalid_dataset():
    with pytest.raises(InvalidDatasetError) as ei:
        load_trades([])
    assert ei.value.code == "invalid_dataset"


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------

def test_happy_csv_path(tmp_path: Path):
    path = tmp_path / "trades.csv"
    path.write_text(
        "ts,price,qty,side\n"
        "1000.0,50000.0,0.1,buy\n"
        "1001.0,50010.0,0.2,sell\n",
        encoding="utf-8",
    )
    trades = load_trades(path, symbol="BTC/USD")
    assert len(trades) == 2
    assert all(isinstance(t, Trade) for t in trades)
    assert trades[0].ts == 1000.0
    assert trades[0].price == 50000.0
    assert trades[0].qty == 0.1
    assert trades[0].side is Side.BUY
    assert trades[1].side is Side.SELL


def test_csv_column_aliases(tmp_path: Path):
    path = tmp_path / "aliases.csv"
    path.write_text(
        "timestamp,price,quantity,aggressor\n"
        "1000,100.5,1.5,b\n"
        "1001,99.5,2.0,s\n",
        encoding="utf-8",
    )
    trades = load_trades(path)
    assert trades[0].side is Side.BUY
    assert trades[1].side is Side.SELL
    assert trades[0].qty == 1.5


def test_csv_side_numeric_and_time_alias(tmp_path: Path):
    path = tmp_path / "numside.csv"
    path.write_text(
        "time,price,volume,side\n"
        "1000,10,1,1\n"
        "1001,11,2,-1\n",
        encoding="utf-8",
    )
    trades = load_trades(path)
    assert trades[0].side is Side.BUY
    assert trades[1].side is Side.SELL
    assert trades[1].qty == 2.0


def test_jsonl_path(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    rows = [
        {"ts": 1.0, "price": 10.0, "qty": 1.0, "side": "buy"},
        {"timestamp": 2.0, "price": 11.0, "size": 0.5, "aggressor": "SELL"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    trades = load_trades(path)
    assert len(trades) == 2
    assert trades[0].side is Side.BUY
    assert trades[1].side is Side.SELL
    assert trades[1].qty == 0.5


def test_json_list_path(tmp_path: Path):
    path = tmp_path / "trades.json"
    path.write_text(
        json.dumps([
            {"ts": 1.0, "price": 10.0, "qty": 1.0, "side": "buy", "trade_id": 7},
            {"ts": 2.0, "price": 11.0, "qty": 2.0, "side": "sell", "ord_type": "market"},
        ]),
        encoding="utf-8",
    )
    trades = load_trades(path)
    assert len(trades) == 2
    assert trades[0].trade_id == 7
    assert trades[1].ord_type == "market"


def test_iterable_of_dicts():
    rows = [
        {"ts": 1.0, "price": 10.0, "qty": 1.0, "side": "buy"},
        {"ts": 2.0, "price": 11.0, "qty": 2.0, "side": "sell"},
    ]
    trades = load_trades(rows)
    assert len(trades) == 2
    assert trades[0].price == 10.0


def test_existing_list_of_trades_passthrough():
    existing = [mk_trade(1.0, 100.0), mk_trade(2.0, 101.0, side="sell", tid=9)]
    out = load_trades(existing)
    assert out is existing or out == existing
    assert out[0].side is Side.BUY
    assert out[1].trade_id == 9


# ---------------------------------------------------------------------------
# invalid schema / side
# ---------------------------------------------------------------------------

def test_bad_side_raises_invalid_dataset():
    with pytest.raises(InvalidDatasetError) as ei:
        load_trades([{"ts": 1.0, "price": 10.0, "qty": 1.0, "side": "maybe"}])
    assert ei.value.code == "invalid_dataset"
    assert "side" in str(ei.value).lower() or (ei.value.hint and "side" in ei.value.hint.lower())


def test_missing_required_column_raises_invalid():
    with pytest.raises(InvalidDatasetError) as ei:
        load_trades([{"ts": 1.0, "price": 10.0, "side": "buy"}])  # no qty
    assert ei.value.code == "invalid_dataset"


def test_dataset_requirements_keys():
    req = dataset_requirements()
    assert isinstance(req, dict)
    # Agents/MCP need a stable description of formats + columns.
    assert "formats" in req or "accepted_formats" in req
    assert "columns" in req or "required_columns" in req
    blob = json.dumps(req).lower()
    assert "csv" in blob
    assert "side" in blob
    assert "price" in blob
    assert "ohlcv" in blob
    assert req.get("unsupported", {}).get("ohlcv_only") is True
    assert "not supported" in req["unsupported"]["reason"].lower()
    assert "yagni" in req["unsupported"]["hint"].lower() or "yagni" in req[
        "unsupported"
    ]["reason"].lower()


def test_ohlcv_only_raises_invalid_dataset():
    """OHLCV-only without aggressor side → InvalidDatasetError (no invent-side)."""
    with pytest.raises(InvalidDatasetError) as ei:
        load_trades([
            {
                "ts": 1_700_000_000.0,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
            },
        ])
    assert ei.value.code == "invalid_dataset"
    assert ei.value.hint
    blob = (str(ei.value) + " " + (ei.value.hint or "")).lower()
    assert "ohlcv" in blob or "aggressor" in blob or "side" in blob
    assert "yagni" in (ei.value.hint or "").lower() or "not supported" in blob


def test_fixture_sample_trades_csv_loads():
    """Demo fixture for AAPL-style equity tape (≥20 rows)."""
    path = Path(__file__).resolve().parent / "fixtures" / "sample_trades.csv"
    assert path.is_file()
    trades = load_trades(path, symbol="AAPL")
    assert len(trades) >= 20
    assert any(t.side is Side.BUY for t in trades)
    assert any(t.side is Side.SELL for t in trades)
