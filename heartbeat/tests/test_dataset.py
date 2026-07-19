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
