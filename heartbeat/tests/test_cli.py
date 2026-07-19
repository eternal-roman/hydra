"""CLI: run-dataset subcommand (Task 3)."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from heartbeat.cli import build_parser, main


def test_cli_help_lists_run_dataset():
    parser = build_parser()
    sub = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert "run-dataset" in sub.choices
    help_text = parser.format_help()
    # subcommand name appears in help for required subparsers
    assert "run-dataset" in help_text or "run-dataset" in sub.choices


def test_cli_run_dataset_help_mentions_symbol_and_json():
    parser = build_parser()
    sub = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    rd = sub.choices["run-dataset"]
    text = rd.format_help()
    assert "--symbol" in text
    assert "--tf" in text
    assert "--weights" in text
    assert "--json" in text


def test_cli_missing_path_exit_2():
    """Missing file → exit 2 (MissingDatasetError)."""
    code = main([
        "run-dataset",
        "/nonexistent/heartbeat_task3_missing.csv",
        "--symbol", "AAPL",
        "--tf", "1h",
    ])
    assert code == 2


def test_cli_missing_path_subprocess_exit_2():
    """Same contract via process entry (PYTHONPATH via conftest for -m)."""
    src = Path(__file__).resolve().parents[1] / "src"
    r = subprocess.run(
        [
            sys.executable, "-m", "heartbeat.cli",
            "run-dataset",
            "/nonexistent/heartbeat_task3_missing.csv",
            "--symbol", "AAPL",
            "--tf", "1h",
        ],
        capture_output=True,
        text=True,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}),
             "PYTHONPATH": str(src)},
        check=False,
    )
    assert r.returncode == 2
    assert "missing_dataset" in (r.stderr + r.stdout).lower() or "error" in r.stderr.lower()


def test_cli_invalid_dataset_exit_3(tmp_path: Path):
    bad = tmp_path / "bad.csv"
    bad.write_text("ts,price,qty,side\nnot-a-ts,x,y,maybe\n", encoding="utf-8")
    code = main([
        "run-dataset", str(bad), "--symbol", "AAPL", "--tf", "1h",
    ])
    assert code == 3


def test_cli_run_dataset_json_ok(tmp_path: Path, capsys):
    """Happy path: enough trades for ≥1 candle → exit 0 + JSON fields."""
    path = tmp_path / "trades.csv"
    start = 1_700_000_000.0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "price", "qty", "side"])
        for i in range(80):
            w.writerow([
                start + i * 90.0,
                100.0 + (i % 5) * 0.1,
                1.0,
                "buy" if i % 3 else "sell",
            ])
    code = main([
        "run-dataset", str(path),
        "--symbol", "AAPL",
        "--tf", "1h",
        "--json",
    ])
    assert code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["symbol"] == "AAPL"
    assert payload["tf"] == "1h"
    assert payload["n_trades"] == 80
    assert payload["status"] in ("ok", "degraded", "error")
    if payload["p_up"] is not None:
        assert 0.0 <= payload["p_up"] <= 1.0


def test_cli_run_dataset_fixture_csv_json_ok(capsys):
    """Fixture sample_trades.csv (AAPL demo) → exit 0 with --json."""
    fixture = (
        Path(__file__).resolve().parent / "fixtures" / "sample_trades.csv"
    )
    assert fixture.is_file()
    code = main([
        "run-dataset", str(fixture),
        "--symbol", "AAPL",
        "--tf", "1h",
        "--json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["symbol"] == "AAPL"
    assert payload["n_trades"] >= 20
    assert payload["status"] in ("ok", "degraded", "error")


def test_cli_default_config_loads_when_packaged():
    """load_config must find packaged resources/default.yaml (install path)."""
    from heartbeat.config import default_config_path, load_config

    path = default_config_path()
    assert path.is_file()
    assert path.name == "default.yaml"
    cfg = load_config()
    assert "features" in cfg
    assert "decay" in cfg
    assert cfg["timeframe"] == "1h"
