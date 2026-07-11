"""Live tape capture: subscribes to CandleStream pushes, writes closed
candles to hydra_history.sqlite (source='tape') via a dedicated writer
thread + bounded queue. The agent's main loop must never stall on a SQLite
fsync — on queue full, candles are dropped and counted (live trading
priority over historical fidelity)."""
from __future__ import annotations

import datetime as _dt
import queue
import threading
from typing import Any, Dict, Optional

from hydra_history_store import CandleRow, HistoryStore


def _parse_iso_to_ts(s: str) -> int:
    # WS v2 emits "interval_begin" as ISO 8601 with Z; tolerant parse.
    s = s.replace("Z", "+00:00")
    try:
        return int(_dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


class TapeCapture:
    def __init__(self, store: HistoryStore, queue_max: int = 256):
        self._store = store
        self._q: "queue.Queue[Optional[CandleRow]]" = queue.Queue(maxsize=queue_max)
        self._thr: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.dropped = 0
        self._dropped_lock = threading.Lock()

    def on_candle(self, pair: str, candle: Dict[str, Any]) -> None:
        """Hook for CandleStream.on_candle. Non-blocking; drops on queue full."""
        ib = candle.get("interval_begin")
        ts = _parse_iso_to_ts(ib) if ib else 0
        if ts <= 0:
            return
        interval_min = int(candle.get("interval", 60))
        grain_sec = interval_min * 60
        try:
            row = CandleRow(
                pair=pair,
                grain_sec=grain_sec,
                ts=ts,
                open=float(candle.get("open", 0)),
                high=float(candle.get("high", 0)),
                low=float(candle.get("low", 0)),
                close=float(candle.get("close", 0)),
                volume=float(candle.get("volume", 0)),
                source="tape",
            )
        except (TypeError, ValueError):
            return
        try:
            self._q.put_nowait(row)
        except queue.Full:
            with self._dropped_lock:
                self.dropped += 1

    def start(self) -> None:
        if self._thr is not None:
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="TapeCapture",
                                     daemon=True)
        self._thr.start()

    def stop(self) -> None:
        """Stop writer thread without blocking shutdown forever.

        v2.27.6: never use unbounded ``put`` for the sentinel — a full queue
        (stalled SQLite) would hang agent shutdown *before* snapshot flush.
        """
        self._stop.set()
        try:
            self._q.put_nowait(None)  # sentinel
        except queue.Full:
            try:
                # Drop one item to make room for sentinel (best-effort).
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass  # thread will exit on _stop even without sentinel
        if self._thr:
            self._thr.join(timeout=5.0)
        self._thr = None

    def flush(self, timeout: float = 5.0) -> None:
        """Block until queue drains (test/dev helper)."""
        deadline_ev = threading.Event()

        def _watch():
            self._q.join()
            deadline_ev.set()

        threading.Thread(target=_watch, daemon=True).start()
        deadline_ev.wait(timeout=timeout)

    def _run(self) -> None:
        batch: list = []
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                if batch:
                    self._flush_batch(batch)
                    batch.clear()
                continue
            if item is None:
                self._q.task_done()
                break
            batch.append(item)
            self._q.task_done()
            if len(batch) >= 32:
                self._flush_batch(batch)
                batch.clear()
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list) -> None:
        try:
            self._store.upsert_candles(batch)
        except Exception as e:
            print(f"  [TAPE] flush error: {type(e).__name__}: {e}")
