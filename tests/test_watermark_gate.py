"""watermark_gate.py: trailer fails, clean text passes."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "watermark_gate.py"


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--files", str(path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_trailer_fails() -> None:
    p = Path(tempfile.gettempdir()) / "hydra_wm_gate_trailer.txt"
    # Split so this test file itself is not a trailer hit under MULTILINE ^.
    p.write_text("fix\n" + "Co-Authored-By: " + "Claude <noreply@anthropic.com>\n", encoding="utf-8")
    proc = _run(p)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is False
    kinds = {h["kind"] for item in data["findings"] for h in item["hits"]}
    assert "trailer" in kinds


def test_gate_script_is_not_a_self_hit() -> None:
    proc = _run(GATE)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_clean_text_passes() -> None:
    p = Path(tempfile.gettempdir()) / "hydra_wm_gate_clean.txt"
    p.write_text("hello world\n", encoding="utf-8")
    proc = _run(p)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["findings"] == []
