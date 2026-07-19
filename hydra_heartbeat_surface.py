"""Read-only heartbeat P(up) surface for the live agent + dashboard.

Polls status files written by a separate `heartbeat run` process
(see heartbeat/README.md Integration contract). Aligns the multi-pair
filename convention used by `hydra_s3.S3Adapter._read_confirmer`:

    {HYDRA_S3_HEARTBEAT_STATUS_DIR}/heartbeat_status_{BASE}_{QUOTE}.json

This module NEVER places orders, never mutates engine state, and never
feeds R1–R11 force_hold. Nested under quant_indicators["heartbeat"] so
R10's top-level field tracker ignores it (same pattern as s3).

Kill switch: HYDRA_HEARTBEAT_SURFACE=0 removes the block entirely
(read per call — live-flippable). Default ON (display only).
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

# Assets whose flow classifier failed the real-tape promote gate
# (HONEST_FINDINGS). Surface still shows p_up if a process is running,
# but flags flow_gate_fail so the UI does not present them as edge.
FLOW_GATE_FAIL_ASSETS = frozenset({"SOL/USD", "ZEC/USD"})

DEFAULT_STATUS_DIR = os.environ.get(
    "HYDRA_S3_HEARTBEAT_STATUS_DIR",
    str(Path("heartbeat") / "data"),
)
STALE_S = 300.0
HISTORY_MAX = 80


def pair_status_filename(pair: str) -> str:
    """Canonical multi-pair status filename (matches hydra_s3 confirmer)."""
    return f"heartbeat_status_{pair.replace('/', '_')}.json"


def resolve_status_path(status_file_or_dir: str | Path, pair: str) -> Path:
    """Map config path + pair → pair-named status file.

    - Directory (no .json suffix) → ``{dir}/heartbeat_status_BTC_USD.json``
    - Generic ``.../heartbeat_status.json`` → sibling pair-named file
    - Already pair-named path → pass through
    """
    raw = Path(status_file_or_dir)
    token = pair.replace("/", "_")
    pair_name = f"heartbeat_status_{token}.json"
    if token in raw.name and raw.suffix == ".json":
        return raw
    if raw.suffix != ".json":
        return raw / pair_name
    # Generic single-file name → Hydra multi-pair sibling
    return raw.parent / pair_name


def read_status(
    path: Path,
    now: Optional[float] = None,
    stale_s: float = STALE_S,
) -> Dict[str, Any]:
    """Read one status file. Missing/stale/tainted → no_opinion (never 0.5)."""
    now = time.time() if now is None else float(now)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "no_opinion", "why": "missing", "p_up": None,
                "active": False}

    if raw.get("tainted"):
        return {"status": "no_opinion", "why": "tainted", "p_up": None,
                "active": False, "ts": raw.get("ts"), "tainted": True}

    try:
        ts = float(raw.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if now - ts > stale_s:
        return {"status": "no_opinion", "why": "stale", "p_up": None,
                "active": False, "ts": ts}

    p_up = raw.get("p_up")
    try:
        p_up_f = float(p_up) if p_up is not None else None
    except (TypeError, ValueError):
        p_up_f = None

    out: Dict[str, Any] = {
        "status": "ok",
        "active": True,
        "p_up": p_up_f,
        "L": raw.get("L"),
        "ts": ts,
        "pair": raw.get("pair"),
        "tf": raw.get("tf"),
        "candle_progress": raw.get("candle_progress"),
        "tainted": False,
        "gap_count": raw.get("gap_count"),
        "max_clock_skew_s": raw.get("max_clock_skew_s"),
        "features": raw.get("features"),
    }
    return out


class HeartbeatSurface:
    """Per-agent poller. One instance; status_dir shared with S3 confirmer."""

    def __init__(
        self,
        pairs: List[str],
        status_dir: Optional[str] = None,
        history_max: int = HISTORY_MAX,
        stale_s: float = STALE_S,
    ):
        self.pairs = list(pairs)
        self.status_dir = status_dir or DEFAULT_STATUS_DIR
        self.stale_s = float(stale_s)
        self._history: Dict[str, Deque[Dict[str, Any]]] = {
            p: deque(maxlen=history_max) for p in pairs
        }
        self._last_ts: Dict[str, float] = {}

    @staticmethod
    def disabled() -> bool:
        return os.environ.get("HYDRA_HEARTBEAT_SURFACE") == "0"

    def indicator_block(self, pair: str) -> Dict[str, Any]:
        """Build quant_indicators['heartbeat'] for one pair. {} if killed."""
        if self.disabled():
            return {}
        path = resolve_status_path(self.status_dir, pair)
        block = read_status(path, now=time.time(), stale_s=self.stale_s)
        block["path"] = str(path)
        if pair in FLOW_GATE_FAIL_ASSETS:
            block["flow_gate_fail"] = True

        # Append history only on fresh ok samples with a new ts
        if block.get("status") == "ok" and block.get("p_up") is not None:
            ts = float(block.get("ts") or 0.0)
            prev = self._last_ts.get(pair)
            hist = self._history.setdefault(pair, deque(maxlen=HISTORY_MAX))
            if prev is None or ts != prev:
                hist.append({
                    "ts": ts,
                    "p_up": float(block["p_up"]),
                    "L": block.get("L"),
                })
                self._last_ts[pair] = ts
            block["history"] = list(hist)
        else:
            hist = self._history.get(pair)
            if hist:
                block["history"] = list(hist)

        return block
