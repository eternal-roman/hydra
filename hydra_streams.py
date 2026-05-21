"""Hydra WebSocket Streams."""
import subprocess
import json
import time
import os
import shlex
import threading
import queue
from typing import Dict, List, Optional, Any, Tuple

from hydra_kraken_cli import KrakenCLI, WSL_DISTRO

# ═══════════════════════════════════════════════════════════════
# BASE STREAM — shared WS subprocess/reader/health infrastructure
# ═══════════════════════════════════════════════════════════════

class BaseStream:
    """Shared infrastructure for all Kraken WS CLI subprocess streams.

    Subclasses override:
        _build_cmd() -> str   — the bash command inside WSL
        _on_message(msg)      — handle one parsed JSON message
        _stream_label() -> str — short label for log lines (e.g. "EXECSTREAM")
    """

    HEARTBEAT_TIMEOUT_S = 30.0
    READER_JOIN_TIMEOUT_S = 5.0
    RESTART_COOLDOWN_S = 30.0

    def __init__(self, paper: bool = False):
        self.paper = paper
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._last_heartbeat: float = 0.0
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._reader_exit_reason: Optional[str] = None
        self._last_restart_attempt: float = 0.0
        self._restart_count: int = 0

    def _build_cmd(self) -> str:
        """Return the bash command to run inside WSL. Subclasses must override."""
        raise NotImplementedError

    def _on_message(self, msg: Dict[str, Any]) -> None:
        """Handle one parsed JSON message. Subclasses must override."""
        raise NotImplementedError

    def _stream_label(self) -> str:
        """Short label for log lines. Override for a better name."""
        return "STREAM"

    def _on_heartbeat(self) -> None:
        """Bump the heartbeat timestamp. Call from _on_message on any
        liveness-indicating traffic."""
        self._last_heartbeat = time.monotonic()

    # ───────── lifecycle ─────────

    def start(self) -> bool:
        """Spawn the subprocess and reader/stderr threads. Returns True on success."""
        if self.paper:
            self._last_heartbeat = time.monotonic()
            return True
        self._shutdown.clear()
        self._reader_exit_reason = None
        self._on_start_reset()
        label = self._stream_label()
        cmd_str = f"source ~/.cargo/env && {self._build_cmd()}"
        api_key = os.environ.get("KRAKEN_API_KEY")
        api_secret = os.environ.get("KRAKEN_API_SECRET")
        if api_key and api_secret:
            cmd_str = f"export KRAKEN_API_KEY={shlex.quote(api_key)} && export KRAKEN_API_SECRET={shlex.quote(api_secret)} && {cmd_str}"

        cmd = [
            "wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd_str
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True,
            )
        except Exception as e:
            print(f"  [{label}] failed to spawn subprocess: {type(e).__name__}: {e}")
            return False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"{label}-reader", daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{label}-stderr", daemon=True,
        )
        self._stderr_thread.start()
        self._last_heartbeat = time.monotonic()
        print(f"  [{label}] stream started")
        return True

    def _on_start_reset(self) -> None:
        """Hook for subclasses to reset state on (re)start. Called before spawn."""
        pass

    def stop(self) -> None:
        """Terminate subprocess, join reader and stderr threads. Idempotent."""
        self._shutdown.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            self._proc = None
        for attr in ("_reader_thread", "_stderr_thread"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.join(timeout=self.READER_JOIN_TIMEOUT_S)
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
                setattr(self, attr, None)

    @property
    def healthy(self) -> bool:
        return self.health_status()[0]

    def health_status(self) -> Tuple[bool, str]:
        if self.paper:
            return True, ""
        if self._proc is None:
            return False, "subprocess not started"
        rc = self._proc.poll()
        if rc is not None:
            return False, f"subprocess exited (rc={rc})"
        if self._reader_thread is None or not self._reader_thread.is_alive():
            reason = self._reader_exit_reason or "exited (reason unknown)"
            return False, f"reader thread {reason}"
        age = time.monotonic() - self._last_heartbeat
        if age > self.HEARTBEAT_TIMEOUT_S:
            return False, (
                f"no heartbeat for {age:.0f}s "
                f"(threshold {self.HEARTBEAT_TIMEOUT_S:.0f}s)"
            )
        return True, ""

    def ensure_healthy(self) -> Tuple[bool, str]:
        if self.paper:
            return True, ""
        healthy, reason = self.health_status()
        if healthy:
            return True, ""
        now = time.monotonic()
        if now - self._last_restart_attempt < self.RESTART_COOLDOWN_S:
            return healthy, reason
        self._last_restart_attempt = now
        self._restart_count += 1
        label = self._stream_label()
        print(f"  [{label}] auto-restart #{self._restart_count}: {reason}")
        try:
            self.stop()
        except Exception as e:
            print(f"  [{label}] stop during restart failed: {type(e).__name__}: {e}")
        if not self.start():
            return False, "restart spawn failed"
        new_healthy, new_reason = self.health_status()
        if new_healthy:
            self._on_restart_success()
        return new_healthy, new_reason

    def _on_restart_success(self) -> None:
        """Hook for subclasses to run post-restart logic (e.g. reconciliation)."""
        pass

    # ───────── reader thread ─────────

    def _reader_loop(self) -> None:
        assert self._proc is not None
        label = self._stream_label()
        exit_reason = "EOF (subprocess closed stdout)"
        try:
            for raw in self._proc.stdout:  # type: ignore[union-attr]
                if self._shutdown.is_set():
                    exit_reason = "shutdown signal"
                    break
                line = raw.rstrip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    print(f"  [{label}] non-JSON line: {line[:120]}")
                    continue
                if "error" in msg:
                    print(f"  [{label}] FATAL: WS error from kraken: {msg}")
                elif "errorMessage" in msg:
                    print(f"  [{label}] FATAL: WS error from kraken: {msg}")
                self._on_message(msg)
        except Exception as e:
            exit_reason = f"crashed: {type(e).__name__}: {e}"
            if not self._shutdown.is_set():
                print(f"  [{label}] reader thread error: {type(e).__name__}: {e}")
        finally:
            self._reader_exit_reason = exit_reason
            if not self._shutdown.is_set():
                print(f"  [{label}] reader thread exited: {exit_reason}")

    def _stderr_loop(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        label = self._stream_label()
        try:
            for raw in self._proc.stderr:  # type: ignore[union-attr]
                if self._shutdown.is_set():
                    break
                line = raw.rstrip()
                if line:
                    print(f"  [{label} stderr] {line[:200]}")
        except Exception as e:
            if not self._shutdown.is_set():
                print(f"  [{label}] stderr reader error: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════
# CANDLE STREAM — kraken ws ohlc push-based candle updates
# ═══════════════════════════════════════════════════════════════

class CandleStream(BaseStream):
    """Push-based OHLC candle stream. Subscribes to all traded pairs in one
    WS connection, stores the latest candle per pair, and exposes it via
    latest_candle(pair). Falls back to REST ohlc() when unhealthy."""

    # Reverse map: WS symbol (e.g. "SOL/USD", "SOL/BTC") → friendly pair.
    # Built dynamically from the pairs list at init.

    def __init__(self, pairs: List[str], interval: int = 5, paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._interval = interval
        self._latest: Dict[str, dict] = {}
        # Build symbol → friendly pair reverse map.
        # WS v2 returns symbols like "SOL/BTC", "BTC/USD" (canonical names).
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p
        self._candle_callbacks: list = []

    def on_candle(self, callback) -> None:
        """Register a callback fired on each push: callback(pair: str, candle: dict).

        Callbacks must be fast and non-blocking — they run inside the WS thread,
        wrapped in try/except so a bad subscriber cannot kill the stream.

        Registration order:
        - Safe to call BEFORE start(): callbacks accumulate in _candle_callbacks
          and fire as soon as the WS connection delivers its first candle. No
          startup race; no "missed early candles" failure mode.
        - Safe to call AFTER start(): the dispatch loop snapshots the list under
          lock on every message, so newly-registered callbacks pick up on the
          next candle.
        """
        with self._lock:
            self._candle_callbacks.append(callback)

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return (f"exec kraken ws ohlc {pairs_str} "
                f"--interval {self._interval} -o json --snapshot true")

    def _stream_label(self) -> str:
        return "CANDLE_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "ohlc":
            # status, subscribe confirmations — bump heartbeat on status
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [CANDLE_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if pair:
                with self._lock:
                    self._latest[pair] = entry
                    cbs = list(self._candle_callbacks)
                for cb in cbs:
                    try:
                        cb(pair, entry)
                    except Exception as e:
                        print(f"  [CANDLE_WS] callback error: {type(e).__name__}: {e}")

    def latest_candle(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the most recent candle for the given pair, or None."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# TICKER STREAM — kraken ws ticker push-based price updates
# ═══════════════════════════════════════════════════════════════

class TickerStream(BaseStream):
    """Push-based ticker stream. Subscribes to all traded pairs in one WS
    connection, stores the latest ticker per pair, and exposes it via
    latest_ticker(pair). Falls back to REST ticker() when unhealthy."""

    def __init__(self, pairs: List[str], paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._latest: Dict[str, dict] = {}
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return f"exec kraken ws ticker {pairs_str} -o json --snapshot true"

    def _stream_label(self) -> str:
        return "TICKER_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "ticker":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [TICKER_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if pair:
                with self._lock:
                    self._latest[pair] = entry

    def latest_ticker(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the most recent ticker for the given pair, or None."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# BOOK STREAM — kraken ws book push-based order book updates
# ═══════════════════════════════════════════════════════════════

class BookStream(BaseStream):
    """Push-based order book stream. Subscribes to all traded pairs in one WS
    connection, stores the latest book per pair, and exposes it via
    latest_book(pair) in the REST-compatible format that OrderBookAnalyzer
    expects: {"bids": [[price, qty, ts], ...], "asks": [[price, qty, ts], ...]}.

    WS book snapshots include a checksum for integrity; we store the raw
    snapshot/update data and convert to REST format on read."""

    def __init__(self, pairs: List[str], depth: int = 10, paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._depth = depth
        self._latest: Dict[str, dict] = {}
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return (f"exec kraken ws book {pairs_str} "
                f"--depth {self._depth} -o json --snapshot true")

    def _stream_label(self) -> str:
        return "BOOK_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "book":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [BOOK_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if not pair:
                continue
            # Convert WS format {price, qty} dicts to REST format [price, qty, 0]
            # so OrderBookAnalyzer works unchanged. Skip malformed levels rather
            # than crash the reader thread on a single bad row.
            def _as_level(d):
                try:
                    return [float(d.get("price", 0)), float(d.get("qty", 0)), 0]
                except (TypeError, ValueError):
                    return None
            bids = []
            for b in entry.get("bids", []):
                if isinstance(b, dict):
                    lv = _as_level(b)
                    if lv is not None:
                        bids.append(lv)
            asks = []
            for a in entry.get("asks", []):
                if isinstance(a, dict):
                    lv = _as_level(a)
                    if lv is not None:
                        asks.append(lv)
            with self._lock:
                self._latest[pair] = {"bids": bids, "asks": asks}

    def latest_book(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the latest order book for the pair in REST-compatible format,
        or None if no data available."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# BALANCE STREAM — kraken ws balances push-based balance updates
# ═══════════════════════════════════════════════════════════════

class BalanceStream(BaseStream):
    """Push-based balance stream. Receives real-time balance updates for all
    assets. latest_balances() returns {asset: amount} for non-zero currency
    balances, matching the shape of KrakenCLI.balance().

    WS returns asset names like "BTC", "USD", "USDC", "SOL" etc.
    We normalize via KrakenCLI._normalize_asset so callers see canonical names.
    Only currency assets are included (equities/ETFs filtered out)."""

    def __init__(self, paper: bool = False):
        super().__init__(paper=paper)
        self._balances: Dict[str, float] = {}

    def _build_cmd(self) -> str:
        return "exec kraken ws balances -o json --snapshot true"

    def _stream_label(self) -> str:
        return "BALANCE_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "balances":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [BALANCE_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            # Only include currency assets (skip equities/ETFs)
            if entry.get("asset_class", "currency") != "currency":
                continue
            asset = entry.get("asset", "")
            balance = entry.get("balance")
            if not asset or balance is None:
                continue
            normalized = KrakenCLI._normalize_asset(asset)
            try:
                bal = float(balance)
            except (TypeError, ValueError):
                # Malformed balance value — skip this entry rather than crash
                # the reader thread (which would force a stream restart).
                continue
            with self._lock:
                if bal > 0:
                    self._balances[normalized] = bal
                else:
                    self._balances.pop(normalized, None)

    def latest_balances(self) -> Dict[str, float]:
        """Return {asset: amount} for non-zero currency balances."""
        with self._lock:
            return dict(self._balances)


# ═══════════════════════════════════════════════════════════════
# EXECUTION STREAM — kraken ws executions push reconciler
# ═══════════════════════════════════════════════════════════════

def _is_fully_filled(vol_exec: float, placed: float, tolerance: float = 0.01) -> bool:
    """Shared fill-detection: True if vol_exec is within `tolerance` (1%)
    of the placed amount. Used by ExecutionStream, restart-gap reconciliation,
    and resume reconciliation so all paths agree."""
    if placed <= 0:
        return False
    return abs(vol_exec - placed) / placed < tolerance


class ExecutionStream(BaseStream):
    """Consumes `kraken ws executions` and delivers push-based lifecycle
    events to the agent tick loop.

    Correlation keys: order_id (from REST placement response) is primary;
    order_userref (numeric tag we passed on placement) is fallback. Both
    are checked — whichever arrives first resolves the match.

    Paper mode uses paper=True which short-circuits start() and lets the
    place_order helper emit synthetic terminal events directly into the
    event queue. No subprocess is spawned.
    """

    def __init__(self, paper: bool = False):
        super().__init__(paper=paper)
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()
        self._known_orders: Dict[str, dict] = {}
        self._userref_to_order_id: Dict[int, str] = {}
        self._last_sequence: Optional[int] = None
        self._pending_reconciliation: List[Dict[str, Any]] = []

    def _build_cmd(self) -> str:
        return ("exec kraken ws executions -o json "
                "--snap-orders true --snap-trades true")

    def _stream_label(self) -> str:
        return "EXECSTREAM"

    def _on_start_reset(self) -> None:
        # Reset sequence on (re)start — new WS connection starts at seq 1.
        # _known_orders intentionally NOT cleared — in-flight orders must
        # survive restarts for snapshot replay to finalize them.
        self._last_sequence = None

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel == "status":
            return
        if msg.get("method") == "subscribe":
            if not msg.get("success"):
                print(f"  [EXECSTREAM] subscribe failed: {msg}")
            return
        if channel != "executions":
            return
        self._on_heartbeat()
        seq = msg.get("sequence")
        if isinstance(seq, int):
            if self._last_sequence is not None and seq != self._last_sequence + 1:
                print(
                    f"  [EXECSTREAM] sequence gap {self._last_sequence}->{seq} "
                    f"(executions may have been dropped; waiting for next snapshot)"
                )
            self._last_sequence = seq
        msg_type = msg.get("type")
        data = msg.get("data") or []
        if not isinstance(data, list):
            return
        for entry in data:
            if isinstance(entry, dict):
                self._event_queue.put((msg_type or "update", entry))

    def _on_restart_success(self) -> None:
        try:
            gap_events = self.reconcile_restart_gap()
            if gap_events:
                self._pending_reconciliation.extend(gap_events)
        except Exception as e:
            print(f"  [EXECSTREAM] restart-gap reconcile failed: {type(e).__name__}: {e}")

    # ───────── restart-gap reconciliation ─────────

    def reconcile_restart_gap(self) -> List[Dict[str, Any]]:
        """Query Kraken for orders in _known_orders that may have filled or
        cancelled while the execution stream was down."""
        if self.paper or not self._known_orders:
            return []

        with self._lock:
            order_ids = [oid for oid in self._known_orders if oid != "unknown"]
        if not order_ids:
            return []

        terminal_events: List[Dict[str, Any]] = []
        BATCH = 20

        for i in range(0, len(order_ids), BATCH):
            batch = order_ids[i:i + BATCH]
            time.sleep(2)
            resp = KrakenCLI.query_orders(*batch, trades=True)
            if not isinstance(resp, dict) or "error" in resp:
                continue

            for txid, order_info in resp.items():
                if not isinstance(order_info, dict):
                    continue
                with self._lock:
                    known = self._known_orders.get(txid)
                if not known:
                    continue

                status = order_info.get("status", "")
                if status not in ("closed", "canceled", "expired"):
                    continue

                vol_exec = float(order_info.get("vol_exec", 0))
                placed = known["placed_amount"]
                raw_price = float(order_info.get("price", 0))
                avg_price = raw_price if raw_price > 0 else None
                fee = float(order_info.get("fee", 0))

                if status == "closed":
                    state = (
                        "FILLED"
                        if _is_fully_filled(vol_exec, placed)
                        else "PARTIALLY_FILLED"
                    )
                elif vol_exec > 0:
                    state = "PARTIALLY_FILLED"
                else:
                    state = "CANCELLED_UNFILLED"

                event = {
                    "order_id": txid,
                    "journal_index": known["journal_index"],
                    "engine_ref": known["engine_ref"],
                    "pre_trade_snapshot": known["pre_trade_snapshot"],
                    "placed_amount": placed,
                    "pair": known["pair"],
                    "side": known["side"],
                    "state": state,
                    "vol_exec": vol_exec,
                    "avg_fill_price": avg_price,
                    "fee_quote": fee,
                    "terminal_reason": f"reconciled after stream restart ({status})",
                    "exec_ids": [],
                    "timestamp": order_info.get("closetm") or order_info.get("opentm"),
                }
                terminal_events.append(event)
                with self._lock:
                    self._known_orders.pop(txid, None)

        if terminal_events:
            print(f"  [EXECSTREAM] reconciled {len(terminal_events)} order(s) after restart gap")
        return terminal_events

    # ───────── registration ─────────

    def register(self, *, order_id: str, userref: Optional[int],
                 journal_index: int, pair: str, side: str,
                 placed_amount: float, engine_ref: Any,
                 pre_trade_snapshot: Any) -> None:
        """Correlate an in-flight placement with its journal entry and
        rollback handle. Skips registration when order_id is 'unknown'
        (REST returned no txid) — such orders can't be tracked by id and
        won't finalize via this stream; the placement helper should log
        a warning in that case."""
        if not order_id or order_id == "unknown":
            return
        with self._lock:
            self._known_orders[order_id] = {
                "order_id": order_id,
                "userref": userref,
                "journal_index": journal_index,
                "pair": pair,
                "side": side,
                "placed_amount": float(placed_amount),
                "engine_ref": engine_ref,
                "pre_trade_snapshot": pre_trade_snapshot,
                "registered_at": time.time(),
                "vol_exec_running": 0.0,
                "cost_running": 0.0,
                "fee_running": 0.0,
                "exec_ids": [],
            }
            if userref is not None:
                self._userref_to_order_id[int(userref)] = order_id

    def inject_event(self, entry: Dict[str, Any], *, kind: str = "update") -> None:
        """Test/paper hook: push an execution entry straight into the queue
        without going through the subprocess. Used by paper mode to synthesize
        fill events and by FakeExecutionStream in tests."""
        self._event_queue.put((kind, entry))

    # ───────── consumption ─────────

    # Terminal Kraken order_status values
    _TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}

    def drain_events(self) -> List[Dict[str, Any]]:
        """Called once per tick. Pops every queued WS entry, updates the
        per-order aggregator, and emits one terminal event per order that
        finished this drain. Non-terminal updates (pending_new, new,
        interim partial fills) update internal state silently.

        Returned event shape (flat dict, agent applies directly to journal
        + engine state):

            {
                "order_id":          str,
                "journal_index":     int,
                "engine_ref":        HydraEngine,
                "pre_trade_snapshot": dict,
                "placed_amount":     float,
                "pair":              str,
                "side":              "BUY" | "SELL",
                "state":             "FILLED" | "PARTIALLY_FILLED" |
                                     "CANCELLED_UNFILLED" | "REJECTED",
                "vol_exec":          float,
                "avg_fill_price":    Optional[float],
                "fee_quote":         float,
                "terminal_reason":   Optional[str],
                "exec_ids":          List[str],
                "timestamp":         Optional[str],
            }
        """
        events: List[Dict[str, Any]] = []
        # Prepend any events from restart-gap reconciliation so the agent
        # processes them in the same tick the stream recovered.
        if self._pending_reconciliation:
            events.extend(self._pending_reconciliation)
            self._pending_reconciliation.clear()
        while True:
            try:
                _kind, entry = self._event_queue.get_nowait()
            except queue.Empty:
                break
            term = self._apply_entry(entry)
            if term is not None:
                events.append(term)
        return events

    def _apply_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fold one WS execution entry into the per-order aggregate. Returns
        a terminal event if the order finalized on this entry, else None."""
        order_id = entry.get("order_id")
        userref = entry.get("order_userref")
        with self._lock:
            known: Optional[dict] = None
            if isinstance(order_id, str) and order_id in self._known_orders:
                known = self._known_orders[order_id]
            elif isinstance(userref, int) and userref in self._userref_to_order_id:
                resolved_id = self._userref_to_order_id[userref]
                known = self._known_orders.get(resolved_id)
                if known is not None:
                    order_id = resolved_id
            if known is None:
                # Not one of ours (snapshot of historical fills, manual trade,
                # or an order that hasn't been register()'d yet due to a race).
                return None

            order_status = entry.get("order_status")

            # Fold trade/fill events into the running totals. Don't gate on
            # exec_type — it's purely labeling (observed "trade" in the v2
            # snapshot). Trust last_qty + last_price to detect a real fill.
            last_qty = entry.get("last_qty")
            last_price = entry.get("last_price")
            if isinstance(last_qty, (int, float)) and last_qty > 0:
                last_qty_f = float(last_qty)
                last_price_f = float(last_price) if isinstance(last_price, (int, float)) else 0.0
                cost_raw = entry.get("cost")
                cost_f = float(cost_raw) if isinstance(cost_raw, (int, float)) else (last_qty_f * last_price_f)
                fees = entry.get("fees") or []
                fee_delta = 0.0
                if isinstance(fees, list):
                    for fee in fees:
                        if isinstance(fee, dict):
                            q = fee.get("qty")
                            if isinstance(q, (int, float)):
                                fee_delta += float(q)
                known["vol_exec_running"] += last_qty_f
                known["cost_running"] += cost_f
                known["fee_running"] += fee_delta
                exec_id = entry.get("exec_id")
                if isinstance(exec_id, str) and exec_id:
                    known["exec_ids"].append(exec_id)

            # Only emit a terminal event once the order reaches a terminal
            # order_status. exec_type alone is not enough — a "trade" exec
            # can be interim on a partially-filled order still open.
            if order_status not in self._TERMINAL_STATUSES:
                return None

            vol_exec = known["vol_exec_running"]
            placed = known["placed_amount"]
            avg_price = (known["cost_running"] / vol_exec) if vol_exec > 0 else None

            if order_status == "filled":
                if _is_fully_filled(vol_exec, placed):
                    state = "FILLED"
                else:
                    state = "PARTIALLY_FILLED"
                terminal_reason: Optional[str] = None
            elif order_status in ("canceled", "expired"):
                reason = entry.get("reason") or order_status
                terminal_reason = str(reason)
                if vol_exec <= 0:
                    state = "CANCELLED_UNFILLED"
                else:
                    state = "PARTIALLY_FILLED"
            elif order_status == "rejected":
                state = "REJECTED"
                terminal_reason = str(entry.get("reason") or "rejected")
            else:
                return None  # unreachable given _TERMINAL_STATUSES guard

            term = {
                "order_id": known["order_id"],
                "journal_index": known["journal_index"],
                "engine_ref": known["engine_ref"],
                "pre_trade_snapshot": known["pre_trade_snapshot"],
                "placed_amount": placed,
                "pair": known["pair"],
                "side": known["side"],
                "state": state,
                "vol_exec": vol_exec,
                "avg_fill_price": avg_price,
                "fee_quote": known["fee_running"],
                "terminal_reason": terminal_reason,
                "exec_ids": list(known["exec_ids"]),
                "timestamp": entry.get("timestamp"),
            }

            # Drop from known maps — terminal means done.
            self._known_orders.pop(known["order_id"], None)
            uref = known.get("userref")
            if isinstance(uref, int):
                self._userref_to_order_id.pop(uref, None)
            return term


class FakeExecutionStream(ExecutionStream):
    """Test/harness double: identical interface, no subprocess, no thread.

    Tests push synthetic WS execution entries via `inject_event(...)` and
    then call `drain_events()` to collect terminal events. Used by the
    live harness in mock mode so scenario runs stay fast and hermetic."""

    def __init__(self):
        super().__init__(paper=False)
        # Override so healthy reports True without a subprocess
        self._fake_healthy = True
        self._last_heartbeat = time.monotonic()

    def start(self) -> bool:
        # No-op — tests drive events via inject_event.
        return True

    def stop(self) -> None:
        self._shutdown.set()

    @property
    def healthy(self) -> bool:
        return self._fake_healthy

    def health_status(self) -> Tuple[bool, str]:
        if self._fake_healthy:
            return True, ""
        return False, "fake stream marked unhealthy"

    def ensure_healthy(self) -> Tuple[bool, str]:
        # Tests are deterministic — never auto-restart, just report.
        return self.health_status()

    def set_healthy(self, value: bool) -> None:
        self._fake_healthy = value


class FakeTickerStream(TickerStream):
    """Test double for TickerStream — no subprocess, returns injected data."""

    def __init__(self, pairs, **kw):
        super().__init__(pairs=pairs, paper=True)
        self._healthy = True

    def start(self):
        return True

    def stop(self):
        pass

    @property
    def healthy(self):
        return self._healthy

    def health_status(self):
        return (self._healthy, "fake" if self._healthy else "fake_unhealthy")

    def ensure_healthy(self):
        # Match BaseStream contract: return (healthy, reason) tuple rather
        # than None. Tests are deterministic; never auto-restart.
        return self.health_status()

    def set_healthy(self, h):
        self._healthy = h

    def inject(self, pair, data):
        """Inject ticker data for a pair (bypasses WS symbol mapping)."""
        with self._lock:
            self._latest[pair] = data


