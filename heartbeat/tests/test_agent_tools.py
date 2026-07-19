"""Agent / MCP tool surface: TOOL_SCHEMAS + call_tool envelope (Task 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartbeat.agent_tools import TOOL_SCHEMAS, call_tool


def _schema_by_name(name: str) -> dict:
    for s in TOOL_SCHEMAS:
        if s["name"] == name:
            return s
    raise AssertionError(f"missing tool schema: {name}")


def test_tool_schemas_list_has_three_named_tools():
    assert isinstance(TOOL_SCHEMAS, list)
    names = [s["name"] for s in TOOL_SCHEMAS]
    assert names == [
        "heartbeat_requirements",
        "heartbeat_run_dataset",
        "heartbeat_explain",
    ]
    for s in TOOL_SCHEMAS:
        assert "description" in s
        assert "inputSchema" in s or "parameters" in s
        # JSON-serializable (MCP hosts need pure JSON schemas)
        json.dumps(s)


def test_call_tool_requirements_ok():
    env = call_tool("heartbeat_requirements", {})
    assert env["ok"] is True
    assert "result" in env
    result = env["result"]
    assert isinstance(result, dict)
    assert "formats" in result or "accepted_formats" in result
    assert "columns" in result or "required_columns" in result
    blob = json.dumps(result).lower()
    assert "csv" in blob
    assert "side" in blob


def test_call_tool_requirements_none_args():
    env = call_tool("heartbeat_requirements", None)
    assert env["ok"] is True


def test_call_tool_run_dataset_missing_path_envelope_not_traceback():
    env = call_tool(
        "heartbeat_run_dataset",
        {"path": "/nonexistent/path/trades.csv", "symbol": "BTC/USD", "tf": "1h"},
    )
    assert env["ok"] is False
    assert "error" in env
    err = env["error"]
    assert err["code"] == "missing_dataset"
    assert isinstance(err["message"], str) and err["message"]
    assert err.get("hint") is not None
    assert "result" not in env or env.get("result") is None


def test_call_tool_run_dataset_invalid_side_envelope():
    # Use rows so we don't need a temp file
    env = call_tool(
        "heartbeat_run_dataset",
        {
            "rows": [{"ts": 1.0, "price": 100.0, "qty": 1.0, "side": "sideways"}],
            "symbol": "BTC/USD",
            "tf": "1h",
        },
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_dataset"
    assert env["error"]["message"]


def test_call_tool_run_dataset_happy_path_rows():
    base = 1_700_000_000.0
    rows = [
        {
            "ts": base + i * 120.0,
            "price": 100.0 + i * 0.01,
            "qty": 1.0,
            "side": "buy" if i % 2 == 0 else "sell",
        }
        for i in range(50)
    ]
    env = call_tool(
        "heartbeat_run_dataset",
        {
            "rows": rows,
            "symbol": "AGENT/USD",
            "tf": "1h",
            "weights_path": None,  # optional; may degrade
        },
    )
    # weights_path None should be treated as omitted
    assert env["ok"] is True, env
    result = env["result"]
    assert result["symbol"] == "AGENT/USD"
    assert result["tf"] == "1h"
    assert result["n_trades"] == 50
    assert result["p_up"] is not None
    assert 0.0 <= result["p_up"] <= 1.0
    assert result["status"] in ("ok", "degraded", "error")
    assert isinstance(result["warnings"], list)
    assert isinstance(result["series"], list)


def test_call_tool_run_dataset_from_csv_path(tmp_path: Path):
    p = tmp_path / "tape.csv"
    lines = ["ts,price,qty,side"]
    base = 1_700_000_000.0
    for i in range(40):
        side = "buy" if i % 2 == 0 else "sell"
        lines.append(f"{base + i * 120.0},{100.0 + i * 0.01},1.0,{side}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    env = call_tool(
        "heartbeat_run_dataset",
        {"path": str(p), "symbol": "CSV/USD", "tf": "1h"},
    )
    assert env["ok"] is True, env
    assert env["result"]["n_trades"] == 40
    assert 0.0 <= env["result"]["p_up"] <= 1.0


def test_call_tool_run_dataset_requires_path_or_rows():
    env = call_tool(
        "heartbeat_run_dataset",
        {"symbol": "X", "tf": "1h"},
    )
    assert env["ok"] is False
    assert env["error"]["code"] in (
        "invalid_arguments",
        "invalid_dataset",
        "missing_dataset",
        "tool_error",
    )
    assert env["error"]["message"]


def test_call_tool_explain_ok():
    env = call_tool("heartbeat_explain", {})
    assert env["ok"] is True
    text = env["result"]
    assert isinstance(text, str)
    lower = text.lower()
    assert "p_up" in lower or "p(up)" in lower
    assert "tainted" in lower
    assert "uncalibrated" in lower or "default_weight" in lower or "coin" in lower
    # No order path
    assert "order" in lower or "no order" in lower or "not an order" in lower


def test_call_tool_unknown_name_envelope():
    env = call_tool("heartbeat_does_not_exist", {})
    assert env["ok"] is False
    assert env["error"]["code"] in ("unknown_tool", "tool_error")
    assert "heartbeat_does_not_exist" in env["error"]["message"]


def test_call_tool_never_raises_on_missing_file():
    """Hard contract: agent hosts get envelopes, not Python tracebacks."""
    env = call_tool("heartbeat_run_dataset", {"path": ""})
    assert isinstance(env, dict)
    assert "ok" in env
    assert env["ok"] is False


def test_public_export_call_tool():
    import heartbeat as hb

    assert callable(hb.call_tool)
    env = hb.call_tool("heartbeat_requirements", {})
    assert env["ok"] is True


def test_run_dataset_schema_documents_path_or_rows():
    schema = _schema_by_name("heartbeat_run_dataset")
    props = schema.get("inputSchema", schema.get("parameters", {})).get(
        "properties", {}
    )
    # Accept either nested inputSchema (MCP) or flat parameters
    if not props and "parameters" in schema:
        props = schema["parameters"].get("properties", {})
    assert "path" in props or "rows" in props
    assert "symbol" in props
    assert "tf" in props
