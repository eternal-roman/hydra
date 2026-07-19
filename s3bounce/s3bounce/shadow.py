"""Shadow ledger — the S3 paper-shadow window's storage.

Two files under the ledger directory:

  events.jsonl   append-only event lines: {"type": "proposal"|"close", ...}
  state.json     open positions + proposed setup ids; written atomically
                 (.tmp -> os.replace) so a crash can never corrupt it.
                 A malformed/garbage state file is treated as empty
                 (events.jsonl remains the audit trail).

Each accepted proposal opens one shadow position per exit arm; positions
advance bar-by-bar through exits.evaluate. Dedupe: a setup id (asset +
low day + low price) is proposed at most once, across restarts.
NO ORDER PATH: this module never talks to an exchange, by construction.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .candles import DailyBar
from .exits import OpenPosition, evaluate

FEE_PER_SIDE = 0.0026


class ShadowLedger:
    def __init__(self, dir_path: str):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.state_path = self.dir / "state.json"
        self.proposed: set[str] = set()
        self.open: list[dict] = []       # serialized OpenPosition + bar_count
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.proposed = set(raw.get("proposed", []))
            self.open = list(raw.get("open", []))
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            self.proposed, self.open = set(), []

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"proposed": sorted(self.proposed),
                                   "open": self.open}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _emit(self, event: dict) -> None:
        event.setdefault("logged_at", time.time())
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    # -- API ----------------------------------------------------------------
    @staticmethod
    def setup_key(asset: str, low_ts: float, low_px: float) -> str:
        return f"{asset}|{int(low_ts)}|{low_px:.10g}"

    def propose(self, asset: str, low_ts: float, low_px: float, atr: float,
                low_idx: int, entry_idx: int, entry_ts: float,
                entry_px: float, score: float, arms: list[str],
                confirmer: Optional[dict] = None,
                extra: Optional[dict] = None,
                premium_cut: Optional[float] = None) -> bool:
        """Open shadow positions for every arm; False if already proposed.
        `premium_cut` is the x5_vigor_routed threshold from the artifact —
        stored on every arm position (only x5 reads it)."""
        key = self.setup_key(asset, low_ts, low_px)
        if key in self.proposed:
            return False
        self.proposed.add(key)
        self._emit({"type": "proposal", "key": key, "asset": asset,
                    "low_ts": low_ts, "low_px": low_px, "atr": atr,
                    "entry_ts": entry_ts, "entry_px": entry_px,
                    "score": score, "arms": arms,
                    "premium_atr": round((entry_px - low_px) / atr, 4),
                    "premium_cut": premium_cut,
                    "confirmer": confirmer, "extra": extra or {}})
        for arm in arms:
            self.open.append({"key": key, "asset": asset, "arm": arm,
                              "entry_ts": entry_ts, "entry_px": entry_px,
                              "low_px": low_px, "atr": atr,
                              "low_idx": low_idx, "entry_idx": entry_idx,
                              "premium_cut": premium_cut,
                              "armed": False, "bars_seen": 0})
        self._save()
        return True

    def mark_bar(self, asset: str, bar: DailyBar,
                 ma9: Optional[float] = None) -> list[dict]:
        """Advance every open position of `asset` through one completed
        bar that follows its entry bar. Returns close events emitted.
        `ma9` = 9-bar MA of closes including `bar` (trail arms; the
        caller owns the bar series — None keeps trails open)."""
        closes = []
        still_open = []
        advanced = False
        for p in self.open:
            # Idempotent per bar: callers re-mark recent bars every tick;
            # a bar at-or-before the last marked (or entry) bar must not
            # advance the hold counter again.
            seen_through = max(p["entry_ts"], p.get("last_bar_ts", 0.0))
            if p["asset"] != asset or bar.open_ts <= seen_through:
                still_open.append(p)
                continue
            p["last_bar_ts"] = bar.open_ts
            p["bars_seen"] += 1
            advanced = True
            pos = OpenPosition(asset=p["asset"], arm=p["arm"],
                               entry_ts=p["entry_ts"], entry_px=p["entry_px"],
                               low_px=p["low_px"], atr=p["atr"],
                               low_idx=p["low_idx"], entry_idx=p["entry_idx"],
                               armed=bool(p.get("armed", False)),
                               premium_cut=p.get("premium_cut"))
            # bar_idx reconstructed from bars elapsed since entry
            bar_idx = p["entry_idx"] + p["bars_seen"]
            d = evaluate(p["arm"], pos, bar, bar_idx, ma9)
            p["armed"] = pos.armed        # trail arming survives restarts
            if d is None:
                still_open.append(p)
                continue
            ret = d.price / p["entry_px"] - 1.0 - 2 * FEE_PER_SIDE
            ev = {"type": "close", "key": p["key"], "asset": asset,
                  "arm": p["arm"], "exit_ts": bar.open_ts,
                  "exit_px": d.price, "reason": d.reason,
                  "hold_bars": p["bars_seen"], "ret_net": ret}
            self._emit(ev)
            closes.append(ev)
        self.open = still_open
        if advanced or closes:
            self._save()          # hold counters must survive restarts
        return closes
