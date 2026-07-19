#!/usr/bin/env python3
"""
HYDRA Agent — Kraken CLI Integration Layer (Live Trading)

Connects the HYDRA engine to live Kraken market data via kraken-cli (WSL).
Trades BTC/USD, ETH/USD, and ZEC/USD by default (v2.29 — three independent
stable-quoted pairs, no triangle/coordinator; 90-day real-tape studies
showed no SOL edge and the SOL/BTC bridge was already proven dead).
Explicit SOL pairs remain fully supported via --pairs, and passing a
SOL/quote + SOL/BTC + BTC/quote trio re-activates the TradingTriangle
and CrossPairCoordinator unchanged.
Broadcasts state over WebSocket for the React dashboard.

Usage:
    python hydra_agent.py --demo --duration 30          # offline, no keys / no WSL
    python hydra_agent.py --pairs BTC/USD,ETH/USD --balance 100 --duration 600
    python hydra_agent.py --pairs BTC/USD,ETH/USD,ZEC/USD --interval 60
    python hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD --interval 60   # legacy SOL triangle
"""

import json
import time
import sys
import os
import random
import argparse
import signal as sig
import textwrap
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections import deque
from typing import Dict, List, Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 on stdout/stderr so non-ASCII glyphs in status prints (e.g. ∞)
# don't crash the tick loop under Windows cmd.exe's default cp1252 codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError) as e:
        import logging; logging.warning(f"Ignored exception: {e}")

# Load .env file if present (no dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                # Strip surrounding quotes (single or double)
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                    _v = _v[1:-1]
                if _v and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v

from hydra_engine import HydraEngine, CrossPairCoordinator, OrderBookAnalyzer, PositionSizer, SIZING_CONSERVATIVE, SIZING_COMPETITION
from hydra_tuner import ParameterTracker
from hydra_journal_migrator import migrate_legacy_trade_log_file

try:
    from hydra_brain import HydraBrain
    HAS_BRAIN = True
except ImportError:
    HAS_BRAIN = False

from hydra_kraken_cli import KrakenCLI
from hydra_pair_registry import STABLE_QUOTES
from hydra_config import TradingTriangle
from hydra_ws_server import DashboardBroadcaster
from hydra_streams import CandleStream, TickerStream, BalanceStream, BookStream, ExecutionStream, _is_fully_filled

# Kraken REST minimum interval (seconds) between distinct REST calls.
# Below this Kraken throttles or bans. Authoritative floor for the agent's
# order path; every REST hit (validate, place, cancel, query) must be spaced
# by at least this. See CLAUDE.md "2s REST floor" invariant.
KRAKEN_REST_FLOOR_S = 2.0

# Seed prices for offline --demo synthetic candles (approx market levels).
# Used only when demo=True; never for live/paper exchange paths.
_DEMO_SEED_PRICES: Dict[str, float] = {
    "BTC/USD": 65_000.0,
    "BTC/USDC": 65_000.0,
    "BTC/USDT": 65_000.0,
    "SOL/USD": 75.0,
    "SOL/USDC": 75.0,
    "SOL/USDT": 75.0,
    "SOL/BTC": 0.00116,
    "ETH/USD": 1_900.0,
    "ETH/USDC": 1_900.0,
    "ETH/USDT": 1_900.0,
    "ETH/BTC": 0.029,
    "ZEC/USD": 500.0,
}

# ═══════════════════════════════════════════════════════════════
# Regime-gated BUY limit offset
# ═══════════════════════════════════════════════════════════════
#
# Empirical analysis of 200 recent fills (15m candles, post-fill min-low
# over [t, t+1h]) showed that BUYs landed before the local low at
# materially different rates per (base, quote_class, regime):
#
#   pair       median 1h DD    %went_lower
#   BTC/USD    -0.33%          93%
#   SOL/BTC    -0.15%          80%
#   SOL/USD    -0.63%          100%   <-- structural early-fire
#
# The fix rests BUY limits below the live bid by a regime-gated offset,
# so the order waits for the dip rather than filling on first touch.
# RANGING and TREND_UP keep offset=0 to avoid missing fills in chop or
# rallies (the "caveat" — calibration sample was a downtrend window).
#
# Keys: (base_asset, quote_class, regime). quote_class collapses
# USD/USDC/USDT -> "STABLE" via STABLE_QUOTES; non-stable BTC quote
# stays "BTC". Missing key -> 0 bps (safe fallback).
_BUY_LIMIT_OFFSET_BPS: Dict[tuple, int] = {
    # PR-C / C5: offsets re-calibrated against post-only fill rate on
    # 15m tape (audit 2026-07-10). Prior 65–90 bps on SOL/STABLE dropped
    # realistic fill rate to ~0.5% while the engine still booked full size
    # at close — capital lock + false inventory. Cap offsets so post-only
    # fill probability remains material (~15–25 bps).
    # BTC on stable quote: NO offset (fills already at local floor).
    ("SOL", "BTC",    "VOLATILE"):    10,
    ("SOL", "BTC",    "TREND_DOWN"):  15,
    ("SOL", "STABLE", "VOLATILE"):    15,
    ("SOL", "STABLE", "TREND_DOWN"):  20,
}


def _buy_limit_offset_bps(pair: str, regime: Optional[str]) -> int:
    """Return basis points to drop below the live bid for a BUY limit.

    Returns 0 (no offset) when:
      - regime is RANGING or TREND_UP (avoid missing fills in chop/rallies)
      - pair / regime combination is not in the offset table
      - regime is None or unknown

    Disabled at runtime via env flag HYDRA_BUY_OFFSET_DISABLED=1.
    """
    if os.environ.get("HYDRA_BUY_OFFSET_DISABLED") == "1":
        return 0
    if not regime or "/" not in pair:
        return 0
    base, quote = pair.split("/", 1)
    base = base.upper()
    quote_class = "STABLE" if quote.upper() in STABLE_QUOTES else quote.upper()
    return _BUY_LIMIT_OFFSET_BPS.get((base, quote_class, regime), 0)


def _apply_buy_limit_offset(pair: str, bid: float, regime: Optional[str]) -> tuple:
    """Apply the regime-gated offset to a live bid.

    Returns (adjusted_price, bps_applied). adjusted_price is rounded to
    the pair's native price_decimals via KrakenCLI._format_price (which
    is also the formatter the order endpoint uses).
    """
    bps = _buy_limit_offset_bps(pair, regime)
    if bps <= 0 or bid <= 0:
        return bid, 0
    raw = bid * (1.0 - bps / 10000.0)
    # Round through the registry so we never produce a price the
    # exchange will reject for over-precision.
    formatted = KrakenCLI._format_price(pair, raw)
    return float(formatted), bps


# ═══════════════════════════════════════════════════════════════
# HYDRA AGENT (Main Loop)
# ═══════════════════════════════════════════════════════════════

class HydraAgent:
    """
    Main agent loop. Fetches live data from Kraken CLI, feeds it to the
    engine, executes real trades, and broadcasts state to the dashboard.
    """

    # Pair configuration. Pre-v2.19 these were hardcoded class constants
    # ("SOL/USDC", "BTC/USDC"); they're now derived per-instance from
    # the active TradingTriangle (`self.triangle.stable_sol`, .stable_btc,
    # .bridge) so a quote-currency change is a one-line config flip.
    ORDER_JOURNAL_CAP = 2000        # Bound in-memory order journal
    SNAPSHOT_EVERY_N_TICKS = 120    # ~10h at 300s ticks (also triggers immediately on journal writes)

    def __init__(
        self,
        pairs: List[str],
        initial_balance: float = 100.0,
        interval_seconds: int = 60,
        duration_seconds: int = 600,
        ws_port: int = 8765,
        mode: str = "conservative",
        paper: bool = False,
        candle_interval: int = 60,
        reset_params: bool = False,
        resume: bool = False,
        demo: bool = False,
    ):
        self.pairs = pairs
        # Derive the active TradingTriangle from the pair list. None when
        # the pair list doesn't form a complete triangle (e.g. tests that
        # spin up a single-pair agent for resume reconciliation). Code
        # paths that require role-based lookup must guard on `self.triangle`.
        self.triangle: Optional[TradingTriangle] = self._derive_triangle(pairs)
        self.initial_balance = initial_balance
        self._competition_start_balance = None  # Set once on first start, persisted across resumes
        # Portfolio-level drawdown (v2.16.2): per-pair engine.max_drawdown is a
        # pinned running max across tiny dips; it does not reflect exchange-wide
        # equity. Track here using total_usd from _compute_balance_usd so the
        # dashboard's "Max DD" widget is meaningful. Persisted across --resume.
        self._portfolio_peak_usd: float = 0.0
        self._portfolio_max_drawdown_pct: float = 0.0
        self._portfolio_current_drawdown_pct: float = 0.0
        # PR-B / B4: portfolio-level BUY halt when max DD ≥ 15%. SELL always
        # allowed (exit guarantee). Sticky for the session once tripped.
        self._portfolio_buy_halted: bool = False
        self.PORTFOLIO_CIRCUIT_BREAKER_PCT: float = 15.0
        self.interval = interval_seconds
        self.duration = duration_seconds
        self.mode = mode
        # Offline demo forces paper: synthetic market data, no WSL/Kraken/API keys.
        self.demo = bool(demo)
        self.paper = True if self.demo else paper
        self.candle_interval = candle_interval
        self.running = True
        self.start_time = None
        self.order_journal: List[Dict[str, Any]] = []
        self._snapshot_dir = os.path.dirname(os.path.abspath(__file__))
        self._completed_trades_since_update = 0  # Counter for tuner update cadence
        self._last_brain_candle_ts: Dict[str, float] = {}  # Per-pair: last candle timestamp brain evaluated
        # Demo RNG + last synthetic mid per pair (seeded once; deterministic enough for smoke).
        self._demo_rng = random.Random(42)
        self._demo_prices: Dict[str, float] = {}
        if self.demo:
            for p in pairs:
                self._demo_prices[p] = float(
                    _DEMO_SEED_PRICES.get(p) or _DEMO_SEED_PRICES.get(p.upper(), 100.0)
                )
        self._last_ai_decision: Dict[str, Dict] = {}         # Per-pair: last brain decision for dashboard persistence
        # Portfolio-level awareness
        self._current_portfolio_summary: Dict[str, Any] = {}  # Aggregate stats computed each tick
        self._portfolio_guidance: Optional[str] = None         # Latest Grok portfolio assessment text
        self._portfolio_candle_epoch: Dict[str, float] = {}    # Per-pair candle ts for epoch tracking
        self._portfolio_epoch_count: int = 0                   # Epochs since last portfolio review
        self._last_portfolio_review_regimes: Dict[str, str] = {}  # Regimes at last review
        # Monotonic client tag seeded from wall-clock to avoid collisions
        # across restarts; flows into Kraken as --userref and comes back on
        # the WS executions stream as order_userref for correlation.
        #
        # This initial time-seed is a floor — after snapshot load and journal
        # merge, _reseed_userref_from_history() raises it above anything we've
        # used in the past. Without that, a restart within the same second as
        # a killed session could collide with still-open orders' userrefs.
        self._userref_counter = int(time.time()) & 0x7FFFFFFF

        # v2.16.0: balance-history buffer for RM drawdown-velocity feature.
        # Bounded at 720 samples (12h @ 1/min). Not snapshot-persisted —
        # reconstitutes from live balance stream on restart; first 10 min
        # post-start feed drawdown_velocity None (insufficient window).
        self._balance_history: deque = deque(maxlen=720)

        # Sizing config based on mode
        sizing = SIZING_COMPETITION if mode == "competition" else SIZING_CONSERVATIVE

        # Self-tuning parameter trackers (one per pair)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.trackers: Dict[str, ParameterTracker] = {}
        for pair in pairs:
            tracker = ParameterTracker(pair=pair, save_dir=base_dir)
            if reset_params:
                tracker.reset()
                print(f"  [TUNER] Reset learned params for {pair}")
            self.trackers[pair] = tracker

        # One engine per pair — apply tuned params if available
        self.engines: Dict[str, HydraEngine] = {}
        for pair in pairs:
            # Volatility thresholds are now adaptive (multiplier on median
            # ATR%) — no candle-interval branching needed; the median
            # self-adjusts for wider candle bars.
            self.engines[pair] = HydraEngine(
                initial_balance=initial_balance / len(pairs),
                asset=pair,
                sizing=sizing,
                candle_interval=candle_interval,
            )
            # Apply any previously learned tuned params
            tuned = self.trackers[pair].get_tunable_params()
            self.engines[pair].apply_tuned_params(tuned)
            if self.trackers[pair].update_count > 0:
                print(f"  [TUNER] {pair}: loaded tuned params (update #{self.trackers[pair].update_count})")

        # Dashboard broadcaster
        self.broadcaster = DashboardBroadcaster(port=ws_port)

        # ─── Backtest subsystem (v2.10.0, Phase 6) ─────────────────────
        # Strictly additive. Kill-switchable via HYDRA_BACKTEST_DISABLED=1
        # (I6). Any failure inside init leaves the live agent completely
        # unaffected — we swallow + log, never raise.
        self.backtest_pool = None
        self.backtest_dispatcher = None
        # v2.27.6: exact "1" like other kill switches (was truthy-any).
        if os.environ.get("HYDRA_BACKTEST_DISABLED") != "1":
            try:
                from hydra_backtest_server import (
                    BacktestWorkerPool, mount_backtest_routes,
                )
                from hydra_backtest_tool import BacktestToolDispatcher
                from hydra_experiments import ExperimentStore
                bt_store = ExperimentStore()
                self.backtest_pool = BacktestWorkerPool(
                    max_workers=2,
                    store=bt_store,
                    broadcaster=self.broadcaster,
                )
                self.backtest_dispatcher = BacktestToolDispatcher(
                    store=bt_store, pool=self.backtest_pool,
                )
                mount_backtest_routes(
                    self.broadcaster, self.backtest_pool,
                    dispatcher=self.backtest_dispatcher,
                )
                print("  [BACKTEST] subsystem mounted (max_workers=2)")
            except Exception as e:
                print(f"  [BACKTEST] init failed ({type(e).__name__}: {e}); disabled for this run")
                self.backtest_pool = None
                self.backtest_dispatcher = None

        # AI Brain (optional — Claude for analysis, Grok for strategic depth)
        self.brain = None
        if HAS_BRAIN:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            xai_key = os.environ.get("XAI_API_KEY", "")
            if anthropic_key or openai_key or xai_key:
                try:
                    self.brain = HydraBrain(
                        anthropic_key=anthropic_key, openai_key=openai_key,
                        xai_key=xai_key,
                        tool_dispatcher=self.backtest_dispatcher,
                        # Gating stays env-driven (HYDRA_BRAIN_TOOLS_ENABLED=1)
                        # so brain tool-use is off by default even when the
                        # subsystem is mounted. Phase 12 flips the default.
                    )
                except Exception as e:
                    print(f"  [WARN] Brain init failed: {e}")

        # v2.14: Derivatives stream (Kraken Futures via kraken CLI, read-only).
        # Powers the Market Quant's QUANT INDICATORS block: funding, OI
        # regime, basis. SPOT-ONLY invariant — no orders placed on futures.
        # Kill switch: HYDRA_QUANT_INDICATORS_DISABLED=1. Failure is
        # silent — falls through to null indicators, Quant's R10 rule
        # handles stale-data force_hold. Offline --demo never starts it
        # (no WSL/kraken-cli required for first-run smoke).
        self.derivatives_stream = None
        if self.demo:
            print("  [QUANT] DerivativesStream skipped (offline --demo)")
        elif os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") != "1":
            try:
                from hydra_derivatives_stream import DerivativesStream
                self.derivatives_stream = DerivativesStream(pairs=list(self.pairs))
                self.derivatives_stream.start()
                print(f"  [QUANT] DerivativesStream started for {len(self.pairs)} pairs (signal input only — SPOT-ONLY execution)")
            except Exception as e:
                print(f"  [QUANT] DerivativesStream init failed ({type(e).__name__}: {e}); quant indicators disabled")
                self.derivatives_stream = None

        # ─── Companion subsystem (v2.10.3+) ────────────────────────────
        # Strictly additive. Off unless HYDRA_COMPANION_ENABLED=1.
        # Kill switch: HYDRA_COMPANION_DISABLED=1 wins over all.
        # Any init failure leaves the live agent completely unaffected.
        self.companion_coordinator = None
        try:
            from hydra_companions.config import is_enabled as _comp_enabled
            if _comp_enabled():
                from hydra_companions.coordinator import CompanionCoordinator
                from hydra_companions.ws_handlers import mount_companion_routes
                self.companion_coordinator = CompanionCoordinator(self)
                mount_companion_routes(self.broadcaster, self.companion_coordinator)
                print("  [COMPANION] subsystem mounted (Athena, Apex, Broski)")
        except Exception as e:
            print(f"  [COMPANION] init failed ({type(e).__name__}: {e}); disabled for this run")
            self.companion_coordinator = None

        # Cross-pair regime coordinator
        self.coordinator = CrossPairCoordinator(pairs)
        self._swap_counter = 0  # Monotonic swap ID generator

        # Execution stream — push-based reconciler backed by `kraken ws
        # executions`. Paper mode short-circuits the subprocess and uses
        # synthetic fill events (inject_event) so the same code path
        # handles both real and paper flows.
        self.execution_stream = ExecutionStream(paper=paper)
        # Push-based market data streams — candle + ticker. Each subscribes
        # to all pairs in one WS connection. Paper mode short-circuits to
        # no-op (REST fallback used instead).
        self.candle_stream = CandleStream(pairs, interval=candle_interval, paper=paper)
        self.ticker_stream = TickerStream(pairs, paper=paper)
        self.balance_stream = BalanceStream(paper=paper)
        self.book_stream = BookStream(pairs, depth=10, paper=paper)
        # v2.20.0 — Live tape capture: subscribe to CandleStream pushes and
        # write closed candles into the canonical hydra_history.sqlite store.
        # Bounded queue + writer thread guarantee the agent's main loop never
        # stalls on a SQLite fsync. Default ON; disable with HYDRA_TAPE_CAPTURE=0.
        # Wrapped in try/except so a SQLite lock / fs issue cannot crash
        # __init__ and silently kill the companion subsystem alongside.
        self._tape_store = None
        self._tape_capture = None
        # Offline --demo must not write synthetic bars into the operator's
        # hydra_history.sqlite (would pollute real tape / research data).
        if self.demo:
            print("  [TAPE] skipped (offline --demo; no history writes)")
        elif os.environ.get("HYDRA_TAPE_CAPTURE", "1") == "1":
            try:
                from hydra_history_store import HistoryStore
                from hydra_tape_capture import TapeCapture
                _tape_db = os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite")
                self._tape_store = HistoryStore(_tape_db)
                self._tape_capture = TapeCapture(self._tape_store)
                self.candle_stream.on_candle(self._tape_capture.on_candle)
                self._tape_capture.start()
            except Exception as e:
                print(f"  [TAPE] init failed ({type(e).__name__}: {e}); disabled for this run")
                self._tape_store = None
                self._tape_capture = None

        # Real-time chart feed: relay every CandleStream push (forming +
        # closed bars) to the dashboard as a lightweight `candle_update`
        # message. Without this, charts only refresh on the tick broadcast
        # (default 300s) even though the WS delivers updates continuously.
        # Runs inside the WS thread — must stay non-blocking; broadcast_message
        # is thread-safe and drops silently with no connected clients.
        self.candle_stream.on_candle(self._relay_candle_update)
        # Tracks the most recently logged unhealthy reason so the tick body
        # only prints on transitions instead of spamming the warning every
        # tick. None means "currently healthy or never warned".
        self._exec_stream_warned_reason: Optional[str] = None

        # Kraken system status — tracks last known status for transition logging.
        # None means "never checked". Only checked in live mode.
        self._last_kraken_status: Optional[str] = None

        # Fee tier cache — refreshed at most once per hour from `kraken volume`.
        # Shape: {"volume_30d_usd": float|None, "pair_fees": {pair: {"maker_pct","taker_pct"}}}
        self._fee_tier_cache: dict = {}
        self._fee_tier_fetched_at: float = 0.0

        # Track previous regime for cross-pair swap triggers
        self.prev_regimes: Dict[str, str] = {}

        # Sweep stale .tmp siblings of state files (left over from a crash
        # mid os.replace). The .replace is atomic so the main file is intact;
        # the .tmp is just orphan garbage and could mislead future debugging.
        try:
            import glob as _glob
            for stale in _glob.glob(os.path.join(self._snapshot_dir, "*.json.tmp")):
                try:
                    os.remove(stale)
                    print(f"  [STARTUP] removed stale tmp: {os.path.basename(stale)}")
                except OSError as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

        # Run the one-shot legacy trade_log -> order_journal migration
        # before touching any on-disk state. Idempotent; no-op after the
        # first run. Lives in hydra_journal_migrator so it can be invoked
        # standalone as well.
        try:
            migrate_legacy_trade_log_file(self._snapshot_dir, verbose=False)
        except Exception as e:
            print(f"  [MIGRATE] legacy journal migration skipped: {e}")

        # Restore from snapshot if requested (never in offline --demo).
        if resume and not self.demo:
            self._load_snapshot()

        # Merge the on-disk rolling journal into self.order_journal regardless
        # of --resume. The snapshot only holds the last 200 entries; the
        # rolling file is the long-horizon record. Prior versions would
        # overwrite the rolling file on the first tick after restart,
        # truncating history — this merges it in first so restarts preserve
        # full depth (bounded by ORDER_JOURNAL_CAP).
        # Offline --demo stays session-only so a first-run smoke never mixes
        # with the operator's live/paper journal or pollutes it on disk.
        if self.demo:
            print("  [DEMO] session-only journal (no merge/persist of operator state)")
        else:
            self._merge_order_journal()

        # Reseed _userref_counter above anything we've used historically.
        # Must run AFTER both _load_snapshot (may carry a persisted counter)
        # AND _merge_order_journal (gives us the historical high-water mark).
        if not self.demo:
            self._reseed_userref_from_history()

        # Graceful shutdown
        sig.signal(sig.SIGINT, self._handle_shutdown)
        sig.signal(sig.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        print("\n\n  [HYDRA] Shutdown signal received. Cancelling orders, flushing snapshot...\n")
        self.running = False
        # Cancel all resting limit orders on the exchange (live mode only)
        if not self.paper:
            try:
                result = KrakenCLI.cancel_all()
                if "error" in result:
                    print(f"  [HYDRA] Cancel-all error: {result['error']}")
                else:
                    print("  [HYDRA] All open orders cancelled.")
            except Exception as e:
                print(f"  [HYDRA] Cancel-all failed: {e}")
        # Tear down all WS stream subprocesses
        for stream, label in [
            (self.execution_stream, "ExecutionStream"),
            (self.candle_stream, "CandleStream"),
            (self.ticker_stream, "TickerStream"),
            (self.balance_stream, "BalanceStream"),
            (self.book_stream, "BookStream"),
        ]:
            try:
                stream.stop()
            except Exception as e:
                print(f"  [HYDRA] {label} stop failed: {e}")
        # v2.14: shut down the Kraken Futures derivatives poller. Daemon
        # thread so process exit would kill it, but signal _stop cleanly
        # so a lingering kraken subprocess doesn't outlive us.
        if self.derivatives_stream is not None:
            try:
                self.derivatives_stream.stop()
            except Exception as e:
                print(f"  [HYDRA] DerivativesStream stop failed: {e}")
        # v2.20.0 — Stop tape capture last (after streams are torn down so no
        # more candles arrive). Drain the queue cleanly.
        if getattr(self, "_tape_capture", None) is not None:
            try:
                self._tape_capture.stop()
            except Exception as e:
                print(f"  [HYDRA] TapeCapture stop failed: {e}")
        # Drain the backtest worker pool (daemon threads — best-effort join).
        if self.backtest_pool is not None:
            try:
                self.backtest_pool.shutdown(timeout=3.0)
            except Exception as e:
                print(f"  [HYDRA] Backtest pool shutdown failed: {e}")
        # Stop the dashboard WebSocket server so port 8765 is released
        # before process exit — prevents EADDRINUSE on rapid --resume.
        if self.broadcaster is not None:
            try:
                self.broadcaster.stop()
            except Exception as e:
                print(f"  [HYDRA] Broadcaster stop failed: {e}")
        # Flush session snapshot for --resume
        try:
            self._save_snapshot()
        except Exception as e:
            print(f"  [HYDRA] Snapshot flush failed: {e}")

    # ─── RM features: balance-history sample ──────────────────────────────

    def _record_balance_sample(self, ts: float, balance: float) -> None:
        """Append one balance sample to the RM-features buffer. Called once
        per tick from the main loop; maxlen-bounded so no trimming needed."""
        if balance is not None and balance >= 0:
            self._balance_history.append((ts, float(balance)))

    # ─── v2.16.0: quant_indicators assembly + RM engine-internal features ──

    @staticmethod
    def _engine_candles_as_dicts(engine: Optional[object]) -> List[Dict[str, float]]:
        """Normalize engine candle buffer into [{ts, close}, ...] form.

        Real HydraEngine exposes `engine.candles` as a list of Candle
        dataclasses (with `timestamp`/`close`). Tests may provide a mock
        with `engine.get_candles()` returning dicts directly. Either form
        is accepted; unknown shapes return []."""
        if engine is None:
            return []
        # Prefer get_candles() if present (mocks/future API)
        if hasattr(engine, "get_candles"):
            try:
                raw = engine.get_candles() or []
                # If already list of dicts, pass through
                if raw and isinstance(raw[0], dict):
                    return list(raw)
                # Otherwise treat as list of Candle-like objects
                return [
                    {"ts": float(getattr(c, "timestamp", getattr(c, "ts", 0))),
                     "close": float(getattr(c, "close"))}
                    for c in raw
                ]
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
        # Fall back to the real engine attribute
        try:
            raw = getattr(engine, "candles", []) or []
            return [
                {"ts": float(getattr(c, "timestamp", getattr(c, "ts", 0))),
                 "close": float(getattr(c, "close"))}
                for c in raw
            ]
        except Exception:
            return []

    def _relay_candle_update(self, pair: str, entry: dict) -> None:
        """CandleStream push → dashboard `candle_update` message.

        Mirrors the tick loop's WS→engine candle conversion (interval_begin
        ISO → epoch) so the dashboard can merge updates into the same
        timeline the state broadcast carries. Never raises — a chart relay
        must not be able to hurt the stream thread.
        """
        try:
            broadcaster = getattr(self, "broadcaster", None)
            if broadcaster is None:
                return
            ts_raw = entry.get("interval_begin") or entry.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = time.time()
            elif isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            else:
                ts = time.time()
            broadcaster.broadcast_message("candle_update", {
                "pair": pair,
                "candle": {
                    "o": float(entry.get("open") or 0.0),
                    "h": float(entry.get("high") or 0.0),
                    "l": float(entry.get("low") or 0.0),
                    "c": float(entry.get("close") or 0.0),
                    "t": ts,
                },
            })
        except Exception:
            pass  # chart relay is best-effort by design

    def _build_quant_indicators(self, pair: str, state: Dict) -> None:
        """Assemble the quant_indicators dict for one pair. Mutates state.

        Combines DerivativesStream snapshot + engine CVD divergence
        (pre-v2.16 fields) with the six v2.16.0 RM engine-internal
        features. The kill switch HYDRA_RM_FEATURES_DISABLED=1 is read
        on every call so it can be flipped live without restart."""
        quant_indicators: Dict[str, Any] = {}
        # Structural coverage: is this pair in the Kraken Futures map at all?
        # (Not "snapshot present" — a covered pair with a warming-up stream
        # must still hit R10's staleness blackout, fail-safe.)
        derivatives_covered = (
            self.derivatives_stream is not None
            and pair in getattr(self.derivatives_stream, "pairs", [])
        )
        if self.derivatives_stream is not None:
            try:
                snap = self.derivatives_stream.latest(pair)
                if snap is not None:
                    quant_indicators = {
                        "funding_bps_8h": snap.funding_bps_8h,
                        "funding_predicted_bps": snap.funding_predicted_bps,
                        "oi_delta_1h_pct": snap.oi_delta_1h_pct,
                        "oi_delta_24h_pct": snap.oi_delta_24h_pct,
                        "oi_price_regime": snap.oi_price_regime,
                        "basis_apr_pct": snap.basis_apr_pct,
                        "staleness_s": round(snap.staleness_s, 1) if snap.staleness_s != float("inf") else None,
                        "synthetic_pair": snap.synthetic,
                        "basis_available": getattr(snap, "basis_available", True),
                    }
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
        if not derivatives_covered:
            # Portfolio satellites (no Kraken Futures mapping) declare
            # themselves so R10 tracks only the fields that can exist —
            # absent funding/OI keys otherwise read as "stale" and the
            # pair would be structurally force-held every tick.
            quant_indicators["derivatives_covered"] = False

        engine = self.engines.get(pair)
        if engine is not None:
            try:
                quant_indicators["cvd_divergence_sigma"] = engine.cvd_divergence_sigma()
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        # v2.16.0: RM engine-internal features. Kill switch read live.
        if os.environ.get("HYDRA_RM_FEATURES_DISABLED") != "1":
            self._add_rm_features(pair, engine, quant_indicators)

        if quant_indicators:
            state["quant_indicators"] = quant_indicators

    def _add_rm_features(
        self, pair: str, engine: Optional[object], qi: Dict[str, Any],
    ) -> None:
        """Compute and attach the six engine-internal RM features. Every
        call site is defensively guarded: a single broken feature cannot
        take down the indicator block."""
        from hydra_rm_features import (
            realized_vol_pct,
            drawdown_velocity_pct_per_hr,
            fill_rate_24h,
            avg_slippage_bps_24h,
            minutes_since_last_trade,
        )
        now = time.time()
        candles = self._engine_candles_as_dicts(engine)

        try:
            qi["realized_vol_1h_pct"] = realized_vol_pct(candles, window_minutes=60)
            qi["realized_vol_24h_pct"] = realized_vol_pct(candles, window_minutes=1440)
        except Exception:
            qi["realized_vol_1h_pct"] = None
            qi["realized_vol_24h_pct"] = None

        try:
            qi["drawdown_velocity_pct_per_hr"] = drawdown_velocity_pct_per_hr(
                list(self._balance_history), now=now,
            )
        except Exception:
            qi["drawdown_velocity_pct_per_hr"] = None

        journal = list(self.order_journal) if hasattr(self, "order_journal") else []
        try:
            qi["fill_rate_24h"] = fill_rate_24h(journal, now=now)
        except Exception:
            qi["fill_rate_24h"] = None
        try:
            qi["avg_slippage_bps_24h"] = avg_slippage_bps_24h(journal, now=now)
        except Exception:
            qi["avg_slippage_bps_24h"] = None
        try:
            qi["minutes_since_last_trade"] = minutes_since_last_trade(journal, now=now)
        except Exception:
            qi["minutes_since_last_trade"] = None

        # Cross-pair correlation: stable_sol <-> stable_btc, symmetric on both pairs.
        try:
            qi["cross_pair_corr_24h"] = self._compute_cross_pair_corr_24h()
        except Exception:
            qi["cross_pair_corr_24h"] = None

    def _compute_cross_pair_corr_24h(self) -> Optional[float]:
        """Derive 15m-candle log-returns for stable_btc and stable_sol, align
        by index, compute Pearson. Returns None if either engine missing
        or insufficient samples (also when self.triangle is None — partial
        triangles can't compute cross-pair stats)."""
        import math
        from hydra_rm_features import cross_pair_corr
        triangle = getattr(self, "triangle", None)
        if triangle is None:
            return None
        btc = self.engines.get(triangle.stable_btc.cli_format)
        sol = self.engines.get(triangle.stable_sol.cli_format)
        if btc is None or sol is None:
            return None
        btc_c = self._engine_candles_as_dicts(btc)
        sol_c = self._engine_candles_as_dicts(sol)
        # Use last 97 candles (15m * 97 ~ 24h) -> 96 returns
        n = min(len(btc_c), len(sol_c), 97)
        if n < 31:
            return None
        btc_tail = btc_c[-n:]
        sol_tail = sol_c[-n:]
        btc_ret, sol_ret = [], []
        for i in range(1, n):
            try:
                b0, b1 = float(btc_tail[i - 1]["close"]), float(btc_tail[i]["close"])
                s0, s1 = float(sol_tail[i - 1]["close"]), float(sol_tail[i]["close"])
                if b0 > 0 and s0 > 0:
                    btc_ret.append(math.log(b1 / b0))
                    sol_ret.append(math.log(s1 / s0))
            except (KeyError, TypeError, ValueError):
                continue
        return cross_pair_corr(btc_ret, sol_ret)

    # ─── Session snapshot (atomic JSON; resumable across runs) ─────────────

    @staticmethod
    def _should_block_buy_for_portfolio_dd(
        portfolio_buy_halted: bool, action: str,
    ) -> bool:
        """PR-B / B4: portfolio circuit breaker blocks BUY only."""
        return bool(portfolio_buy_halted) and str(action).upper() == "BUY"

    def _snapshot_path(self) -> str:
        return os.path.join(self._snapshot_dir, "hydra_session_snapshot.json")

    def _journal_for_persistence(self) -> List[Dict[str, Any]]:
        """Return the journal slice to persist to disk. Excludes
        PLACEMENT_FAILED entries — those are pre-exchange diagnostics
        useful in-session for live debugging, but they re-pollute the
        dashboard on restart if persisted. The in-memory
        self.order_journal still contains them for the current session.

        Caps at the most recent 200 non-failed entries (matches the prior
        [-200:] cap on the unfiltered list)."""
        # Strip in-memory-only blobs that are large / redundant on disk.
        # pre_trade_snapshot is kept in-memory for same-session cancel/true-up;
        # on resume, CANCELLED_UNFILLED without snapshot still warns (pre-PR-C).
        # We DO persist a compact snapshot key when present so resume rollback
        # can restore engine books after crash mid-PLACED.
        out: List[Dict[str, Any]] = []
        for e in self.order_journal:
            if e.get("lifecycle", {}).get("state") == "PLACEMENT_FAILED":
                continue
            out.append(e)
        return out[-200:]

    def _save_snapshot(self):
        """Atomically save session state to disk (.tmp -> os.replace).

        Offline --demo is a no-op: never overwrite the operator's
        hydra_session_snapshot.json with synthetic state.
        """
        # getattr: tests may construct via object.__new__ without __init__
        if getattr(self, "demo", False):
            return
        snapshot = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "paper": self.paper,
            "pairs": self.pairs,
            # Indicator semantics are interval-dependent; --resume compares
            # this against the live setting and drops candle history on
            # mismatch rather than mixing timeframes silently. getattr:
            # object.__new__ test constructs may lack the attribute.
            "candle_interval": getattr(self, "candle_interval", None),
            "competition_start_balance": self._competition_start_balance,
            "engines": {pair: eng.snapshot_runtime() for pair, eng in self.engines.items()},
            "coordinator_regime_history": self.coordinator.regime_history,
            "order_journal": self._journal_for_persistence(),
            # Persist the userref counter so a restart never re-issues a
            # userref already in-flight on the exchange from this session.
            "userref_counter": self._userref_counter,
            "portfolio_drawdown": {
                "peak_usd": getattr(self, "_portfolio_peak_usd", 0.0),
                "max_pct": getattr(self, "_portfolio_max_drawdown_pct", 0.0),
            },
            # v2.18.0: persist DerivativesStream OI + mark-price history
            # so `oi_delta_1h_pct` / `oi_price_regime` are live within
            # one poll cycle on `--resume` rather than after a fresh
            # 1 H warmup. Fail-soft on load (stale gate + missing key).
            # getattr guards tests that instantiate via object.__new__
            # (same getattr/object.__new__ guard pattern).
            "derivatives_history": (
                getattr(self, "derivatives_stream", None).snapshot()
                if getattr(self, "derivatives_stream", None) else {}
            ),
        }
        path = self._snapshot_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f, default=str)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [SNAPSHOT] Save failed: {e}")

    @staticmethod
    def _detect_snapshot_stable_quote(snapshot: dict) -> Optional[str]:
        """Detect the stable quote a persisted snapshot was written under.

        Scans `pairs` for the first stable-quoted entry; returns the
        uppercased quote ("USDC", "USD", "USDT") or None when the
        snapshot has no stable-quoted pairs (e.g. a SOL/BTC-only
        backtest). Used by `_load_snapshot` to decide whether to
        invoke the state migrator.
        """
        pairs = snapshot.get("pairs") or []
        if not isinstance(pairs, list):
            return None
        for p in pairs:
            if not isinstance(p, str) or "/" not in p:
                continue
            quote = p.split("/", 1)[1].strip().upper()
            if quote in STABLE_QUOTES:
                return quote
        return None

    @staticmethod
    def _derive_triangle(pairs: List[str]) -> Optional[TradingTriangle]:
        """Best-effort triangle derivation from a pair-symbol list.

        Used internally by `__init__` so role-based lookups
        (`self.triangle.stable_sol`, etc.) work without the caller having
        to construct a TradingTriangle explicitly. Returns None when the
        list doesn't contain a complete triangle (tests with a single
        pair, partial-triangle backtests). Callers must guard on None.
        """
        sol_stable = None
        btc_stable = None
        bridge = None
        for sym in pairs:
            p = KrakenCLI.registry.get(sym)
            if p is None:
                continue
            if p.base == "SOL" and p.is_stable_quoted:
                sol_stable = p
            elif p.base == "BTC" and p.is_stable_quoted:
                btc_stable = p
            elif p.base == "SOL" and p.quote == "BTC":
                bridge = p
        if (sol_stable is not None and btc_stable is not None
                and bridge is not None
                and sol_stable.quote == btc_stable.quote):
            try:
                return TradingTriangle(
                    stable_sol=sol_stable,
                    stable_btc=btc_stable,
                    bridge=bridge,
                    quote=sol_stable.quote,
                )
            except Exception:
                return None
        return None

    @staticmethod
    def _normalize_pair_name(pair: str) -> str:
        """Normalize legacy XBT pair names to BTC canonical form.

        Handles snapshot/journal data written before the XBT→BTC migration.
        Delegates to the PairRegistry, which knows every alias dialect.
        """
        if not pair:
            return pair
        p = KrakenCLI.registry.get(pair)
        return p.cli_format if p else pair

    @staticmethod
    def _normalize_journal_pairs(journal: list):
        """Normalize pair names in journal entries from XBT to BTC canonical."""
        for entry in journal:
            if isinstance(entry, dict) and "pair" in entry:
                entry["pair"] = HydraAgent._normalize_pair_name(entry["pair"])

    def _load_snapshot(self):
        """Restore engine + coordinator state from snapshot file."""
        path = self._snapshot_path()
        if not os.path.exists(path):
            print("  [SNAPSHOT] No snapshot file found, starting fresh.")
            return
        try:
            with open(path, "r") as f:
                snapshot = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"  [SNAPSHOT] Load failed for {path}: {type(e).__name__}: {e} — starting fresh.")
            return
        try:
            if snapshot.get("version") != 1:
                print(f"  [SNAPSHOT] Unknown version {snapshot.get('version')}, skipping.")
                return
            # v2.19: quote-currency migration. If the snapshot's recorded
            # pairs use a different stable quote than the active triangle
            # (e.g. USDC snapshot, USD-default agent), rewrite the pair-
            # keyed fields so engine state, regime history, and OI deques
            # are preserved across the quote flip.
            #
            # Pattern: migrate a deep copy, persist the copy, only
            # rebind `snapshot` once the disk write succeeded. If
            # persist fails we still proceed with the migrated copy in
            # memory (otherwise the engines wouldn't restore — their
            # keys are already on the active-quote side); but the log
            # makes the desync window explicit so the operator sees it.
            # The next regular `_save_snapshot` tick reconciles disk
            # from `self.engines`, so the window is at most one save
            # cycle.
            triangle = getattr(self, "triangle", None)
            if triangle is not None:
                target_quote = triangle.quote
                source_quote = self._detect_snapshot_stable_quote(snapshot)
                if source_quote and source_quote != target_quote:
                    import copy as _copy
                    from hydra_state_migrator import migrate_snapshot
                    candidate = _copy.deepcopy(snapshot)
                    migrate_snapshot(
                        candidate,
                        source_quote=source_quote,
                        target_quote=target_quote,
                    )
                    persist_ok = False
                    try:
                        tmp = path + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(candidate, f, default=str)
                        os.replace(tmp, path)
                        persist_ok = True
                    except OSError as e:
                        print(f"  [SNAPSHOT] Post-migration write failed: {e}; "
                              f"in-memory state will be used this session "
                              f"and disk will reconcile on next save tick.")
                    snapshot = candidate
                    if persist_ok:
                        print(f"  [SNAPSHOT] Migrated pair keys "
                              f"{source_quote} → {target_quote} "
                              f"(engine state preserved).")
            # Timeframe guard: candle history recorded at a different
            # interval must not seed this session's indicators (a 15m RSI
            # is not a 60m RSI). Positions/balances/journal still restore;
            # candles re-warm from the REST OHLC seed at the live interval.
            snap_interval = snapshot.get("candle_interval")
            drop_candles = (
                snap_interval is not None
                and int(snap_interval) != int(self.candle_interval)
            )
            if drop_candles:
                print(f"  [SNAPSHOT] candle_interval changed "
                      f"({snap_interval}m → {self.candle_interval}m) — "
                      f"dropping snapshot candle history; positions and "
                      f"journal restore normally.")
            # Normalize legacy XBT pair names in engine keys
            engines_raw = snapshot.get("engines", {})
            engines = {self._normalize_pair_name(k): v for k, v in engines_raw.items()}
            for pair, eng_snap in engines.items():
                if pair in self.engines:
                    if drop_candles:
                        eng_snap = dict(eng_snap)
                        eng_snap.pop("candles", None)
                        eng_snap.pop("prices", None)
                    self.engines[pair].restore_runtime(eng_snap)
            # Normalize coordinator regime history keys
            coord_raw = snapshot.get("coordinator_regime_history", {})
            for pair, history in coord_raw.items():
                norm_pair = self._normalize_pair_name(pair)
                if norm_pair in self.coordinator.regime_history:
                    self.coordinator.regime_history[norm_pair] = list(history)
            self.order_journal = list(snapshot.get("order_journal", []))
            self._normalize_journal_pairs(self.order_journal)
            if snapshot.get("competition_start_balance") is not None:
                self._competition_start_balance = float(snapshot["competition_start_balance"])
            # Restore portfolio-level drawdown so weeks-long peak is preserved
            # across --resume. Fresh start (no key) leaves the zeros set in __init__.
            pdd = snapshot.get("portfolio_drawdown") or {}
            try:
                self._portfolio_peak_usd = float(pdd.get("peak_usd", 0.0))
                self._portfolio_max_drawdown_pct = float(pdd.get("max_pct", 0.0))
                # Re-arm sticky portfolio BUY halt if session already breached 15%.
                # getattr: tests may construct via __new__ without __init__.
                cb = float(getattr(self, "PORTFOLIO_CIRCUIT_BREAKER_PCT", 15.0))
                if self._portfolio_max_drawdown_pct >= cb:
                    self._portfolio_buy_halted = True
            except (TypeError, ValueError):
                self._portfolio_peak_usd = 0.0
                self._portfolio_max_drawdown_pct = 0.0
                self._portfolio_buy_halted = False
            # Carry the persisted userref floor into _userref_counter. The
            # _reseed_userref_from_history() call in __init__ will raise it
            # further if the journal reveals higher values.
            persisted_uref = snapshot.get("userref_counter")
            if isinstance(persisted_uref, int) and 0 < persisted_uref < (1 << 31):
                self._userref_counter = max(self._userref_counter, persisted_uref)
            # v2.18.0: Rehydrate DerivativesStream OI + price history.
            # Stream already started earlier in __init__; restore is
            # lock-protected so a poll racing with this call is safe.
            # Stale-gate drops history when downtime exceeded
            # MAX_RESTORE_GAP_S so `_delta_pct` can still return None
            # rather than against a misleading baseline.
            deriv_attr = getattr(self, "derivatives_stream", None)
            if deriv_attr is not None:
                try:
                    deriv_attr.restore(snapshot.get("derivatives_history") or {})
                except Exception as e:
                    print(f"  [SNAPSHOT] derivatives_history restore skipped: "
                          f"{type(e).__name__}: {e}")
            print(f"  [SNAPSHOT] Restored session from {snapshot.get('timestamp', '?')}")
        except Exception as e:
            print(f"  [SNAPSHOT] Restore failed for {path}: {type(e).__name__}: {e} — starting fresh.")

    def _merge_order_journal(self):
        """Merge on-disk journal files into self.order_journal.

        Sources (in order):
          1. hydra_order_journal.json — rolling file, authoritative long-
             horizon record.  _save_snapshot caps at [-200:] so the
             rolling file preserves depth across restarts.
          2. hydra_order_journal_backfill.json — optional one-shot file
             for manual trades placed outside Hydra.  Consumed and
             deleted after merge so entries are ingested exactly once.

        Dedup key is (placed_at, order_id) when a Kraken order_id is
        available, else (placed_at, pair, side, intent.amount) — precise
        enough because placed_at has microsecond resolution.

        Conflict policy: on duplicate key, the on-disk file wins.
        After the merge, the next _save_snapshot rewrites the snapshot
        to match.
        """
        def _key(entry):
            t = entry.get("placed_at", "")
            ref = entry.get("order_ref") or {}
            order_id = ref.get("order_id") if isinstance(ref, dict) else None
            if order_id:
                return (t, order_id)
            intent = entry.get("intent") or {}
            return (t, entry.get("pair", ""), entry.get("side", ""),
                    intent.get("amount", 0) if isinstance(intent, dict) else 0)

        seen = {_key(e): e for e in self.order_journal}
        merged_count = 0
        overwritten_count = 0

        # Merge from rolling journal + optional backfill file (manual trades).
        # Backfill file is consumed once and deleted after successful merge.
        rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")
        backfill_file = os.path.join(self._snapshot_dir, "hydra_order_journal_backfill.json")
        backfill_consumed = False

        for filepath in (rolling_file, backfill_file):
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r") as f:
                    on_disk = json.load(f)
            except Exception as e:
                print(f"  [JOURNAL] Could not read {os.path.basename(filepath)} for merge: {e}")
                continue
            if not isinstance(on_disk, list):
                continue
            for e in on_disk:
                k = _key(e)
                if k not in seen:
                    seen[k] = e
                    merged_count += 1
                else:
                    # On-disk file wins on conflict — see docstring.
                    if seen[k] is not e:
                        seen[k] = e
                        overwritten_count += 1
            if filepath == backfill_file:
                backfill_consumed = True

        merged = sorted(seen.values(), key=lambda e: e.get("placed_at", ""))
        if len(merged) > self.ORDER_JOURNAL_CAP:
            merged = merged[-self.ORDER_JOURNAL_CAP:]
        self.order_journal = merged
        self._normalize_journal_pairs(self.order_journal)

        if backfill_consumed:
            try:
                os.remove(backfill_file)
                print(f"  [JOURNAL] Consumed and removed backfill file")
            except OSError as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        if merged_count or overwritten_count:
            parts = []
            if merged_count:
                parts.append(f"merged {merged_count} new")
            if overwritten_count:
                parts.append(f"overwrote {overwritten_count} stale")
            print(f"  [JOURNAL] {' + '.join(parts)}; "
                  f"total = {len(self.order_journal)}")

    def run(self):
        """Main agent loop."""
        self.start_time = time.time()
        self._print_banner()

        # Start WebSocket server for dashboard
        self.broadcaster.start()
        time.sleep(0.5)

        # Set dead man's switch (live mode only) — timeout must exceed tick interval
        self._dms_timeout = max(60, self.interval + 30)
        if not self.paper:
            print(f"  [HYDRA] Setting dead man's switch ({self._dms_timeout}s)...")
            result = KrakenCLI.cancel_after(self._dms_timeout)
            if "error" not in result:
                print("  [HYDRA] Dead man's switch active")
            else:
                print(f"  [WARN] Dead man's switch: {result.get('error', 'unknown')}")

        # Load dynamic pair constants from Kraken (overlays the PairRegistry).
        # Hardcoded constants remain as fallbacks for any pair not returned.
        if not self.paper:
            print("\n  [HYDRA] Loading pair constants from Kraken...")
            pair_constants = KrakenCLI.load_pair_constants(self.pairs)
            if pair_constants:
                KrakenCLI.apply_pair_constants(pair_constants)
                for pair in self.pairs:
                    self.engines[pair].sizer.apply_pair_limits(pair_constants)
                loaded_pairs = ", ".join(
                    f"{p}(dec={pair_constants[p]['price_decimals']},min={pair_constants[p]['ordermin']})"
                    for p in pair_constants
                )
                print(f"  [HYDRA] Pair constants loaded: {loaded_pairs}")
            else:
                print("  [WARN] Pair constants unavailable — using hardcoded fallbacks")
            time.sleep(2)  # Rate limit

        # Warmup: historical candles (needed before balance conversion / first tick)
        balances_converted = False
        if self.demo:
            print("\n  [HYDRA] Warming up with synthetic candles (offline --demo)...")
            self._warmup_demo_candles(n=80)
            print(f"  [HYDRA] Demo balance: ${self.initial_balance:,.2f} (no exchange)")
        else:
            print("\n  [HYDRA] Warming up with historical candles...")
            for pair in self.pairs:
                # Daily seed FIRST (trend overlay: ~720 daily bars warm the
                # sma200 immediately), then intraday bars refine today's
                # close. Fail-soft: without the seed the overlay reports
                # None and the rails run exactly as pre-overlay.
                daily = KrakenCLI.ohlc(pair, interval=1440)
                if daily:
                    self.engines[pair].seed_daily_closes(daily)
                    score = self.engines[pair].daily_trend_score()
                    print(f"  [HYDRA] {pair}: {len(daily)} daily bars seeded "
                          f"(trend score: {score if score is not None else 'warming'})")
                time.sleep(2)  # Respect rate limits
                candles = KrakenCLI.ohlc(pair, interval=self.candle_interval)
                if candles:
                    for c in candles[-200:]:
                        self.engines[pair].ingest_candle(c)
                    price = candles[-1]["close"]
                    print(f"  [HYDRA] {pair}: {min(len(candles), 200)} candles loaded, last price: ${price:,.4f}")
                else:
                    print(f"  [WARN] {pair}: no historical data")
                time.sleep(2)  # Respect rate limits

            # Fetch live account balance and initialize engines from real funds
            print("\n  [HYDRA] Checking account balance...")
            bal = KrakenCLI.balance()
            if "error" not in bal:
                for asset, amount in bal.items():
                    print(f"  [HYDRA]   {asset}: {amount}")

                # Cache BEFORE _set_engine_balances so v2.11.0's live-path
                # `tradable` flag initialization can read real BTC/quote holdings.
                # Prior ordering marked every non-USD pair info-only at startup
                # until the first tick's _refresh_tradable_flags() self-corrected.
                self._cached_balance = bal

                if not self.paper:
                    # Compute tradable USD balance (excludes staked/bonded assets)
                    breakdown = self._compute_balance_usd(bal)
                    tradable = breakdown["tradable_usd"]
                    staked = breakdown["staked_usd"]
                    total = breakdown["total_usd"]
                    print(f"  [HYDRA] Portfolio: ${total:,.2f} total | ${tradable:,.2f} tradable | ${staked:,.2f} staked")

                    if tradable > 0:
                        per_pair_usd = tradable / len(self.pairs)
                        self._set_engine_balances(per_pair_usd)
                        balances_converted = True
                        # Exchange cash is already net of fill fees. Resume
                        # reconcile must mark fee_applied without re-debiting.
                        self._balances_from_exchange = True
                        self.initial_balance = tradable
                        # Lock in competition starting balance on first start only —
                        # on --resume, preserve the original so cumulative P&L is correct.
                        if self._competition_start_balance is None:
                            self._competition_start_balance = tradable
                        print(f"  [HYDRA] Engine balance set from exchange: ${per_pair_usd:,.2f} per pair")
                    else:
                        print(f"  [WARN] No tradable balance — using --balance fallback: ${self.initial_balance:,.2f}")
            else:
                print(f"  [WARN] Balance check failed: {bal} — using --balance fallback: ${self.initial_balance:,.2f}")

        # Convert engine balances from USD to quote currency for non-USD pairs
        # (e.g. SOL/BTC engine needs balance in BTC, not USD).
        # Skip if _set_engine_balances was already called above (live mode with
        # exchange data).  Resumed sessions still need conversion because old
        # snapshots (pre-multi-currency fix) stored USD values for BTC-quoted pairs.
        if not balances_converted:
            per_pair_usd = self.initial_balance / len(self.pairs)
            self._set_engine_balances(per_pair_usd)

        # Ensure competition start balance is set (fallback/paper path)
        if self._competition_start_balance is None:
            self._competition_start_balance = self.initial_balance

        # Start the execution stream (kraken ws executions subprocess +
        # background reader). Paper mode no-ops. Failure leaves healthy=False
        # which we surface each tick; placement still works, lifecycle
        # finalization just won't happen until the stream recovers.
        if not self.execution_stream.start():
            print("  [WARN] ExecutionStream failed to start — placements will not auto-finalize")

        # Start push-based market data streams (candle + ticker).
        # Failure is non-fatal — _fetch_and_tick falls back to REST.
        if not self.paper:
            if not self.candle_stream.start():
                print("  [WARN] CandleStream failed to start — falling back to REST ohlc")
            if not self.ticker_stream.start():
                print("  [WARN] TickerStream failed to start — falling back to REST ticker")
            
            time.sleep(1.5)  # Rate limit: allow execution_stream token request to settle
            if not self.balance_stream.start():
                print("  [WARN] BalanceStream failed to start — falling back to REST balance")
            if not self.book_stream.start():
                print("  [WARN] BookStream failed to start — falling back to REST depth")

        # Reconcile stale PLACED journal entries from previous sessions.
        # After --resume, the journal may contain entries that finalized on
        # the exchange while we were offline. Query the exchange and update
        # lifecycle state; register still-open orders with the live stream.
        if not self.paper:
            self._reconcile_stale_placed()

        loop_label = "DEMO (synthetic)" if self.demo else ("PAPER" if self.paper else "LIVE")
        print(f"\n  [HYDRA] Starting {loop_label} trading loop")
        print(f"  [HYDRA] Pairs: {', '.join(self.pairs)}")
        print(f"  [HYDRA] Interval: {self.interval}s | Duration: {self.duration}s")
        print(f"  {'='*80}")

        tick = 0
        while self.running and (self.duration == 0 or (time.time() - self.start_time) < self.duration):
            # HF-004 fix: wrap the tick body in try/except so an unhandled
            # exception does not kill the run() loop. When start_hydra.bat
            # restarts the agent after a crash, in-memory order_journal
            # entries since the last snapshot are lost. Log the traceback
            # and continue.
            journal_size_start = len(self.order_journal)
            try:
                tick += 1
                elapsed = time.time() - self.start_time
                remaining = "∞" if self.duration == 0 else f"{self.duration - elapsed:.0f}s"

                ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                print(f"\n  === Tick {tick} | {ts} | Elapsed: {elapsed:.0f}s | Remaining: {remaining} ===")

                # Phase 0: System status gate — skip tick during maintenance.
                # post_only is fine (we only place post-only orders). API failure
                # degrades gracefully to "online" so we never stall on a broken
                # status endpoint.
                if not self.paper:
                    _status_resp = KrakenCLI.system_status()
                    _kraken_status = (
                        _status_resp.get("status", "online")
                        if isinstance(_status_resp, dict) and "error" not in _status_resp
                        else "online"
                    )
                    if _kraken_status not in ("online", "post_only"):
                        if self._last_kraken_status != _kraken_status:
                            print(f"  [HYDRA] Kraken status: {_kraken_status} — skipping tick")
                        self._last_kraken_status = _kraken_status
                        # Sleep the full tick interval to avoid busy-looping
                        next_tick_time = self.start_time + tick * self.interval
                        _maint_sleep = next_tick_time - time.time()
                        if _maint_sleep > 0 and self.running:
                            time.sleep(_maint_sleep)
                        continue
                    if self._last_kraken_status not in ("online", "post_only", None):
                        print(f"  [HYDRA] Kraken back online (was {self._last_kraken_status})")
                    self._last_kraken_status = _kraken_status
                    time.sleep(2)  # Rate limit

                # Refresh dead man's switch every tick (live mode only)
                if not self.paper:
                    KrakenCLI.cancel_after(self._dms_timeout)
                    time.sleep(2)  # Rate limit

                # Phase 0.5: Re-evaluate per-engine `tradable` flags from the
                # latest balance snapshot. Flips an engine to informational-
                # only when its quote currency is depleted, or re-activates
                # it when the operator (or a stable_btc fill) tops it back up.
                # Cheap dict lookup; transition logging only, no tick spam.
                if not self.paper:
                    self._refresh_tradable_flags()

                # Phase 1: Fetch data and run all engines (regimes, signals, positions)
                engine_states = {}
                for pair in self.pairs:
                    engine_states[pair] = self._fetch_and_tick(pair)

                # Capture engine's original signal before any external modifiers
                original_signals = {}
                for pair, state in engine_states.items():
                    if state:
                        original_signals[pair] = {
                            "action": state["signal"]["action"],
                            "confidence": state["signal"]["confidence"],
                        }

                # Phase 1.5: Cross-pair regime coordination
                # Update coordinator with latest regimes, then apply overrides
                for pair, state in engine_states.items():
                    if state:
                        self.coordinator.update(pair, state.get("regime", "RANGING"))

                # Rule 4 confluence needs price histories — pull from the
                # engines rather than bloating the broadcast state dicts.
                price_series = {
                    p: list(self.engines[p].prices) for p in self.pairs
                }
                cross_overrides = self.coordinator.get_overrides(
                    engine_states, price_series=price_series,
                )
                pending_swaps = []
                for pair, override in cross_overrides.items():
                    state = engine_states.get(pair)
                    if not state:
                        continue
                    print(f"  [CROSS] {pair}: {override['action']} → {override['signal']} "
                          f"(conf {override['confidence_adj']:.2f}) — {override['reason']}")
                    state["signal"]["action"] = override["signal"]
                    state["signal"]["confidence"] = override["confidence_adj"]
                    state["signal"]["reason"] = f"[CROSS-PAIR] {override['reason']}"
                    state["cross_pair_override"] = override
                    # Collect swap opportunities for execution after trades
                    if override.get("swap"):
                        pending_swaps.append(override["swap"])

                # If coordinator changed signal direction, reset baseline for cap
                for pair in self.pairs:
                    orig = original_signals.get(pair)
                    state = engine_states.get(pair)
                    if orig and state and state["signal"]["action"] != orig["action"]:
                        original_signals[pair]["confidence"] = state["signal"]["confidence"]

                # Phase 1.75: Order book intelligence
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state:
                        continue
                    depth = self.book_stream.latest_book(pair)
                    if isinstance(depth, dict) and "error" not in depth:
                        signal_action = state["signal"].get("action", "HOLD")
                        book_analysis = OrderBookAnalyzer.analyze(depth, signal_action)
                        state["order_book"] = book_analysis
                        # Apply modifier to signal confidence
                        old_conf = state["signal"]["confidence"]
                        new_conf = max(0.0, min(1.0, old_conf + book_analysis["confidence_modifier"]))
                        if book_analysis["confidence_modifier"] != 0:
                            state["signal"]["confidence"] = new_conf
                            print(f"  [BOOK] {pair}: imbalance {book_analysis['imbalance_ratio']:.2f}, "
                                  f"spread {book_analysis['spread_bps']:.1f}bps, "
                                  f"conf {old_conf:.2f} → {new_conf:.2f} "
                                  f"(mod {book_analysis['confidence_modifier']:+.2f})"
                                  f"{' [BID WALL]' if book_analysis['bid_wall'] else ''}"
                                  f"{' [ASK WALL]' if book_analysis['ask_wall'] else ''}")

                # Phase 1.8: FOREX session-aware confidence weighting
                # Crypto volume clusters around traditional FX sessions.
                # London/NY overlap (12-16 UTC) is peak liquidity → signals more reliable.
                # Dead zone (21-00 UTC) is thinnest → signals less reliable.
                utc_hour = datetime.now(timezone.utc).hour
                if 12 <= utc_hour < 16:      # London/NY overlap — peak
                    session_mod = 0.04
                    session_label = "London/NY"
                elif 7 <= utc_hour < 12:      # London session
                    session_mod = 0.02
                    session_label = "London"
                elif 16 <= utc_hour < 21:     # NY session
                    session_mod = 0.02
                    session_label = "New York"
                elif 0 <= utc_hour < 7:       # Asian session
                    session_mod = -0.03
                    session_label = "Asian"
                else:                          # 21-00 UTC dead zone
                    session_mod = -0.05
                    session_label = "dead zone"

                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state or session_mod == 0:
                        continue
                    old_conf = state["signal"]["confidence"]
                    new_conf = max(0.0, min(1.0, old_conf + session_mod))
                    if old_conf != new_conf and state["signal"]["action"] != "HOLD":
                        state["signal"]["confidence"] = new_conf
                        if abs(session_mod) >= 0.03:  # Only log notable adjustments
                            print(f"  [SESSION] {pair}: {session_label} ({utc_hour:02d}:xx UTC), "
                                  f"conf {old_conf:.2f} → {new_conf:.2f} ({session_mod:+.2f})")

                # ── Total modifier cap ──────────────────────────────────
                # External modifiers (cross-pair + order book + session) can reduce confidence
                # without limit but cannot boost it more than +0.15 above the engine's original
                # signal.  This prevents stacking modifiers from inflating weak signals into
                # high-conviction trades that get oversized via Kelly criterion.
                MAX_TOTAL_MODIFIER_BOOST = 0.15
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    orig = original_signals.get(pair)
                    if not state or not orig:
                        continue
                    orig_conf = orig["confidence"]
                    if state["signal"]["confidence"] > orig_conf + MAX_TOTAL_MODIFIER_BOOST:
                        state["signal"]["confidence"] = orig_conf + MAX_TOTAL_MODIFIER_BOOST
                    if state["signal"]["confidence"] < 0.0:
                        state["signal"]["confidence"] = 0.0

                # Phase 1.9: Compute aggregate portfolio context for brain
                try:
                    self._current_portfolio_summary = self._build_portfolio_summary()
                except Exception:
                    self._current_portfolio_summary = {}

                # v2.16.0: feed RM drawdown-velocity buffer with the
                # already-computed total NAV. Skip when summary failed.
                try:
                    equity = self._current_portfolio_summary.get("total_equity_usd")
                    if equity is not None:
                        self._record_balance_sample(time.time(), equity)
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")

                # Phase 1.95: Periodic portfolio strategist review (Grok)
                # Track candle epoch — advances when ALL pairs have new timestamps
                epoch_advanced = True
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state:
                        epoch_advanced = False
                        break
                    candles = state.get("candles", [])
                    ts = candles[-1]["t"] if candles else 0
                    prev = self._portfolio_candle_epoch.get(pair, 0.0)
                    if ts <= prev:
                        epoch_advanced = False
                        break
                if epoch_advanced:
                    for pair in self.pairs:
                        state = engine_states.get(pair)
                        candles = state.get("candles", []) if state else []
                        self._portfolio_candle_epoch[pair] = candles[-1]["t"] if candles else 0
                    self._portfolio_epoch_count += 1

                # Check for multi-pair regime transitions (2+ pairs changed)
                regime_changes = 0
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state:
                        current = state.get("regime", "RANGING")
                        if self._last_portfolio_review_regimes.get(pair) != current:
                            regime_changes += 1
                force_portfolio_review = regime_changes >= 2

                should_review = (
                    (self._portfolio_epoch_count >= 3 or force_portfolio_review)
                    and self.brain and self.brain.has_strategist
                )
                if should_review:
                    review_state = self._build_portfolio_review_state(engine_states)
                    guidance = self.brain.run_portfolio_review(review_state)
                    if guidance:
                        self._portfolio_guidance = guidance
                        # Print the FULL guidance, wrapped at 100 cols with
                        # continuation lines aligned under the message body
                        # so multi-sentence Grok output reads cleanly next to
                        # the surrounding [BRAIN] / [SWAP] / [COMPANION] lines.
                        _prefix = "  [PORTFOLIO] New guidance: "
                        _cont = " " * len(_prefix)
                        for _line in textwrap.wrap(guidance, width=100,
                                                   initial_indent=_prefix,
                                                   subsequent_indent=_cont,
                                                   break_long_words=False,
                                                   break_on_hyphens=False):
                            print(_line)
                    self._portfolio_epoch_count = 0
                    self._last_portfolio_review_regimes = {
                        p: (engine_states.get(p) or {}).get("regime", "RANGING")
                        for p in self.pairs
                    }

                # Phase 2: Run brain with full cross-pair context (parallel across pairs)
                all_states = {}
                brain_pairs = []
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state:
                        if state["signal"]["action"] != "HOLD" and self.brain:
                            brain_pairs.append((pair, state))
                        else:
                            # Inject cached brain decision for dashboard persistence.
                            # v2.14.1: tag the replay with cached_at_tick so the
                            # dashboard can distinguish a live decision from a
                            # stale one replayed across a HOLD tick. Shallow-copy
                            # so we don't mutate the cached payload in place.
                            cached = self._last_ai_decision.get(pair)
                            if cached and self.brain:
                                replay = dict(cached)
                                replay["cached"] = True
                                replay["cached_at_tick"] = state.get("tick", 0)
                                state["ai_decision"] = replay
                            all_states[pair] = state

                if brain_pairs:
                    with ThreadPoolExecutor(max_workers=len(brain_pairs)) as executor:
                        futures = {
                            executor.submit(self._apply_brain, pair, state, engine_states): pair
                            for pair, state in brain_pairs
                        }
                        for future in as_completed(futures):
                            pair = futures[future]
                            try:
                                all_states[pair] = future.result(timeout=60)
                            except Exception as e:
                                print(f"  [WARN] Brain failed for {pair}: {e}")
                                all_states[pair] = engine_states[pair]

                # Phase 2.5: Execute finalized signals on engines (deferred from
                # generate_only=True). Always runs so coordinator overrides apply
                # even without a brain. Skip swap pairs.
                swap_pairs = set()
                if pending_swaps:
                    for s in pending_swaps:
                        swap_pairs.add(s["sell_pair"])
                        swap_pairs.add(s["buy_pair"])
                for pair in self.pairs:
                    if pair in swap_pairs:
                        continue
                    state = all_states.get(pair)
                    if not state:
                        continue
                    sig = state.get("signal", {})
                    ai = state.get("ai_decision", {})
                    engine = self.engines[pair]
                    pre_trade_snap = engine.snapshot_position()
                    # Clamp the brain's size_multiplier to [0.0, 1.5] so no single
                    # modifier can exceed Kelly's hard cap.
                    _sm = ai.get("size_multiplier")
                    _brain_mult = float(1.0 if _sm is None else _sm)
                    _final_mult = max(0.0, min(1.5, _brain_mult))
                    _action = sig.get("action", "HOLD")
                    if self._should_block_buy_for_portfolio_dd(
                        getattr(self, "_portfolio_buy_halted", False), _action
                    ):
                        trade = None
                        print(
                            f"  [PORTFOLIO CB] {pair}: BUY blocked "
                            f"(portfolio max DD "
                            f"{getattr(self, '_portfolio_max_drawdown_pct', 0.0):.1f}%)"
                        )
                    else:
                        trade = engine.execute_signal(
                            action=_action,
                            confidence=sig.get("confidence", 0),
                            reason=sig.get("reason", ""),
                            strategy=state.get("strategy", "MOMENTUM"),
                            size_multiplier=_final_mult,
                        )
                    if trade is None and sig.get("action") in ("BUY", "SELL") and ai:
                        print(f"  [BRAIN] {pair}: {sig['action']} signal did not execute "
                              f"(conf={sig.get('confidence', 0):.2f}, "
                              f"size_mult={ai.get('size_multiplier', 1.0):.2f}, "
                              f"brain={ai.get('action', '?')})")
                    if trade:
                        is_usd_pair = (pair.split("/")[1].upper() if "/" in pair else "") in STABLE_QUOTES
                        value_decimals = 2 if is_usd_pair else 8
                        state["last_trade"] = {
                            "action": trade.action,
                            "price": round(trade.price, 8),
                            "amount": round(trade.amount, 8),
                            "value": round(trade.value, value_decimals),
                            "reason": trade.reason,
                            "confidence": round(trade.confidence, 4),
                            "profit": round(trade.profit, value_decimals) if trade.profit is not None else None,
                            "params_at_entry": trade.params_at_entry,
                        }
                        state["_pre_trade_snapshot"] = pre_trade_snap

                # Print status and place orders (sequential — rate limiting required)
                # Skip swap pairs — the swap handler manages their execution.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if state:
                        self._print_tick_status(pair, state)
                        if state.get("last_trade") and pair not in swap_pairs:
                            success = self._place_order(pair, state["last_trade"], state)
                            if not success and state.get("_pre_trade_snapshot"):
                                engine = self.engines[pair]
                                engine.restore_position(state["_pre_trade_snapshot"])
                                print(f"  [ROLLBACK] {pair}: engine state rolled back after failed placement")

                # Phase 3: Execute coordinated swaps, then check regime transitions
                if pending_swaps:
                    for swap in pending_swaps:
                        self._execute_coordinated_swap(swap, all_states)
                self._log_regime_transitions(all_states)

                # Phase 4: Record trade outcomes for self-tuning
                # Only record when a position is fully closed so the tuner learns
                # from the total accumulated P&L, not individual partial-sell legs.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if not state or not state.get("last_trade"):
                        continue
                    trade = state["last_trade"]
                    engine = self.engines[pair]
                    if trade["action"] == "SELL" and trade.get("profit") is not None and engine.position.size == 0:
                        params_at_entry = trade.get("params_at_entry") or engine.snapshot_params()
                        outcome = "win" if trade["profit"] > 0 else "loss"
                        self.trackers[pair].record_trade(
                            params_at_entry, "SELL", outcome, trade["profit"],
                        )
                        self._completed_trades_since_update += 1

                # Run tuner updates every 50 completed trades
                if self._completed_trades_since_update >= 50:
                    self._run_tuner_update()

                # Strip internal rollback data before broadcasting to dashboard
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if state:
                        state.pop("_pre_trade_snapshot", None)

                # Refresh performance/portfolio/position in state dicts from
                # engine's actual state. When brain is active, tick() ran with
                # generate_only=True so the state dict was built BEFORE
                # execute_signal() updated counters.  Even without brain,
                # a failed order + rollback can desync the dict.  Refreshing
                # here ensures the dashboard always sees authoritative values.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if not state:
                        continue
                    engine = self.engines[pair]
                    current_price = engine.prices[-1] if engine.prices else 0
                    equity = engine.balance + (engine.position.size * current_price)
                    is_usd_pair = (pair.split("/")[1].upper() if "/" in pair else "") in STABLE_QUOTES
                    vd = 2 if is_usd_pair else 8
                    pnl_pct = ((equity - engine.initial_balance) / engine.initial_balance * 100) if engine.initial_balance > 0 else 0
                    wl = engine.win_count + engine.loss_count
                    win_rate = (engine.win_count / wl * 100) if wl > 0 else 0
                    state["performance"] = {
                        "total_trades": engine.total_trades,
                        "win_count": engine.win_count,
                        "loss_count": engine.loss_count,
                        "win_rate_pct": round(win_rate, 2),
                        "sharpe_estimate": round(engine._calc_sharpe(), 4),
                    }
                    state["portfolio"] = {
                        "balance": round(engine.balance, vd),
                        "equity": round(equity, vd),
                        "pnl_pct": round(pnl_pct, 4),
                        "max_drawdown_pct": round(engine.max_drawdown, 4),
                        "peak_equity": round(engine.peak_equity, vd),
                    }
                    state["position"] = {
                        "size": round(engine.position.size, 8),
                        "avg_entry": round(engine.position.avg_entry, 8),
                        "unrealized_pnl": round(engine.position.unrealized_pnl, vd),
                    }

                # Broadcast state to dashboard (uses cached balance, no extra API call)
                dashboard_state = self._build_dashboard_state(tick, all_states, elapsed)
                self.broadcaster.broadcast(dashboard_state)

                # Drain queued WS execution events and apply them to the
                # journal + engine state. Pushes, not polls — the stream
                # has been delivering events in the background since tick
                # start. In paper mode this drains any synthetic fills
                # _place_paper_order injected during this tick.
                #
                # Health policy: ensure_healthy() reports current state and,
                # in live mode, attempts an auto-restart of the subprocess
                # if it's dead (subject to RESTART_COOLDOWN_S). The warning
                # is rate-limited to transitions — printing every tick spams
                # the operator and obscures the actionable signal. The reason
                # string identifies WHICH check failed so we can debug.
                if not self.execution_stream.paper:
                    healthy, reason = self.execution_stream.ensure_healthy()
                    if not healthy:
                        if self._exec_stream_warned_reason != reason:
                            print(
                                f"  [WARN] execution stream unhealthy — {reason} "
                                f"(lifecycle finalization stalled)"
                            )
                            self._exec_stream_warned_reason = reason
                    elif self._exec_stream_warned_reason is not None:
                        print("  [EXECSTREAM] stream healthy again")
                        self._exec_stream_warned_reason = None
                for term in self.execution_stream.drain_events():
                    self._apply_execution_event(term)

                # Market data stream health — auto-restart if dead.
                # No transition logging needed; REST fallback is seamless.
                if not self.paper:
                    self.candle_stream.ensure_healthy()
                    self.ticker_stream.ensure_healthy()
                    self.balance_stream.ensure_healthy()
                    self.book_stream.ensure_healthy()

                # Rolling save — persist the order journal every tick so
                # no data is lost on crash. Atomic write (.tmp + os.replace)
                # so a crash mid-write cannot corrupt the file into
                # half-valid JSON. Mirrors _save_snapshot's pattern.
                # Offline --demo never touches the operator rolling journal.
                if not getattr(self, "demo", False):
                    filtered_journal = self._journal_for_persistence()
                    if filtered_journal:
                        rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")
                        rolling_tmp = rolling_file + ".tmp"
                        try:
                            with open(rolling_tmp, "w") as f:
                                json.dump(filtered_journal, f, indent=2)
                            os.replace(rolling_tmp, rolling_file)
                        except Exception as e:
                            # HF-003 fix: previously "except Exception: pass" silently
                            # swallowed write failures (permission, disk, lock, etc.),
                            # making logging outages invisible. Log the failure so it's
                            # visible in stdout and in hydra_errors.log via the outer
                            # tick-body exception handler.
                            print(f"  [WARN] rolling journal write failed: {type(e).__name__}: {e}")

                # Cap order journal to prevent unbounded memory growth
                if len(self.order_journal) > self.ORDER_JOURNAL_CAP:
                    self.order_journal = self.order_journal[-self.ORDER_JOURNAL_CAP:]


            except Exception as e:
                print(f"  [ERROR] Tick {tick} crashed: {type(e).__name__}: {e}")
                try:
                    err_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_errors.log")
                    with open(err_file, "a", encoding="utf-8") as f:
                        f.write(f"\n=== Tick {tick} @ {datetime.now(timezone.utc).isoformat()} ===\n")
                        f.write(traceback.format_exc())
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")  # if error log write fails, at least we printed to stdout

            # HF-004 fix: snapshot immediately if the journal grew this tick,
            # so a subsequent crash does not lose the newly-appended entries.
            # Also save on the periodic cadence for engine state that
            # changes without placements.
            journal_grew = len(self.order_journal) > journal_size_start
            if journal_grew or tick % self.SNAPSHOT_EVERY_N_TICKS == 0:
                self._save_snapshot()

            # Sleep until next tick
            next_tick_time = self.start_time + tick * self.interval
            sleep_time = next_tick_time - time.time()
            if sleep_time > 0 and self.running:
                time.sleep(sleep_time)
        # Final tuner update on shutdown
        self._run_tuner_update()

        # Final report
        self._print_final_report()

    def _demo_seed_price(self, pair: str) -> float:
        """Return a reasonable starting mid for offline synthetic series."""
        if pair in self._demo_prices:
            return self._demo_prices[pair]
        key = pair.upper() if isinstance(pair, str) else pair
        if key in _DEMO_SEED_PRICES:
            return float(_DEMO_SEED_PRICES[key])
        # Heuristic from base/quote roles when pair not in the seed table.
        base = key.split("/")[0] if "/" in key else key
        quote = key.split("/")[1] if "/" in key else "USD"
        if base == "BTC" and quote in STABLE_QUOTES:
            return 95_000.0
        if base == "SOL" and quote in STABLE_QUOTES:
            return 150.0
        if base == "SOL" and quote == "BTC":
            return 0.00158
        return 100.0

    def _make_synthetic_candle(self, pair: str, *, advance: bool = True) -> dict:
        """One OHLC bar for offline --demo. Advances mid when advance=True."""
        mid = self._demo_prices.get(pair) or self._demo_seed_price(pair)
        if advance:
            # Mild random walk; keep prices strictly positive.
            shock = self._demo_rng.gauss(0.00015, 0.004)
            mid = max(mid * (1.0 + shock), mid * 1e-6 if mid > 0 else 1e-6)
            self._demo_prices[pair] = mid
        # Unique timestamps so engine.ingest_candle does not dedupe-in-place forever.
        ts = time.time() + self._demo_rng.random() * 0.001
        wiggle = abs(self._demo_rng.gauss(0, 0.002))
        high = mid * (1.0 + wiggle + 0.001)
        low = mid * (1.0 - wiggle - 0.001)
        open_ = mid * (1.0 + self._demo_rng.gauss(0, 0.0005))
        return {
            "open": open_,
            "high": max(high, open_, mid, low),
            "low": min(low, open_, mid, high),
            "close": mid,
            "volume": 50.0 + self._demo_rng.random() * 200.0,
            "timestamp": ts,
        }

    def _warmup_demo_candles(self, n: int = 80) -> None:
        """Seed engines with synthetic history so indicators leave warmup."""
        for pair in self.pairs:
            if pair not in self._demo_prices:
                self._demo_prices[pair] = self._demo_seed_price(pair)
            # Space synthetic history backwards so timestamps are ordered.
            base_ts = time.time() - n * float(self.candle_interval) * 60.0
            for i in range(n):
                c = self._make_synthetic_candle(pair, advance=True)
                c["timestamp"] = base_ts + (i + 1) * float(self.candle_interval) * 60.0
                self.engines[pair].ingest_candle(c)
            last = self._demo_prices[pair]
            print(f"  [HYDRA] {pair}: {n} synthetic candles, last price: {last:,.6g}")

    def _fetch_and_tick(self, pair: str) -> Optional[dict]:
        """Phase 1: Fetch latest data and run engine tick.

        Prefers WS candle stream when a real push is available.
        Offline --demo synthesizes the next bar (no WSL / keys).
        Paper mode falls back to REST ohlc when the paper stream has no
        push payload (paper streams report healthy but never deliver).
        Live mode without a candle returns None (stream recovery path).

        When a brain is active, uses generate_only=True so the engine produces
        signals without executing trades internally. This prevents engine state
        from diverging when the brain later overrides a signal.
        """
        engine = self.engines[pair]
        candle_ingested = False

        # Offline demo: always synthesize a fresh bar (no exchange I/O).
        if getattr(self, "demo", False):
            engine.ingest_candle(self._make_synthetic_candle(pair, advance=True))
            candle_ingested = True
        else:
            # Try WS candle stream first (no API call, no rate-limit sleep)
            ws_candle = (
                self.candle_stream.latest_candle(pair)
                if self.candle_stream.healthy
                else None
            )
            if ws_candle:
                # Convert WS ohlc shape to engine candle format.
                # WS uses interval_begin (ISO) or timestamp; parse to epoch.
                ts_raw = ws_candle.get("interval_begin") or ws_candle.get("timestamp")
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts = time.time()
                elif isinstance(ts_raw, (int, float)):
                    ts = float(ts_raw)
                else:
                    ts = time.time()
                engine.ingest_candle({
                    "open": ws_candle.get("open", 0),
                    "high": ws_candle.get("high", 0),
                    "low": ws_candle.get("low", 0),
                    "close": ws_candle.get("close", 0),
                    "volume": ws_candle.get("volume", 0),
                    "timestamp": ts,
                })
                candle_ingested = True

            # Paper streams short-circuit healthy=True but never push candles.
            # Fall back to REST so --paper actually advances the engines.
            if not candle_ingested and self.paper:
                candles = KrakenCLI.ohlc(pair, interval=self.candle_interval)
                if candles:
                    # Prefer the newest bar; re-ingest updates in place by ts.
                    engine.ingest_candle(candles[-1])
                    candle_ingested = True
                time.sleep(KRAKEN_REST_FLOOR_S)

        if not candle_ingested:
            # Live: CandleStream unavailable — skip tick for this pair.
            # Engine retains previous candle data from warmup / prior ticks.
            return None

        # Always generate_only so coordinator can mutate the signal before
        # phase-2.5 execute_signal. Pre-trade snapshots are taken in phase 2.5
        # immediately before execute (not here).
        state = engine.tick(generate_only=True)
        return state

    def _apply_brain(self, pair: str, state: dict, all_engine_states: dict) -> dict:
        """Phase 2: Run brain with full cross-pair context. Mutates state in place."""
        if not self.brain or state["signal"]["action"] == "HOLD":
            # Inject cached decision for dashboard persistence (brain didn't fire)
            cached = self._last_ai_decision.get(pair)
            if cached:
                state["ai_decision"] = cached
            return state

        # Pre-brain filter: skip brain for BUY signals that can't produce tradeable order size
        if state["signal"]["action"] == "BUY":
            engine = self.engines[pair]
            test_size = engine.sizer.calculate(
                state["signal"]["confidence"], engine.balance, state["price"], pair,
            )
            if test_size == 0:
                cached = self._last_ai_decision.get(pair)
                if cached:
                    state["ai_decision"] = cached
                return state  # Signal too weak to trade; don't waste brain tokens

        # Candle-freshness gate: only invoke brain when the pair has a NEW candle.
        # On forming-candle updates (same interval_begin), the engine deduplicates
        # in place — indicators are near-identical.  Skip brain to avoid duplicate
        # evaluation on unchanged data.
        candles = state.get("candles", [])
        current_candle_ts = candles[-1]["t"] if candles else 0.0
        last_ts = self._last_brain_candle_ts.get(pair, 0.0)
        if current_candle_ts > 0 and current_candle_ts == last_ts:
            cached = self._last_ai_decision.get(pair)
            if cached:
                state["ai_decision"] = cached
            return state  # Same candle as last brain evaluation — skip

        # Inject cross-pair triangle context and portfolio-level awareness
        state["triangle_context"] = self._build_triangle_context(pair, all_engine_states)
        state["portfolio_summary"] = self._current_portfolio_summary
        if self._portfolio_guidance:
            state["portfolio_guidance"] = self._portfolio_guidance

        # Fetch spread data for risk assessment. Prefer WS ticker (no API call).
        try:
            ticker = self.ticker_stream.latest_ticker(pair) or {}
            if "error" not in ticker and "bid" in ticker:
                bid, ask = ticker["bid"], ticker["ask"]
                mid = (bid + ask) / 2
                spread_bps = round((ask - bid) / mid * 10000, 1) if mid > 0 else 0
                state["spread"] = {"bid": bid, "ask": ask, "spread_bps": spread_bps}
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

        # v2.14: inject QUANT INDICATORS block — DerivativesStream values
        # plus engine's CVD divergence. Absent stream / stale data surface
        # as None; the Quant prompt + Python R10 rule handle degradation.
        # v2.16.0: also adds the six engine-internal RM features.
        self._build_quant_indicators(pair, state)

        try:
            decision = self.brain.deliberate(state)

            # v2.14 W3: API-down safety. When the brain has fallen back
            # specifically because the LLM API is unreachable (api_available
            # is False, i.e. we are inside the 60-tick backoff window after
            # 3+ consecutive failures), block NEW entries and let exits
            # pass through. An unvetted BUY during an Anthropic outage can
            # print money on the wrong side of a regime flip that the Quant
            # would have flagged; an unvetted SELL only ever reduces risk
            # and is therefore safe to pass to the engine.
            # Budget-exceeded fallbacks (api_available=True, budget capped)
            # are deliberate and do NOT trigger the block.
            api_down = decision.fallback and not self.brain.api_available
            blocked_by_api_down = False
            if api_down and state["signal"]["action"] == "BUY":
                blocked_by_api_down = True
                original_reason = state["signal"].get("reason", "")
                state["signal"]["action"] = "HOLD"
                state["signal"]["reason"] = (
                    f"[API DOWN BLOCK] LLM unreachable; entry suppressed "
                    f"(retry_at_tick={self.brain.retry_at_tick}). Original: {original_reason}"
                )
                try:
                    self.brain._log_jsonl({
                        "event": "api_down_block",
                        "pair": pair,
                        "tick": state.get("tick", 0),
                        "blocked_action": "BUY",
                        "original_reason": original_reason,
                        "consecutive_failures": self.brain.consecutive_failures,
                        "retry_at_tick": self.brain.retry_at_tick,
                    })
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")

            # v2.14 W1f: deterministic rule stack. Apply R1-R10 on the
            # SAME engine signal + quant context the LLMs saw, producing a
            # final size_multiplier that composes with the brain's
            # quant × rm product. Force_hold from any rule forces the
            # action to HOLD. Rules fire on indicator VALUES, not LLM
            # interpretation — the guardrail LLMs cannot talk around.
            #
            # v2.14.1: rules now run even on fallback/api_down_block paths.
            # R10 (staleness) and R3/R4 (OI regime) operate on indicator
            # values alone and should still fire in those states — a stale
            # or crashing data feed is MORE dangerous when the LLMs are
            # also unavailable, not less. R8 (contrarian edge) needs
            # positioning_bias, which is absent in fallback, so it simply
            # won't fire — acceptable degradation.
            rules_triggered: list = []
            rules_force_hold = False
            rules_size_mult = 1.0
            rules_force_hold_reason = ""
            engine_action_for_rules = state["signal"]["action"]
            quant_out_for_rules = {
                "positioning_bias": getattr(decision, "positioning_bias", None)
                    or state.get("ai_positioning_bias") or "",
                "force_hold": False,
            }
            # PR-E / E1: kill switch must skip rules entirely (not just the
            # derivatives stream). Otherwise null indicators R10-blackout.
            _quant_rules_disabled = (
                os.environ.get("HYDRA_QUANT_INDICATORS_DISABLED") == "1"
            )
            try:
                if not _quant_rules_disabled:
                    from hydra_quant_rules import apply_rules as _apply_quant_rules
                    rule_result = _apply_quant_rules(
                        engine_action=engine_action_for_rules,
                        quant_output=quant_out_for_rules,
                        quant_indicators=state.get("quant_indicators") or None,
                    )
                    rules_triggered = [
                        {"rule_id": f.rule_id, "name": f.name, "effect": f.effect,
                         "size_mult": f.size_mult, "reason": f.reason}
                        for f in rule_result.triggered
                    ]
                    rules_force_hold = rule_result.force_hold
                    rules_force_hold_reason = rule_result.force_hold_reason
                    rules_size_mult = rule_result.size_multiplier
            except Exception as re:
                print(f"  [QUANT RULES] apply_rules error ({type(re).__name__}: {re})")

            # Final stacked size: brain (quant × rm) × rules. Clamp once.
            # v2.14.1: record the unclamped product so the dashboard can
            # tell the operator when the [0, 1.5] ceiling was actually
            # binding vs. merely a defensive guard.
            brain_size = float(1.0 if decision.size_multiplier is None else decision.size_multiplier)
            pre_clamp_product = brain_size * rules_size_mult
            final_size_multiplier = max(0.0, min(1.5, pre_clamp_product))
            size_clamp_applied = pre_clamp_product != final_size_multiplier
            if rules_force_hold:
                final_size_multiplier = 0.0

            # v2.14.1: preserve the original engine reason when W3 api-down
            # block rewrote state["signal"]["reason"], so the dashboard can
            # surface both threads without parsing the "Original: ..." tail.
            api_down_original_reason = ""
            if blocked_by_api_down:
                api_down_original_reason = original_reason

            state["ai_decision"] = {
                "action": decision.action,
                "final_signal": decision.final_signal,
                "confidence_adj": decision.confidence_adj,
                # v2.14: three-layer size disclosure for auditability.
                "size_multiplier": final_size_multiplier,
                "size_multiplier_brain": brain_size,           # quant × rm
                "size_multiplier_rules": rules_size_mult,      # R1-R10 stack
                # v2.14.1: unclamped product and clamp-applied flag so the
                # dashboard can distinguish "hit the ceiling" from "under".
                "size_multiplier_unclamped": round(pre_clamp_product, 4),
                "size_multiplier_clamped": size_clamp_applied,
                "rules_triggered": rules_triggered,
                "rules_force_hold": rules_force_hold,
                "rules_force_hold_reason": rules_force_hold_reason,
                "analyst_reasoning": decision.analyst_reasoning,
                "risk_reasoning": decision.risk_reasoning,
                "strategist_reasoning": decision.strategist_reasoning,
                # v2.14.2: structured rationale — rendered as pills/chips
                # on the dashboard so the decision reads as an audit trail
                # rather than one mono-text blob.
                "positioning_bias": decision.positioning_bias,
                "key_factors": decision.key_factors,
                "concern": decision.concern,
                "signal_agreement": decision.signal_agreement,
                "escalated": decision.escalated,
                "summary": decision.combined_summary,
                "risk_flags": decision.risk_flags,
                "portfolio_health": decision.portfolio_health,
                "fallback": decision.fallback,
                "api_down_block": blocked_by_api_down,
                # v2.14.1: separate field for the pre-rewrite engine reason.
                "api_down_original_reason": api_down_original_reason,
                "tokens_used": decision.tokens_used,
                "latency_ms": round(decision.latency_ms, 0),
                # v2.14: surface the indicator block that drove this decision
                "quant_indicators": state.get("quant_indicators") or None,
                # v2.14.1: tick counter this decision was generated at.
                # When the dashboard replays a cached decision on a pair
                # that didn't re-deliberate, current_tick > this value.
                "generated_at_tick": state.get("tick", 0),
            }
            # Cache for dashboard persistence on ticks where brain doesn't fire
            self._last_ai_decision[pair] = state["ai_decision"]

            # Mark candle as evaluated only when brain ran LLM calls (not fallback).
            # On fallback (budget exceeded, API down), leave timestamp unchanged so
            # the next tick retries this candle.
            if not decision.fallback:
                self._last_brain_candle_ts[pair] = current_candle_ts

            # Apply AI decision to engine state
            # Note: engine ran with generate_only=True, so no trade was executed yet.
            # Brain controls sizing via size_multiplier only — engine confidence
            # passes through untouched to Kelly criterion.  confidence_adj is
            # preserved in state["ai_decision"] for dashboard/logging.
            if blocked_by_api_down:
                pass  # state["signal"] already rewritten above; skip OVERRIDE/ADJUST below
            elif rules_force_hold:
                # v2.14: a deterministic rule trumped the LLM layer. Force
                # HOLD and surface which rule, so audit is unambiguous.
                state["signal"]["action"] = "HOLD"
                state["signal"]["reason"] = f"[QUANT RULES FORCE_HOLD] {rules_force_hold_reason}"
            elif decision.action == "OVERRIDE":
                state["signal"]["action"] = decision.final_signal
                state["signal"]["reason"] = f"[AI OVERRIDE] {decision.combined_summary}"
                # PR-E / E2: re-run rules on FINAL action so SELL→BUY cannot
                # skip R1 (or any direction-sensitive rule).
                if (not _quant_rules_disabled
                        and decision.final_signal
                        and decision.final_signal != engine_action_for_rules):
                    try:
                        from hydra_quant_rules import apply_rules as _apply_quant_rules
                        rr2 = _apply_quant_rules(
                            engine_action=decision.final_signal,
                            quant_output=quant_out_for_rules,
                            quant_indicators=state.get("quant_indicators") or None,
                        )
                        if rr2.force_hold:
                            rules_force_hold = True
                            rules_force_hold_reason = rr2.force_hold_reason
                            final_size_multiplier = 0.0
                            state["signal"]["action"] = "HOLD"
                            state["signal"]["reason"] = (
                                f"[QUANT RULES FORCE_HOLD post-OVERRIDE] "
                                f"{rr2.force_hold_reason}"
                            )
                            for f in rr2.triggered:
                                rules_triggered.append({
                                    "rule_id": f.rule_id, "name": f.name,
                                    "effect": f.effect, "size_mult": f.size_mult,
                                    "reason": f.reason,
                                })
                            state["ai_decision"]["rules_force_hold"] = True
                            state["ai_decision"]["rules_force_hold_reason"] = (
                                rr2.force_hold_reason
                            )
                            state["ai_decision"]["size_multiplier"] = 0.0
                            state["ai_decision"]["rules_triggered"] = rules_triggered
                    except Exception as re2:
                        print(f"  [QUANT RULES] post-OVERRIDE re-apply error "
                              f"({type(re2).__name__}: {re2})")
            elif decision.action == "ADJUST":
                state["signal"]["reason"] = f"[AI ADJUSTED] {decision.combined_summary}"
            # CONFIRM leaves signal unchanged, just adds reasoning

            # R11/QFE — Quant Force Exit: rescue profitable exits from
            # force_hold.  Runs AFTER signal rewriting — if the engine
            # wanted SELL and it got blocked to HOLD (by rules OR brain),
            # check if we should let the exit through to capture profit.
            qfe_active = False
            qfe_reason = ""
            qfe_trigger_values: dict = {}
            if (engine_action_for_rules == "SELL"
                    and state["signal"]["action"] == "HOLD"
                    and not blocked_by_api_down
                    and not _quant_rules_disabled):
                pos = state.get("position", {})
                pos_size = pos.get("size", 0)
                avg_entry = pos.get("avg_entry", 0)
                current_price = state.get("price", 0)
                if pos_size > 0 and avg_entry > 0:
                    pnl_pct = (current_price - avg_entry) / avg_entry * 100
                    try:
                        from hydra_quant_rules import evaluate_qfe as _evaluate_qfe
                        positioning = (
                            getattr(decision, "positioning_bias", None)
                            or state.get("ai_positioning_bias") or ""
                        )
                        qfe_result = _evaluate_qfe(
                            position_size=pos_size,
                            unrealized_pnl_pct=pnl_pct,
                            quant_indicators=state.get("quant_indicators"),
                            positioning_bias=positioning,
                        )
                        if qfe_result.force_exit:
                            qfe_active = True
                            qfe_reason = qfe_result.force_exit_reason
                            qfe_trigger_values = qfe_result.trigger_values
                            state["signal"]["action"] = "SELL"
                            state["signal"]["reason"] = (
                                f"[QFE PROFIT EXIT] {qfe_reason}"
                            )
                            final_size_multiplier = 1.0
                            state["ai_decision"]["size_multiplier"] = 1.0
                            print(
                                f"  [QFE] {pair}: force_exit overrides "
                                f"force_hold — P&L {pnl_pct:+.2f}%, "
                                f"no squeeze catalyst"
                            )
                    except Exception as qe:
                        print(f"  [QFE] evaluate_qfe error ({type(qe).__name__}: {qe})")

            state["ai_decision"]["qfe_active"] = qfe_active
            state["ai_decision"]["qfe_reason"] = qfe_reason
            state["ai_decision"]["qfe_trigger_values"] = qfe_trigger_values
        except Exception as e:
            state["ai_decision"] = {"action": "FALLBACK", "error": str(e), "fallback": True}
            # Do NOT update _last_brain_candle_ts — allow retry on next tick

        return state

    def _build_triangle_context(self, current_pair: str, all_states: dict) -> dict:
        """Build cross-pair context summary for brain deliberation."""
        pairs = {}
        sol_exposure = 0.0
        btc_exposure = 0.0

        for pair, state in all_states.items():
            if state is None:
                continue
            pos = state.get("position", {}).get("size", 0)
            price = state.get("price", 0)

            # Net asset exposure across the triangle.
            # Spot positions: holding SOL (whether purchased via stable
            # quote or BTC) only adds SOL exposure. The BTC spent on a
            # SOL/BTC buy is already reflected in the account's BTC
            # balance, not a synthetic "short BTC" obligation — this is
            # spot trading, not margin. BTC exposure comes exclusively
            # from the stable_btc pair.
            pair_obj = KrakenCLI.registry.get(pair)
            if pair_obj is not None:
                if pair_obj.base == "SOL":
                    sol_exposure += pos
                elif pair_obj.base == "BTC" and pair_obj.is_stable_quoted:
                    btc_exposure += pos

            # Sibling pair summaries (exclude current pair)
            if pair != current_pair:
                pairs[pair] = {
                    "regime": state.get("regime", "UNKNOWN"),
                    "signal": state.get("signal", {}).get("action", "HOLD"),
                    "confidence": state.get("signal", {}).get("confidence", 0),
                    "position_size": pos,
                    "price": price,
                }

        return {
            "pairs": pairs,
            "net_exposure": {
                "SOL": round(sol_exposure, 6),
                "BTC": round(btc_exposure, 6),
            },
        }

    # ─── Portfolio-level awareness ───

    def _build_portfolio_summary(self) -> dict:
        """Compute aggregate portfolio stats across all pairs for brain context."""
        asset_prices = self._get_asset_prices()
        total_equity_usd = 0.0
        total_realized_usd = 0.0
        total_unrealized_usd = 0.0
        total_initial_usd = 0.0
        agg_wins = 0
        agg_losses = 0
        agg_trades = 0
        worst_dd = 0.0
        per_pair_pnl: Dict[str, float] = {}

        for pair in self.pairs:
            engine = self.engines.get(pair)
            if not engine:
                continue
            price = engine.prices[-1] if engine.prices else 0
            equity = engine.balance + (engine.position.size * price)
            quote = pair.split("/")[1] if "/" in pair else "USD"
            quote_usd = asset_prices.get(quote, 1.0)

            # P&L
            realized = self._compute_pair_realized_pnl(pair)
            unrealized = (engine.position.size * (price - engine.position.avg_entry)
                          if engine.position.size > 0 else 0)
            total_realized_usd += realized * quote_usd
            total_unrealized_usd += unrealized * quote_usd
            total_equity_usd += equity * quote_usd
            total_initial_usd += engine.initial_balance * quote_usd
            per_pair_pnl[pair] = round((realized + unrealized) * quote_usd, 2)

            # Aggregate performance
            agg_wins += engine.win_count
            agg_losses += engine.loss_count
            agg_trades += engine.total_trades
            if engine.max_drawdown > worst_dd:
                worst_dd = engine.max_drawdown

        total_pnl_usd = total_realized_usd + total_unrealized_usd
        total_pnl_pct = (total_pnl_usd / total_initial_usd * 100) if total_initial_usd > 0 else 0
        agg_wl = agg_wins + agg_losses
        agg_win_rate = (agg_wins / agg_wl * 100) if agg_wl > 0 else 0

        # Net USD exposure
        net_exposure_usd = 0.0
        for pair in self.pairs:
            engine = self.engines.get(pair)
            if engine and engine.position.size > 0:
                price = engine.prices[-1] if engine.prices else 0
                quote = pair.split("/")[1] if "/" in pair else "USD"
                quote_usd = asset_prices.get(quote, 1.0)
                net_exposure_usd += engine.position.size * price * quote_usd

        # Recent trades from journal (last 10 filled, all pairs)
        FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
        recent_trades = []
        for entry in reversed(self.order_journal):
            lc = entry.get("lifecycle") or {}
            if lc.get("state") not in FILL_STATES:
                continue
            recent_trades.append({
                "pair": entry.get("pair", "?"),
                "side": entry.get("side", "?"),
                "price": lc.get("avg_fill_price") or (entry.get("intent") or {}).get("limit_price") or 0,
                "vol": lc.get("vol_exec") or 0,
                "time": (entry.get("placed_at") or "")[:16],
            })
            if len(recent_trades) >= 10:
                break
        recent_trades.reverse()  # chronological order

        return {
            "total_equity_usd": round(total_equity_usd, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "agg_win_rate_pct": round(agg_win_rate, 1),
            "agg_trades": agg_trades,
            "worst_drawdown_pct": round(worst_dd, 2),
            "per_pair_pnl_usd": per_pair_pnl,
            "net_exposure_usd": round(net_exposure_usd, 2),
            "recent_trades": recent_trades,
        }

    def _build_portfolio_review_state(self, engine_states: dict) -> dict:
        """Build enriched portfolio state for Grok portfolio review."""
        ps = dict(self._current_portfolio_summary)
        pair_details = []
        for pair in self.pairs:
            engine = self.engines.get(pair)
            state = engine_states.get(pair, {})
            if not engine:
                continue
            pair_details.append({
                "pair": pair,
                "regime": state.get("regime", "UNKNOWN"),
                "signal": state.get("signal", {}).get("action", "HOLD"),
                "confidence": state.get("signal", {}).get("confidence", 0),
                "position": engine.position.size,
                "pnl_usd": ps.get("per_pair_pnl_usd", {}).get(pair, 0),
                "drawdown": engine.max_drawdown,
                "wins": engine.win_count,
                "losses": engine.loss_count,
            })
        ps["pair_details"] = pair_details
        return ps

    # ─── Order placement (writes the journal, registers with the stream) ───

    # Safety gap: when reseeding from journal history, jump this far ahead
    # so we're not sharing the immediate neighborhood with any recent entry.
    _USERREF_SAFETY_GAP = 1000

    def _journal_max_userref(self) -> int:
        """Scan self.order_journal for the highest integer userref seen.
        Returns 0 if none found."""
        hi = 0
        for entry in self.order_journal:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("order_ref") or {}
            if not isinstance(ref, dict):
                continue
            uref = ref.get("order_userref")
            if isinstance(uref, int) and 0 < uref < (1 << 31) and uref > hi:
                hi = uref
        return hi

    def _reseed_userref_from_history(self) -> None:
        """Raise _userref_counter above anything historically used.

        Called once in __init__ after snapshot load + journal merge. Protects
        against restart-collision: if the previous session left open orders
        with userrefs near the current time (the default seed), a fresh seed
        could re-issue the same userref and route WS fills to the wrong
        journal entry via _userref_to_order_id.
        """
        journal_max = self._journal_max_userref()
        if journal_max > 0:
            new_floor = min(journal_max + self._USERREF_SAFETY_GAP, 0x7FFFFFFF)
            if new_floor > self._userref_counter:
                self._userref_counter = new_floor

    def _next_userref(self) -> int:
        """Monotonic client tag used for --userref on placement so WS
        executions can correlate back to the local journal entry."""
        self._userref_counter += 1
        # Kraken userref is int32. Wrap defensively — re-consult history so
        # the wrap-reseed can't land back on a still-open order's userref.
        if self._userref_counter > 0x7FFFFFFF:
            time_seed = int(time.time()) & 0x7FFFFFFF
            journal_max = self._journal_max_userref()
            self._userref_counter = max(time_seed, journal_max + self._USERREF_SAFETY_GAP)
            if self._userref_counter > 0x7FFFFFFF:
                # Extreme degenerate case: journal has values near 2^31. Fall
                # back to time_seed alone, accepting the micro-collision risk.
                self._userref_counter = time_seed
        return self._userref_counter

    def _build_journal_entry(self, pair: str, trade: dict, state: dict) -> Dict[str, Any]:
        """Construct a new-shape order journal entry from a tick's trade
        intent + decision context. Lifecycle is filled in by the caller
        once placement completes (initial state = PLACED on success,
        PLACEMENT_FAILED on any pre-exchange failure).

        Decision context is pulled from `state` — this is the bot's
        private view of why it's placing the order, and the one thing
        Kraken cannot reconstruct.
        """
        action_upper = trade["action"].upper()
        confidence = trade.get("confidence")
        # Brain verdict summary if the brain fired this tick
        ai = state.get("ai_decision") if isinstance(state, dict) else None
        brain_verdict = None
        if isinstance(ai, dict) and not ai.get("fallback"):
            brain_verdict = {
                "action": ai.get("action"),
                "final_signal": ai.get("final_signal"),
                "summary": ai.get("summary"),
            }
        book = state.get("order_book") if isinstance(state, dict) else None
        book_mod = book.get("confidence_modifier") if isinstance(book, dict) else None
        return {
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "pair": pair,
            "side": action_upper,
            "intent": {
                "amount": trade["amount"],
                "limit_price": trade.get("price"),
                # v2.15.0: paper mode now also records limit+post-only so
                # harness drift tests can enforce post-only across modes.
                "post_only": True,
                "order_type": "limit",
                "paper": self.paper,
            },
            "decision": {
                "strategy": state.get("strategy") if isinstance(state, dict) else None,
                "regime": state.get("regime") if isinstance(state, dict) else None,
                "reason": trade.get("reason"),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                "params_at_entry": trade.get("params_at_entry"),
                "cross_pair_override": state.get("cross_pair_override") if isinstance(state, dict) else None,
                # Lifted from cross_pair_override when a Rule 4 confluence
                # boost drove the trade. Surfaces {source_pair, rho, bonus,
                # other_conf, window} at the top level for dashboard/analytics
                # consumers that don't want to unwrap the override dict.
                "confluence_source": (
                    (state.get("cross_pair_override") or {}).get("confluence_source")
                    if isinstance(state, dict) else None
                ),
                "book_confidence_modifier": book_mod,
                "brain_verdict": brain_verdict,
                "swap_id": trade.get("swap_id"),
            },
            "order_ref": {"order_userref": None, "order_id": None},
            "lifecycle": {
                "state": "PLACED",
                "vol_exec": 0.0,
                "avg_fill_price": None,
                "fee_quote": 0.0,
                "final_at": None,
                "terminal_reason": None,
                "exec_ids": [],
            },
        }

    def _place_order(self, pair: str, trade: dict, state: dict) -> bool:
        """Place an order via kraken-cli and write the initial journal entry.

        On success: returns True, writes a PLACED-state entry, and registers
        the order with self.execution_stream so subsequent WS events
        finalize its lifecycle asynchronously via _apply_execution_event.

        On any pre-exchange failure (ticker/validate/placement rejected):
        returns False, writes a terminal PLACEMENT_FAILED entry, and the
        caller rolls back the engine's pre-trade snapshot.

        Post-placement failures (post-only reject, DMS cancel, partial
        fills) are handled asynchronously by the execution stream — NOT
        here — on subsequent ticks.
        """
        if self.paper:
            return self._place_paper_order(pair, trade, state)

        amount = trade["amount"]
        action_upper = trade["action"].upper()
        action = action_upper.lower()
        entry = self._build_journal_entry(pair, trade, state)
        pre_trade_snap = state.get("_pre_trade_snapshot") if isinstance(state, dict) else None
        # PR-C / C2: persist snapshot on the journal so resume cancel/reject
        # can roll back even after process restart.
        if pre_trade_snap is not None:
            entry["pre_trade_snapshot"] = pre_trade_snap

        # ─── Real-balance preflight ─────────────────────────────────────
        # The engine sizes orders against its internal bookkeeping balance,
        # which may not reflect actual exchange holdings — especially for
        # non-USD-quoted pairs like SOL/BTC where the engine's BTC balance
        # is derived from a USD split, not real BTC on the account.
        # Check the actual currency balance before burning API calls.
        if action == "buy":
            quote = pair.split("/")[1]
            real_bal = self._get_real_quote_balance(quote)
            if real_bal is not None:
                cost_estimate = amount * (trade.get("price", 0) or 0)
                costmin = PositionSizer.MIN_COST.get(quote, 0.5)
                if real_bal < costmin or (cost_estimate > 0 and real_bal < cost_estimate):
                    is_usd = quote in STABLE_QUOTES
                    fmt = f"${real_bal:,.2f}" if is_usd else f"{real_bal:.8f}"
                    cost_fmt = f"${cost_estimate:,.2f}" if is_usd else f"{cost_estimate:.8f}"
                    engine = self.engines.get(pair)
                    # Post-v2.11.0 this path should be unreachable for non-
                    # stable-quoted pairs — the engine's `tradable` flag and
                    # _refresh_tradable_flags() combine to prevent sizing
                    # against a phantom balance. If we're here with
                    # tradable=True, it's a race with BalanceStream or a
                    # regression; surface sharply so it's easy to spot.
                    if engine is not None and getattr(engine, "tradable", True) and quote not in STABLE_QUOTES:
                        print(f"  [TRADE] Unexpected insufficient {quote} balance on "
                              f"tradable=True engine {pair} — likely BalanceStream race "
                              f"or regression. real={fmt} cost={cost_fmt}")
                    else:
                        print(f"  [TRADE] Insufficient {quote} balance ({fmt}) for {pair} "
                              f"BUY cost ~{cost_fmt} — skipping")
                    self._finalize_failed_entry(
                        entry, terminal_reason=f"insufficient_{quote}_balance",
                    )
                    return False
        elif action == "sell":
            base = pair.split("/")[0]
            real_base_bal = self._get_real_quote_balance(base)
            if real_base_bal is not None:
                min_size = PositionSizer.MIN_ORDER_SIZE.get(base, 0.02)
                if real_base_bal < min_size:
                    print(f"  [TRADE] Insufficient {base} balance "
                          f"({real_base_bal:.8f}) for {pair} SELL — "
                          f"below ordermin ({min_size}) — skipping")
                    self._finalize_failed_entry(
                        entry, terminal_reason=f"insufficient_{base}_balance",
                    )
                    return False
                if real_base_bal < amount:
                    print(f"  [TRADE] {pair} SELL: exchange {base} balance "
                          f"({real_base_bal:.8f}) < engine amount "
                          f"({amount:.8f}) — clamping to exchange balance")
                    # PR-C / C3: engine already sold full size optimistically —
                    # true-up to clamped amount at last close before place.
                    eng = self.engines.get(pair)
                    snap = pre_trade_snap
                    if eng is not None and snap is not None and real_base_bal > 0:
                        px = eng.prices[-1] if eng.prices else 0.0
                        if px > 0:
                            eng.true_up_fill(
                                side="SELL",
                                amount=float(real_base_bal),
                                fill_price=float(px),
                                pre_trade_snapshot=snap,
                                reason="SELL clamp to exchange base before place",
                            )
                    amount = real_base_bal
                    trade["amount"] = amount
                    entry["intent"]["amount"] = amount

        # ─── Ticker fetch (WS stream only — refuse to trade without live price) ───
        ticker = self.ticker_stream.latest_ticker(pair) if self.ticker_stream.healthy else None
        if not ticker or "bid" not in ticker:
            print(f"  [TRADE] TickerStream has no bid/ask for {pair} — refusing to trade")
            self._finalize_failed_entry(
                entry, terminal_reason="ticker_stream_unavailable",
            )
            return False

        limit_price = ticker["bid"] if action == "buy" else ticker["ask"]
        # Regime-gated BUY offset: rest BUY limits below the live bid in
        # downtrend/volatile regimes to wait for the dip instead of filling
        # on first touch. SELL stays at raw ask. See _BUY_LIMIT_OFFSET_BPS.
        offset_bps = 0
        if action == "buy":
            regime = state.get("regime") if isinstance(state, dict) else None
            limit_price, offset_bps = _apply_buy_limit_offset(pair, limit_price, regime)
        entry["intent"]["limit_price"] = limit_price
        entry["intent"]["buy_offset_bps"] = offset_bps

        # ─── Validate ───
        time.sleep(KRAKEN_REST_FLOOR_S)
        print(f"  [TRADE] Validating {action_upper} {amount:.8f} {pair} @ {limit_price} (post-only limit)...")
        if action == "buy":
            val_result = KrakenCLI.order_buy(pair, amount, price=limit_price, validate=True)
        else:
            val_result = KrakenCLI.order_sell(pair, amount, price=limit_price, validate=True)
        if "error" in val_result:
            print(f"  [TRADE] Validation failed: {val_result['error']}")
            self._finalize_failed_entry(
                entry, terminal_reason=f"validation_failed:{val_result['error']}",
            )
            return False

        # ─── Re-fetch ticker (price may have drifted during validate) ───
        fresh_ticker = self.ticker_stream.latest_ticker(pair) or {}
        if "error" not in fresh_ticker and "bid" in fresh_ticker:
            limit_price = fresh_ticker["bid"] if action == "buy" else fresh_ticker["ask"]
            if action == "buy":
                regime = state.get("regime") if isinstance(state, dict) else None
                limit_price, offset_bps = _apply_buy_limit_offset(pair, limit_price, regime)
                entry["intent"]["buy_offset_bps"] = offset_bps
            entry["intent"]["limit_price"] = limit_price

        # ─── Place for real ───
        # Kraken REST 2s floor: the validate call above is a distinct REST hit;
        # without spacing, validate→place fire ~network-latency apart (<2s) and
        # can trip Kraken's throttle/ban. Sleep the floor before placement.
        # (Mock-mode harness patches time.sleep to a no-op, so this is free in CI.)
        time.sleep(KRAKEN_REST_FLOOR_S)
        userref = self._next_userref()
        print(f"  [TRADE] Placing {action_upper} {amount:.8f} {pair} @ {limit_price} "
              f"(limit post-only, userref={userref})...")
        if action == "buy":
            result = KrakenCLI.order_buy(pair, amount, price=limit_price, userref=userref)
        else:
            result = KrakenCLI.order_sell(pair, amount, price=limit_price, userref=userref)

        if "error" in result:
            print(f"  [TRADE] FAILED: {result['error']}")
            self._finalize_failed_entry(
                entry, terminal_reason=f"placement_error:{result['error']}",
            )
            return False

        # ─── Accepted: extract order_id, register with stream, append PLACED ───
        order_id = result.get("txid", result.get("result", {}).get("txid", "unknown"))
        if isinstance(order_id, list):
            order_id = order_id[0] if order_id else "unknown"
        print(f"  [TRADE] PLACED: {action_upper} {amount:.8f} {pair} | order_id: {order_id}")

        entry["order_ref"] = {"order_userref": userref, "order_id": order_id}
        self.order_journal.append(entry)
        journal_index = len(self.order_journal) - 1

        # Register with the execution stream so WS events can finalize this
        # order's lifecycle on subsequent ticks. Orders that come back as
        # order_id='unknown' cannot be correlated by id; register() is a
        # no-op in that case and the entry will stay at PLACED until manual
        # audit (rare — Kraken almost always returns a txid on success).
        self.execution_stream.register(
            order_id=order_id, userref=userref, journal_index=journal_index,
            pair=pair, side=action_upper, placed_amount=amount,
            engine_ref=self.engines[pair],
            pre_trade_snapshot=pre_trade_snap,
        )
        return True

    def _place_paper_order(self, pair: str, trade: dict, state: dict) -> bool:
        """Place a paper-mode order.

        Offline --demo synthesizes a fill immediately (no kraken-cli).
        Paper mode with WSL uses `kraken paper` then synthesizes the same
        fill event. Journal + ExecutionStream path stays identical so live
        and paper share one lifecycle.
        """
        amount = trade["amount"]
        action_upper = trade["action"].upper()
        action = action_upper.lower()
        entry = self._build_journal_entry(pair, trade, state)

        if getattr(self, "demo", False):
            print(f"  [DEMO] Placing {action_upper} {amount:.8f} {pair} (synthetic fill)...")
        else:
            time.sleep(KRAKEN_REST_FLOOR_S)
            print(f"  [PAPER] Placing {action_upper} {amount:.8f} {pair} (paper limit)...")
            if action == "buy":
                result = KrakenCLI.paper_buy(pair, amount, order_type="limit")
            else:
                result = KrakenCLI.paper_sell(pair, amount, order_type="limit")
            if "error" in result:
                print(f"  [PAPER] FAILED: {result['error']}")
                self._finalize_failed_entry(
                    entry, terminal_reason=f"paper_failed:{result['error']}",
                )
                return False

        # Success — paper/demo fills at the requested limit_price. Append the
        # entry as PLACED first (so it has a journal index), then synthesize
        # a FILLED execution event for the stream to emit on drain.
        tag = "DEMO" if getattr(self, "demo", False) else "PAPER"
        print(f"  [{tag}] PLACED: {action_upper} {amount:.8f} {pair}")
        # Build a deterministic pseudo order_id for paper correlation.
        paper_order_id = f"{tag}-{int(time.time() * 1e6)}"
        paper_userref = self._next_userref()
        entry["order_ref"] = {"order_userref": paper_userref, "order_id": paper_order_id}
        self.order_journal.append(entry)
        journal_index = len(self.order_journal) - 1
        self.execution_stream.register(
            order_id=paper_order_id, userref=paper_userref, journal_index=journal_index,
            pair=pair, side=action_upper, placed_amount=amount,
            engine_ref=self.engines[pair],
            pre_trade_snapshot=state.get("_pre_trade_snapshot") if isinstance(state, dict) else None,
        )
        limit_price = entry["intent"]["limit_price"] or float(trade.get("price") or 0)
        # Paper fee-true (v2.27): inject Kraken base maker tier (16 bps) so
        # paper PnL matches live/backtest accounting instead of fee-free fills.
        notional = float(amount) * float(limit_price or 0.0)
        paper_fee = notional * 0.0016 if notional > 0 else 0.0
        synthetic_fill = {
            "exec_type": "trade",
            "exec_id": f"{paper_order_id}-fill",
            "order_id": paper_order_id,
            "order_status": "filled",
            "last_qty": amount,
            "last_price": limit_price,
            "cost": amount * limit_price,
            "fees": [{"qty": paper_fee}] if paper_fee > 0 else [],
            "order_userref": paper_userref,
            "side": action,
            "symbol": pair,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.execution_stream.inject_event(synthetic_fill)
        return True

    def _finalize_failed_entry(self, entry: Dict[str, Any], *, terminal_reason: str) -> None:
        """Patch a journal entry to PLACEMENT_FAILED and append it. Used
        for pre-exchange failures (ticker/validate/placement rejected)."""
        entry["lifecycle"] = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": 0.0,
            "final_at": datetime.now(timezone.utc).isoformat(),
            "terminal_reason": terminal_reason,
            "exec_ids": [],
        }
        self.order_journal.append(entry)

    def _reconcile_stale_placed(self):
        """Query exchange for PLACED journal entries that have no ExecutionStream
        registration — typically orders from a previous session that finalized
        while we were offline.

        For terminal orders (closed/canceled/expired): updates journal lifecycle
        directly. Engine rollback is NOT possible for entries from previous
        sessions (no pre_trade_snapshot persisted), so we log a warning.

        For still-open orders: registers them with the live ExecutionStream so
        WS events can finalize them normally.
        """
        # Collect PLACED entries with queryable order IDs
        stale = []
        for idx, entry in enumerate(self.order_journal):
            lifecycle = entry.get("lifecycle", {})
            if lifecycle.get("state") != "PLACED":
                continue
            order_id = entry.get("order_ref", {}).get("order_id")
            if not order_id or order_id == "unknown":
                continue
            stale.append((idx, entry, order_id))

        if not stale:
            return

        print(f"  [HYDRA] Reconciling {len(stale)} stale PLACED journal entries...")

        # Dedup order_ids (shouldn't have duplicates, but be safe)
        seen_ids = set()
        unique_stale = []
        for idx, entry, oid in stale:
            if oid not in seen_ids:
                seen_ids.add(oid)
                unique_stale.append((idx, entry, oid))

        # Batch query exchange
        BATCH = 20
        order_ids = [oid for _, _, oid in unique_stale]
        # Build lookup: order_id → (journal_index, entry)
        oid_to_entry = {oid: (idx, entry) for idx, entry, oid in unique_stale}

        reconciled = 0
        registered = 0
        for i in range(0, len(order_ids), BATCH):
            batch = order_ids[i:i + BATCH]
            time.sleep(2)  # Rate limit
            resp = KrakenCLI.query_orders(*batch, trades=True)
            if not isinstance(resp, dict) or "error" in resp:
                print(f"  [WARN] stale-placed query failed: {resp}")
                continue

            for txid, order_info in resp.items():
                if not isinstance(order_info, dict):
                    continue
                if txid not in oid_to_entry:
                    continue
                idx, entry = oid_to_entry[txid]
                status = order_info.get("status", "")

                if status in ("closed", "canceled", "expired"):
                    # Terminal — finalize journal entry
                    vol_exec = float(order_info.get("vol_exec", 0))
                    placed = entry.get("intent", {}).get("amount", 0)
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

                    entry["lifecycle"] = {
                        "state": state,
                        "vol_exec": vol_exec,
                        "avg_fill_price": avg_price,
                        "fee_quote": fee,
                        "final_at": order_info.get("closetm") or datetime.now(timezone.utc).isoformat(),
                        "terminal_reason": f"reconciled on resume ({status})",
                        "exec_ids": [],
                    }
                    pair = entry.get("pair", "?")
                    side = entry.get("side", "?")
                    print(f"  [HYDRA] {pair} {side} {txid}: {state} "
                          f"(vol={vol_exec:.8f}, reconciled on resume)")
                    if state in ("FILLED", "PARTIALLY_FILLED"):
                        # When engines were rebased from exchange balances,
                        # fees are already reflected in cash — only stamp
                        # fee_applied. Debit only when balances are still
                        # the optimistic snapshot / --balance fallback.
                        apply_debit = not getattr(
                            self, "_balances_from_exchange", False)
                        self._deduct_fill_fee(
                            self.engines.get(pair), entry,
                            apply_debit=apply_debit)

                    # Previous-session fills have no pre_trade_snapshot (not
                    # persisted). We use the arithmetic fallback in
                    # reconcile_partial_fill for PARTIALLY_FILLED, accepting
                    # minor avg_entry drift if the original trade was an
                    # average-in. For fully unfilled, log — operator verifies.
                    if state == "PARTIALLY_FILLED":
                        engine = self.engines.get(pair)
                        placed = float(entry.get("intent", {}).get("amount", 0) or 0)
                        limit_px = avg_price if avg_price else float(
                            entry.get("intent", {}).get("limit_price", 0) or 0
                        )
                        if engine and placed > 0 and limit_px > 0:
                            try:
                                engine.reconcile_partial_fill(
                                    side=side,
                                    placed_amount=placed,
                                    vol_exec=vol_exec,
                                    limit_price=limit_px,
                                    pre_trade_snapshot=None,
                                    reason=f"PARTIALLY_FILLED reconciled on resume ({txid})",
                                )
                                print(f"  [HYDRA] {pair} {side} engine adjusted "
                                      f"(arithmetic fallback; avg_entry may drift "
                                      f"slightly if original was an average-in)")
                            except Exception as e:
                                print(f"  [WARN] {pair} {side} partial-fill reconcile "
                                      f"failed ({e}); engine over-committed")
                    elif state in ("CANCELLED_UNFILLED", "REJECTED"):
                        print(f"  [WARN] {pair} {side} was never filled — engine position may be "
                              f"stale from snapshot. Operator should verify.")
                    reconciled += 1

                elif status in ("open", "pending", "pending_new", "new"):
                    # Still live on the exchange — register with ExecutionStream
                    # so the WS stream can finalize it normally.
                    pair = entry.get("pair", "")
                    side = entry.get("side", "")
                    userref = entry.get("order_ref", {}).get("order_userref")
                    placed_amount = entry.get("intent", {}).get("amount", 0)
                    engine = self.engines.get(pair)
                    if engine and pair:
                        self.execution_stream.register(
                            order_id=txid,
                            userref=userref,
                            journal_index=idx,
                            pair=pair,
                            side=side,
                            placed_amount=float(placed_amount),
                            engine_ref=engine,
                            pre_trade_snapshot=None,  # unavailable after restart
                        )
                        registered += 1
                        print(f"  [HYDRA] {pair} {side} {txid}: still open — "
                              f"registered with execution stream")

        parts = []
        if reconciled:
            parts.append(f"{reconciled} finalized")
        if registered:
            parts.append(f"{registered} re-registered")
        if parts:
            print(f"  [HYDRA] Stale PLACED reconciliation: {', '.join(parts)}")
        else:
            print(f"  [HYDRA] Stale PLACED reconciliation: all {len(stale)} entries still pending on exchange or query failed")

    def _apply_execution_event(self, event: Dict[str, Any]) -> None:
        """Apply one terminal event from the execution stream to the
        journal entry it came from AND the engine state. Called in the
        tick loop after drain_events()."""
        idx = event.get("journal_index")
        order_id = event.get("order_id")
        entry = None

        # Primary: try index if it's still valid and matches the order_id
        if isinstance(idx, int) and 0 <= idx < len(self.order_journal):
            candidate = self.order_journal[idx]
            cand_oid = candidate.get("order_ref", {}).get("order_id")
            if cand_oid == order_id:
                entry = candidate

        # Fallback: reverse-scan by order_id (handles journal trimming)
        if entry is None and order_id:
            for e in reversed(self.order_journal):
                if e.get("order_ref", {}).get("order_id") == order_id:
                    entry = e
                    break

        if entry is None:
            print(f"  [EXEC] journal entry not found for order_id={order_id} "
                  f"idx={idx} — event dropped")
            return
        state_name = event["state"]
        entry["lifecycle"] = {
            "state": state_name,
            "vol_exec": event["vol_exec"],
            "avg_fill_price": event.get("avg_fill_price"),
            "fee_quote": event.get("fee_quote") or 0.0,
            "final_at": event.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "terminal_reason": event.get("terminal_reason"),
            "exec_ids": event.get("exec_ids") or [],
        }

        engine = event.get("engine_ref")
        pre_snap = event.get("pre_trade_snapshot")
        pair = event.get("pair")
        side = event.get("side")
        placed_amount = event.get("placed_amount") or 0.0
        vol_exec = event.get("vol_exec") or 0.0

        if state_name == "FILLED":
            # PR-C / C1: true-up engine to exchange fill price (not candle close).
            fill_price = float(event.get("avg_fill_price") or 0.0)
            fill_amt = float(vol_exec) if vol_exec and vol_exec > 0 else float(placed_amount or 0.0)
            # Prefer event snapshot; fall back to journal-persisted (C2).
            snap = pre_snap
            if snap is None and isinstance(entry, dict):
                snap = entry.get("pre_trade_snapshot")
            if engine is not None and snap is not None and fill_price > 0 and fill_amt > 0:
                try:
                    ok = engine.true_up_fill(
                        side=side or "",
                        amount=fill_amt,
                        fill_price=fill_price,
                        pre_trade_snapshot=snap,
                        reason=f"FILLED true-up: {event.get('terminal_reason') or ''}",
                    )
                    if ok:
                        print(f"  [EXEC] {pair} {side} FILLED: true-up "
                              f"{fill_amt:.8f} @ {fill_price}")
                except Exception as e:
                    print(f"  [EXEC] {pair} {side} FILLED: true-up failed ({e}); "
                          f"fee still applied")
            self._deduct_fill_fee(engine, entry)
            return
        if state_name in ("CANCELLED_UNFILLED", "REJECTED"):
            snap = pre_snap
            if snap is None and isinstance(entry, dict):
                snap = entry.get("pre_trade_snapshot")
            if engine is not None and snap is not None:
                engine.restore_position(snap)
                print(f"  [EXEC] {pair} {side} {state_name}: engine rolled back "
                      f"(reason: {event.get('terminal_reason') or 'n/a'})")
            elif engine is not None and snap is None:
                print(f"  [EXEC] {pair} {side} {state_name}: no pre_trade_snapshot "
                      f"— engine may be stale (operator verify)")
            return
        if state_name == "PARTIALLY_FILLED":
            # Engine was optimistically committed to the full placed_amount;
            # actual fill was only vol_exec. true-up via reconcile_partial_fill.
            ratio = (vol_exec / placed_amount) if placed_amount > 0 else 0.0
            limit_price = float(event.get("avg_fill_price") or 0.0)
            if limit_price <= 0 and engine is not None and engine.prices:
                # Fallback when Kraken didn't report an avg_fill_price
                limit_price = engine.prices[-1]
            snap = pre_snap
            if snap is None and isinstance(entry, dict):
                snap = entry.get("pre_trade_snapshot")
            if engine is not None:
                try:
                    engine.reconcile_partial_fill(
                        side=side or "",
                        placed_amount=float(placed_amount),
                        vol_exec=float(vol_exec),
                        limit_price=limit_price,
                        pre_trade_snapshot=snap,
                        reason=f"PARTIALLY_FILLED: {event.get('terminal_reason') or ''}",
                    )
                    print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                          f"filled {vol_exec:.8f}/{placed_amount:.8f} ({ratio:.1%}) — "
                          f"engine reconciled to actual fill")
                except Exception as e:
                    print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                          f"reconcile failed ({e}); engine may be over-committed")
                # Fee is independent of reconcile success — terminal fill
                # still paid the exchange fee.
                self._deduct_fill_fee(engine, entry)
            else:
                print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                      f"filled {vol_exec:.8f}/{placed_amount:.8f} ({ratio:.1%}) — "
                      f"no engine_ref; journal carries truth but engine is stale")
            return

    def _deduct_fill_fee(self, engine, entry, *, apply_debit: bool = True) -> None:
        """Fee-true accounting (v2.27): debit the exchange fee from the
        engine's quote balance when a fill is confirmed. Pre-v2.27 the fee
        was journaled (lifecycle.fee_quote) but never hit engine equity, so
        live P&L was overstated by ~16 bps per fill vs the backtest, which
        has always deducted it (SimulatedFiller). Idempotent via the
        lifecycle.fee_applied flag — resume reconciliation may revisit an
        entry. Kill switch: HYDRA_FEE_DEDUCTION_DISABLED=1.

        apply_debit=False: stamp fee_applied without changing balance. Used
        when engines were rebased from exchange cash (fees already netted).
        """
        if os.environ.get("HYDRA_FEE_DEDUCTION_DISABLED") == "1":
            return
        if not isinstance(entry, dict):
            return
        lifecycle = entry.get("lifecycle")
        if not isinstance(lifecycle, dict) or lifecycle.get("fee_applied"):
            return
        try:
            fee = float(lifecycle.get("fee_quote") or 0.0)
        except (TypeError, ValueError):
            return
        if fee <= 0:
            return
        if apply_debit:
            if engine is None or not hasattr(engine, "balance"):
                return
            # Floor at zero: the exchange would have rejected an order whose fee
            # exceeded available funds, so a negative engine balance models an
            # impossible state (and would poison downstream sizing math).
            engine.balance = max(0.0, engine.balance - fee)
        lifecycle["fee_applied"] = True

    def _run_tuner_update(self):
        """Run Bayesian parameter update across all pair trackers."""
        for pair in self.pairs:
            tracker = self.trackers[pair]
            if len(tracker.observations) < 20:
                continue
            old_params = tracker.get_tunable_params()
            new_params = tracker.update()
            changes = tracker.get_changes_log(old_params)
            if changes:
                print(f"  [TUNER] {pair}: parameter update #{tracker.update_count}")
                for line in changes:
                    print(f"  [TUNER] {line}")
                # Apply to engine
                self.engines[pair].apply_tuned_params(new_params)
        self._completed_trades_since_update = 0

    def _execute_coordinated_swap(self, swap: dict, all_states: dict):
        """Execute a coordinated cross-pair swap (sell one pair, buy another).

        Generates two trades as an atomic unit with a shared swap_id.
        Executes the sell leg first, then the buy leg. If the buy leg cannot
        proceed after the sell has been placed on the exchange, the resting
        sell is cancelled so the swap is not left half-executed — the
        resulting CANCELLED_UNFILLED event rolls back the engine via
        _apply_execution_event. Pre-flight checks (buy_engine exists,
        buy_price > 0) run before the sell placement so common failures
        don't reach the exchange at all.
        """
        sell_pair = swap["sell_pair"]
        buy_pair = swap["buy_pair"]
        reason = swap["reason"]

        sell_state = all_states.get(sell_pair)
        buy_state = all_states.get(buy_pair)
        if not sell_state or not buy_state:
            print(f"  [SWAP] Cannot execute swap: missing state for {sell_pair} or {buy_pair}")
            return

        sell_engine = self.engines.get(sell_pair)
        if not sell_engine or sell_engine.position.size <= 0:
            print(f"  [SWAP] No position to sell on {sell_pair}, skipping swap")
            return

        # Pre-flight buy-leg checks — catch deterministic failures BEFORE
        # placing the sell so we don't leave an orphan sell on the exchange.
        buy_engine = self.engines.get(buy_pair)
        if not buy_engine:
            print(f"  [SWAP] No engine for {buy_pair}, skipping swap (pre-flight)")
            return
        if buy_state.get("price", 0) <= 0:
            print(f"  [SWAP] No price for {buy_pair}, skipping swap (pre-flight)")
            return

        self._swap_counter += 1
        swap_id = f"swap_{self._swap_counter}_{int(time.time())}"
        sell_amount = sell_engine.position.size
        sell_price = sell_state.get("price", 0)

        print(f"  [SWAP] Coordinated swap {swap_id}: SELL {sell_amount:.8f} {sell_pair} @ {sell_price} → BUY {buy_pair}")
        print(f"  [SWAP] Reason: {reason}")

        # Leg 1: Sell — update engine state first, then execute on exchange
        sell_snap = sell_engine.snapshot_position()
        sell_trade_obj = sell_engine.execute_signal(
            action="SELL", confidence=0.85,
            reason=f"[SWAP {swap_id}] Sell leg: {reason}",
            strategy=sell_state.get("strategy", "MOMENTUM"),
        )
        if not sell_trade_obj:
            print(f"  [SWAP] Engine rejected sell on {sell_pair}, skipping swap")
            return

        sell_trade = {
            "action": "SELL",
            "amount": sell_trade_obj.amount,
            "price": sell_trade_obj.price,
            "reason": sell_trade_obj.reason,
            "confidence": 0.85,
            "swap_id": swap_id,
        }
        sell_state["_pre_trade_snapshot"] = sell_snap
        if not self._place_order(sell_pair, sell_trade, sell_state):
            sell_engine.restore_position(sell_snap)
            print(f"  [ROLLBACK] {sell_pair}: engine state rolled back after failed swap sell")
            return

        # Capture the sell order_id so we can cancel it if the buy leg fails.
        # After a successful _place_order, the most recent journal entry is
        # ours (single-threaded tick loop). Matched by pair+side+swap_id to
        # defend against any unexpected ordering.
        sell_order_id: Optional[str] = None
        for entry in reversed(self.order_journal):
            if (entry.get("pair") == sell_pair
                    and entry.get("side") == "SELL"
                    and (entry.get("decision") or {}).get("swap_id") == swap_id):
                sell_order_id = (entry.get("order_ref") or {}).get("order_id")
                break

        def _cancel_orphan_sell(why: str) -> None:
            """Cancel the in-flight sell on exchange if the buy leg can't proceed.

            Engine rollback happens automatically when cancellation propagates
            through the execution stream as CANCELLED_UNFILLED (see
            _apply_execution_event). We don't restore the engine manually
            because the sell could have partially filled between placement
            and cancellation — the stream's terminal event carries the
            authoritative vol_exec.

            In paper mode the sell was filled synthetically at placement
            time, so there is nothing to cancel on the exchange — we just
            log the unbalanced swap so the operator can see it.
            """
            if self.paper:
                print(f"  [SWAP] WARNING: paper sell already synthesized as filled; "
                      f"swap {swap_id} half-executed ({why})")
                return
            if not sell_order_id or sell_order_id == "unknown":
                print(f"  [SWAP] WARNING: no order_id captured for sell leg; "
                      f"cannot cancel orphan ({why})")
                return
            try:
                time.sleep(2)  # rate limit
                cancel_result = KrakenCLI.cancel_order(sell_order_id)
                if isinstance(cancel_result, dict) and "error" in cancel_result:
                    print(f"  [SWAP] WARNING: cancel orphan sell {sell_order_id} "
                          f"failed: {cancel_result['error']} ({why}). "
                          f"Sell may have filled before cancel; check journal.")
                else:
                    print(f"  [SWAP] Cancelled orphan sell {sell_order_id} ({why}). "
                          f"Engine rollback will complete when CANCELLED_UNFILLED "
                          f"event drains.")
            except Exception as e:
                print(f"  [SWAP] WARNING: cancel orphan sell {sell_order_id} "
                      f"raised {type(e).__name__}: {e} ({why})")

        # Leg 2: Buy on the target pair. Re-read price in case it drifted
        # during the sell placement's rate-limit sleeps.
        buy_price = buy_state.get("price", 0)
        if buy_price <= 0:
            _cancel_orphan_sell("buy price disappeared after sell placement")
            return

        # Engine sizes the buy via Kelly criterion — execute_signal handles
        # position sizing, balance check, and minimum order enforcement internally.
        if self._should_block_buy_for_portfolio_dd(self._portfolio_buy_halted, "BUY"):
            _cancel_orphan_sell("portfolio circuit breaker blocks BUY leg")
            return
        buy_snap = buy_engine.snapshot_position()
        buy_trade_obj = buy_engine.execute_signal(
            action="BUY", confidence=0.85,
            reason=f"[SWAP {swap_id}] Buy leg: {reason}",
            strategy=buy_state.get("strategy", "MOMENTUM"),
        )
        if not buy_trade_obj:
            _cancel_orphan_sell("engine rejected buy (halted or insufficient balance)")
            return

        # Use the engine's actual executed amount for the exchange order
        buy_trade = {
            "action": "BUY",
            "amount": buy_trade_obj.amount,
            "price": buy_trade_obj.price,
            "reason": buy_trade_obj.reason,
            "confidence": 0.85,
            "swap_id": swap_id,
        }
        buy_state["_pre_trade_snapshot"] = buy_snap
        if not self._place_order(buy_pair, buy_trade, buy_state):
            buy_engine.restore_position(buy_snap)
            print(f"  [ROLLBACK] {buy_pair}: engine state rolled back after failed swap buy")
            _cancel_orphan_sell("_place_order failed for buy leg")
            return

        # Both legs placed — the swap_id tag on each leg's journal entry
        # is how callers link them back together. No separate marker row.
        print(f"  [SWAP] Swap {swap_id} placed (both legs; lifecycle via execution stream)")

    def _log_regime_transitions(self, all_states: Dict[str, dict]):
        """Log regime transitions across pairs for observability.
        Actionable cross-pair overrides are handled by CrossPairCoordinator in Phase 1.5.
        """
        for pair, state in all_states.items():
            current_regime = state.get("regime", "RANGING")
            prev_regime = self.prev_regimes.get(pair)

            if prev_regime and prev_regime != current_regime:
                print(f"  [REGIME] {pair}: {prev_regime} -> {current_regime}")

                # Opportunistic cross-pair logic. Both rules key off the
                # stable-quoted SOL pair and reference the bridge — exactly
                # the roles bound by `self.triangle`. Skip when triangle
                # is partial (single-pair tests, partial backtests) or
                # missing (object.__new__-bypass tests).
                triangle = getattr(self, "triangle", None)
                if triangle is not None:
                    sol_stable_key = triangle.stable_sol.cli_format
                    bridge_key = triangle.bridge.cli_format
                    quote_label = triangle.quote
                    # If stable_sol shifts to TREND_DOWN and we hold SOL,
                    # consider selling SOL for BTC.
                    if pair == sol_stable_key and current_regime == "TREND_DOWN":
                        if bridge_key in all_states:
                            btc_regime = all_states[bridge_key].get("regime")
                            if btc_regime in ("TREND_UP", "RANGING"):
                                print(f"  [REGIME] Cross-pair opportunity: SOL weakening vs {quote_label} but "
                                      f"{bridge_key} is {btc_regime} — consider selling SOL for BTC")
                    # If stable_sol shifts to TREND_UP, MOMENTUM is active.
                    if pair == sol_stable_key and current_regime == "TREND_UP":
                        print(f"  [REGIME] SOL trending up — MOMENTUM strategy active")

            self.prev_regimes[pair] = current_regime

    def _set_engine_balances(self, per_pair_usd: float):
        """Set engine balances and the per-engine `tradable` flag.

        Stable-quoted pairs (USD/USDC/USDT) get a 1/N slice of the tradable USD balance.

        Non-USD-quoted pairs (e.g. SOL/BTC) previously received a USD→quote
        converted slice, which produced a "phantom" balance when the account
        held none of the quote currency. That phantom balance caused the
        engine to size and attempt orders it could never actually place,
        triggering a loop of `PLACEMENT_FAILED: insufficient_{quote}_balance`
        entries (see v2.11.0 CHANGELOG).

        Fixed policy:
          • Balance = real exchange holding of the quote currency (not a
            USD-derived estimate).
          • `tradable = True` iff the real holding exceeds costmin for that
            quote — otherwise the engine is `tradable=False` (signal still
            generated for Rule 4 confluence, but no Trade is ever produced).

        When an engine already holds a position (e.g. from --resume), we set
        initial_balance = cash + position_value so that P&L starts at 0% from
        the point of the balance reset, rather than showing a bogus gain from
        the position being valued against a tiny converted initial balance.
        """
        prices = self._get_asset_prices()

        # Per-quote pools (live only): each stable-quoted engine is funded
        # from the REAL holding of its own quote currency, split across the
        # pairs sharing that quote. A uniform "every stable = $1" slice let
        # a USDC-quoted engine size orders against USD it cannot spend
        # (phantom balance → PLACEMENT_FAILED loop) the moment the universe
        # mixes quotes (--pairs auto). Paper keeps the uniform split — it
        # simulates strategy, not funding. Fails soft to the uniform slice
        # when no balance data is available yet.
        stable_quote_counts: Dict[str, int] = {}
        for p in self.pairs:
            q = p.split("/")[1]
            if q in STABLE_QUOTES:
                stable_quote_counts[q] = stable_quote_counts.get(q, 0) + 1

        def _stable_slice(q: str) -> float:
            if self.paper:
                return per_pair_usd
            pool = self._get_real_quote_balance(q)
            if pool is None:
                return per_pair_usd  # no balance data yet — legacy behavior
            n = stable_quote_counts.get(q, 1)
            return pool / n if n else 0.0

        for pair in self.pairs:
            engine = self.engines[pair]
            quote = pair.split("/")[1]
            current_price = engine.prices[-1] if engine.prices else 0
            if quote in STABLE_QUOTES:
                slice_quote = _stable_slice(quote)
                equity = slice_quote + engine.position.size * current_price
                engine.balance = slice_quote
                engine.initial_balance = equity
                # PR-B / B3: never rebase peak downward on re-seed / resume.
                engine.peak_equity = max(float(engine.peak_equity or 0.0), equity)
                # Always tradable: with a zero pool the sizer sizes entries
                # to 0 (balance < costmin) but held inventory stays sellable.
                engine.tradable = True
                continue

            # Non-stable-quoted pairs (the SOL/BTC bridge) are signal-only
            # by default: on real 1h tape the bridge produced zero trades in
            # 1y and its only 2y trade lost money while dragging portfolio
            # Sharpe from -0.63 to -0.99 (isolation study, fees-on realistic
            # fills; evidence in .hydra-flywheel/bridge_isolation.json).
            # Its candles still stream for cross-pair confluence and the
            # synthetic derivatives signal. Existing inventory drains via
            # exit_only (SELLs flow, BUYs refused) so a held bridge position
            # is never stranded. Opt back in: HYDRA_BRIDGE_TRADING=1.
            if os.environ.get("HYDRA_BRIDGE_TRADING") != "1":
                engine.exit_only = True
                has_position = engine.position.size > 0
                equity = engine.position.size * current_price
                engine.balance = 0.0
                engine.initial_balance = equity if equity > 0 else 0.0
                engine.peak_equity = max(
                    float(engine.peak_equity or 0.0), engine.initial_balance,
                )
                engine.tradable = has_position  # exit path stays open until flat
                state_label = "draining position" if has_position else "signal-only"
                print(f"  [HYDRA] {pair}: {state_label} — bridge trading off "
                      f"(HYDRA_BRIDGE_TRADING=1 to trade the bridge)")
                continue
            engine.exit_only = False

            # Paper mode: keep the legacy USD→quote conversion so strategy
            # simulations are not artificially gated by on-account holdings.
            # Paper users are testing the strategy, not funding constraints.
            if self.paper:
                if quote in prices and prices[quote] > 0:
                    balance_quote = per_pair_usd / prices[quote]
                    equity = balance_quote + engine.position.size * current_price
                    engine.balance = balance_quote
                    engine.initial_balance = equity
                    engine.peak_equity = max(float(engine.peak_equity or 0.0), equity)
                else:
                    equity = per_pair_usd + engine.position.size * current_price
                    engine.balance = per_pair_usd
                    engine.initial_balance = equity
                    engine.peak_equity = max(float(engine.peak_equity or 0.0), equity)
                engine.tradable = True
                continue

            # Live mode, non-USD quote: use the real exchange balance.
            real_quote = self._get_real_quote_balance(quote) or 0.0
            costmin = PositionSizer.MIN_COST.get(quote, 0.0)
            if real_quote > costmin:
                equity = real_quote + engine.position.size * current_price
                engine.balance = real_quote
                engine.initial_balance = equity
                engine.peak_equity = max(float(engine.peak_equity or 0.0), equity)
                engine.tradable = True
                print(f"  [HYDRA] {pair}: tradable — real balance {real_quote:.8f} {quote} "
                      f"(equity {equity:.8f})")
            else:
                # Informational-only: engine ticks normally, surfaces
                # regime + signal for confluence, but _maybe_execute
                # short-circuits so no placement is attempted.
                equity = engine.position.size * current_price
                engine.balance = 0.0
                engine.initial_balance = equity if equity > 0 else 0.0
                engine.peak_equity = max(
                    float(engine.peak_equity or 0.0),
                    engine.initial_balance,
                )
                engine.tradable = False
                print(f"  [HYDRA] {pair}: informational-only — no {quote} held "
                      f"(balance {real_quote:.8f}, costmin {costmin})")

    def _refresh_tradable_flags(self) -> None:
        """Re-evaluate the `tradable` flag for every engine once per tick.

        Cheap: reads the latest BalanceStream snapshot (push-based, no
        REST call). Transitions are logged exactly once (False→True and
        True→False). When a pair flips False→True — e.g. a stable_btc BUY
        just filled, so we now hold BTC — the engine's balance and equity
        baseline are re-seeded from the real holding so its circuit
        breaker and P&L calculations start clean from that point.

        Stable-quoted pairs (USD/USDC/USDT) are skipped because their
        tradability depends on the shared tradable USD pool, not on
        holding a specific currency.
        """
        for pair in self.pairs:
            engine = self.engines[pair]
            quote = pair.split("/")[1]
            if quote in STABLE_QUOTES:
                if not engine.tradable:
                    # Stable-quoted pairs should never be informational-only;
                    # if they somehow are, re-enable them. Balance unchanged.
                    engine.tradable = True
                continue
            if os.environ.get("HYDRA_BRIDGE_TRADING") != "1":
                # Bridge stays in drain mode (see _set_engine_balances):
                # tradable only while a position remains, never re-activated
                # just because BTC landed in the account.
                engine.exit_only = True
                has_position = engine.position.size > 0
                if engine.tradable and not has_position:
                    engine.balance = 0.0
                    engine.tradable = False
                    print(f"  [HYDRA] {pair}: bridge flat — now signal-only "
                          f"(HYDRA_BRIDGE_TRADING=1 to trade the bridge)")
                elif has_position and not engine.tradable:
                    engine.tradable = True  # resume surfaced inventory: drain it
                continue
            engine.exit_only = False
            real_quote = self._get_real_quote_balance(quote) or 0.0
            costmin = PositionSizer.MIN_COST.get(quote, 0.0)
            should_be_tradable = real_quote > costmin
            if should_be_tradable and not engine.tradable:
                current_price = engine.prices[-1] if engine.prices else 0
                equity = real_quote + engine.position.size * current_price
                engine.balance = real_quote
                engine.initial_balance = equity
                # PR-B / B3: preserve historical peak across re-activation.
                engine.peak_equity = max(float(engine.peak_equity or 0.0), equity)
                # Do NOT zero max_drawdown — economic peak memory must remain.
                engine.equity_history = []
                engine.tradable = True
                print(f"  [HYDRA] {pair}: ACTIVATED — real {quote} balance "
                      f"{real_quote:.8f} available (equity {equity:.8f})")
            elif not should_be_tradable and engine.tradable:
                engine.balance = 0.0
                engine.tradable = False
                print(f"  [HYDRA] {pair}: DEACTIVATED — {quote} balance depleted "
                      f"({real_quote:.8f} < costmin {costmin})")

    def _get_asset_prices(self) -> dict:
        """Get current USD prices for known assets from engine state.
        Returns {canonical_asset: usd_price}."""
        # Seed unit-prices for every supported stable quote (any unit value
        # in STABLE_QUOTES is by definition $1 for portfolio valuation).
        prices = {q: 1.0 for q in STABLE_QUOTES}
        for pair, engine in self.engines.items():
            if engine.prices:
                base, quote = pair.split("/")
                if quote in STABLE_QUOTES:
                    prices[base] = engine.prices[-1]
        # Derive BTC price from the bridge if no stable_btc pair available.
        # getattr guards tests that instantiate via object.__new__(HydraAgent)
        # (bypasses __init__ and therefore never sets self.triangle).
        triangle = getattr(self, "triangle", None)
        if "BTC" not in prices and "SOL" in prices:
            bridge_key = (triangle.bridge.cli_format if triangle is not None
                          else "SOL/BTC")  # static fallback for triangle-less tests
            bridge_engine = self.engines.get(bridge_key)
            if bridge_engine and bridge_engine.prices:
                sol_per_btc = bridge_engine.prices[-1]
                if sol_per_btc > 0:
                    prices["BTC"] = prices["SOL"] / sol_per_btc
        return prices

    def _get_real_quote_balance(self, quote: str) -> Optional[float]:
        """Return the actual exchange balance for a quote currency.

        Prefers the real-time BalanceStream; falls back to the cached REST
        balance from startup.  Returns None only if no balance data is
        available at all (should not happen after warmup).
        """
        bal = None
        if not self.paper and self.balance_stream.healthy:
            bal = self.balance_stream.latest_balances()
        if not bal:
            bal = getattr(self, "_cached_balance", None)
        if not bal:
            return None
        # Sum all non-staked holdings that normalize to the quote currency.
        total = 0.0
        for asset, amount in bal.items():
            if KrakenCLI._is_staked(asset):
                continue
            if KrakenCLI._normalize_asset(asset) == quote:
                total += amount
        return total

    def _extract_fee_tier(self, vol_response: dict) -> dict:
        """Normalize a `kraken volume` response into a compact fee-tier dict.

        Returns {"volume_30d_usd": float|None, "pair_fees": {friendly_pair: {"maker_pct","taker_pct"}}}.
        Defensive: any missing / malformed sub-field is silently coerced to None
        instead of raising, so a transient Kraken shape change cannot crash the tick.
        """
        result = {"volume_30d_usd": None, "pair_fees": {}}
        if not isinstance(vol_response, dict):
            return result
        try:
            v = vol_response.get("volume")
            if v is not None:
                result["volume_30d_usd"] = float(v)
        except (TypeError, ValueError) as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        fees_taker = vol_response.get("fees") or {}
        fees_maker = vol_response.get("fees_maker") or {}
        if not isinstance(fees_taker, dict):
            fees_taker = {}
        if not isinstance(fees_maker, dict):
            fees_maker = {}
        # Kraken may return fee keys in several forms ("SOLUSD", "SOL/USD", "BTCUSD",
        # "XXBTZUSD" historically). The PairRegistry already knows every alias
        # dialect, so we just resolve raw_key → friendly via the registry,
        # falling back to raw_key for unknown pairs. A None resolution is
        # logged once per raw_key per session to surface API drift early
        # (e.g. Kraken adds a new pair format, or our registry is stale).
        seen_keys = set(fees_taker.keys()) | set(fees_maker.keys())
        unresolved_seen = getattr(self, "_unresolved_fee_keys_logged", None)
        if unresolved_seen is None:
            unresolved_seen = set()
            try:
                self._unresolved_fee_keys_logged = unresolved_seen
            except Exception:
                pass  # object.__new__() bypass test path; non-fatal
        for raw_key in seen_keys:
            resolved = KrakenCLI.registry.get(raw_key)
            if resolved is None and raw_key not in unresolved_seen:
                unresolved_seen.add(raw_key)
                print(f"  [FEE-TIER] Unrecognized Kraken fee-key {raw_key!r} — "
                      f"registry alias dictionary may be stale.")
            friendly = resolved.cli_format if resolved else raw_key
            taker_entry = fees_taker.get(raw_key) or {}
            maker_entry = fees_maker.get(raw_key) or {}
            taker_pct = None
            maker_pct = None
            if isinstance(taker_entry, dict):
                try:
                    val = taker_entry.get("fee")
                    if val is not None:
                        taker_pct = float(val)
                except (TypeError, ValueError):
                    taker_pct = None
            if isinstance(maker_entry, dict):
                try:
                    val = maker_entry.get("fee")
                    if val is not None:
                        maker_pct = float(val)
                except (TypeError, ValueError):
                    maker_pct = None
            result["pair_fees"][friendly] = {"maker_pct": maker_pct, "taker_pct": taker_pct}
        return result

    def _compute_balance_usd(self, balance: dict) -> dict:
        """Convert raw Kraken balance to USD breakdown with staked asset handling.

        Returns {
            "total_usd": float,      # All assets in USD
            "tradable_usd": float,   # Only tradable (non-staked) assets
            "staked_usd": float,     # Staked/bonded/locked assets
            "assets": [{"asset": str, "amount": float, "usd_value": float, "staked": bool}, ...]
        }
        """
        prices = self._get_asset_prices()
        assets = []
        total_usd = 0.0
        tradable_usd = 0.0
        staked_usd = 0.0

        for asset, amount in balance.items():
            staked = KrakenCLI._is_staked(asset)
            canonical = KrakenCLI._normalize_asset(asset)
            usd_price = prices.get(canonical, 0.0)
            usd_value = amount * usd_price

            assets.append({
                "asset": asset,
                "canonical": canonical,
                "amount": round(amount, 8),
                "usd_value": round(usd_value, 2),
                "staked": staked,
            })
            total_usd += usd_value
            if staked:
                staked_usd += usd_value
            else:
                tradable_usd += usd_value

        # Sort: tradable first, then staked; within each group alphabetical
        assets.sort(key=lambda a: (a["staked"], a["asset"]))

        return {
            "total_usd": round(total_usd, 2),
            "tradable_usd": round(tradable_usd, 2),
            "staked_usd": round(staked_usd, 2),
            "assets": assets,
        }

    def _build_dashboard_state(self, tick: int, all_states: dict,
                                elapsed: float) -> dict:
        """Build the full state dict for the dashboard WebSocket."""
        # Balance: prefer WS stream when healthy (real-time, no API call).
        # When the stream is unhealthy, hold the last cached balance — no REST
        # fallback (Hydra policy: no REST for market/account polling).
        ws_bal = (
            self.balance_stream.latest_balances()
            if not self.paper and self.balance_stream.healthy
            else None
        )
        if ws_bal:
            self._cached_balance = ws_bal
        else:
            pass  # Use startup-cached balance until WS reconnects
        bal = getattr(self, '_cached_balance', {})

        # Fee tier refresh — at most once per hour, live mode only (paper has no fee data).
        # On failure we leave the cache stale and do NOT advance _fee_tier_fetched_at,
        # so the next tick will retry. Diagnostic-only: has no effect on trading.
        if not self.paper:
            now = time.time()
            if now - self._fee_tier_fetched_at > 3600:
                time.sleep(2)  # Rate limit
                vol = KrakenCLI.volume(self.pairs)
                if isinstance(vol, dict) and "error" not in vol:
                    self._fee_tier_cache = self._extract_fee_tier(vol)
                    self._fee_tier_fetched_at = now
                else:
                    err = vol.get("error") if isinstance(vol, dict) else str(vol)
                    print(f"  [FEES] volume fetch failed: {err}")

        # Compute USD-equivalent balance breakdown
        balance_usd = self._compute_balance_usd(bal) if bal else {
            "total_usd": 0, "tradable_usd": 0, "staked_usd": 0, "assets": []
        }

        # v2.16.2: portfolio-level drawdown tracking. Uses total_usd so a
        # coordinated dip across all pairs (or a forex swing) is captured
        # rather than buried under the max-of-per-pair aggregation the
        # dashboard used to do. Zero totals (balance fetch failed) are
        # skipped so we never register a spurious 100% drawdown.
        total_usd_live = float(balance_usd.get("total_usd") or 0.0)
        if total_usd_live > 0:
            if total_usd_live > self._portfolio_peak_usd:
                self._portfolio_peak_usd = total_usd_live
            if self._portfolio_peak_usd > 0:
                cur_dd = ((self._portfolio_peak_usd - total_usd_live) /
                          self._portfolio_peak_usd * 100.0)
                self._portfolio_current_drawdown_pct = round(cur_dd, 4)
                if cur_dd > self._portfolio_max_drawdown_pct:
                    self._portfolio_max_drawdown_pct = cur_dd
                # PR-B / B4: sticky portfolio BUY halt at 15% max DD.
                _pcb = float(getattr(self, "PORTFOLIO_CIRCUIT_BREAKER_PCT", 15.0))
                if self._portfolio_max_drawdown_pct >= _pcb:
                    if not getattr(self, "_portfolio_buy_halted", False):
                        print(
                            f"  [PORTFOLIO CB] max DD "
                            f"{self._portfolio_max_drawdown_pct:.1f}% ≥ "
                            f"{_pcb:.0f}% — "
                            f"blocking new BUYs (SELL still allowed)"
                        )
                    self._portfolio_buy_halted = True

        pairs_data = {}
        for pair, state in all_states.items():
            pairs_data[pair] = state
            # Per-pair tradable flag — dashboard renders an INFO-ONLY
            # badge when False. Defaults to True if the engine is
            # missing (defensive: should not happen).
            engine = self.engines.get(pair)
            if state is not None:
                state["tradable"] = bool(getattr(engine, "tradable", True)) if engine else True

        # Journal-derived stats — wrapped in try/except so a malformed journal
        # entry can never crash the broadcast and blank the dashboard.
        journal_stats: Dict[str, Any] = {
            "total_fills": 0, "fills_by_pair": {}, "fill_win_rate": 0,
            "pnl_by_pair": {}, "total_realized_pnl_usd": 0,
            "total_unrealized_pnl_usd": 0, "total_pnl_usd": 0,
            # v2.20.0 — Hydra-only variants (dashboard toggle "Hydra-only ON"
            # excludes source='kraken_backfill', i.e. trades NOT placed by
            # Hydra). Toggle OFF reads the base fields above (full history).
            "total_fills_hydra_only": 0, "fill_win_rate_hydra_only": 0,
            "total_realized_pnl_usd_hydra_only": 0,
            "total_pnl_usd_hydra_only": 0,
        }
        try:
            _FILL_STATES = ("FILLED", "PARTIALLY_FILLED")

            def _is_hydra_only(entry: dict) -> bool:
                """Hydra-placed trades have source != 'kraken_backfill'."""
                return (entry.get("source") or "") != "kraken_backfill"

            # Single pass populates BOTH the full-history and hydra-only views
            # in lockstep. fills_by_pair is full-history (right-sidebar
            # per-pair cards never filter); the *_hydra_only counters branch
            # off the same loop with the source filter applied.
            fills_by_pair: Dict[str, Dict[str, Any]] = {}
            total_fills = 0
            total_fills_hydra_only = 0
            _buy_cost: Dict[str, float] = {}
            _buy_qty: Dict[str, float] = {}
            _buy_cost_h: Dict[str, float] = {}
            _buy_qty_h: Dict[str, float] = {}
            sell_wins_h = 0
            sell_losses_h = 0
            for entry in self.order_journal:
                lc = entry.get("lifecycle") or {}
                if lc.get("state") not in _FILL_STATES:
                    continue
                total_fills += 1
                p = entry.get("pair", "")
                if p not in fills_by_pair:
                    fills_by_pair[p] = {"buys": 0, "sells": 0, "sell_wins": 0, "sell_losses": 0}
                side = entry.get("side")
                vol = float(lc.get("vol_exec") or 0)
                price = float(lc.get("avg_fill_price") or (entry.get("intent") or {}).get("limit_price") or 0)
                hydra_only = _is_hydra_only(entry)
                if hydra_only:
                    total_fills_hydra_only += 1
                if side == "BUY":
                    fills_by_pair[p]["buys"] += 1
                    _buy_cost[p] = _buy_cost.get(p, 0) + vol * price
                    _buy_qty[p] = _buy_qty.get(p, 0) + vol
                    if hydra_only:
                        _buy_cost_h[p] = _buy_cost_h.get(p, 0) + vol * price
                        _buy_qty_h[p] = _buy_qty_h.get(p, 0) + vol
                elif side == "SELL":
                    fills_by_pair[p]["sells"] += 1
                    avg_buy = (_buy_cost.get(p, 0) / _buy_qty[p]) if _buy_qty.get(p, 0) > 0 else 0
                    if avg_buy > 0 and price > 0:
                        if price >= avg_buy:
                            fills_by_pair[p]["sell_wins"] += 1
                        else:
                            fills_by_pair[p]["sell_losses"] += 1
                    sold_cost = vol * avg_buy if avg_buy > 0 else 0
                    _buy_cost[p] = max(0.0, _buy_cost.get(p, 0) - sold_cost)
                    _buy_qty[p] = max(0.0, _buy_qty.get(p, 0) - vol)
                    if hydra_only:
                        avg_buy_h = (_buy_cost_h.get(p, 0) / _buy_qty_h[p]) if _buy_qty_h.get(p, 0) > 0 else 0
                        if avg_buy_h > 0 and price > 0:
                            if price >= avg_buy_h:
                                sell_wins_h += 1
                            else:
                                sell_losses_h += 1
                        sold_cost_h = vol * avg_buy_h if avg_buy_h > 0 else 0
                        _buy_cost_h[p] = max(0.0, _buy_cost_h.get(p, 0) - sold_cost_h)
                        _buy_qty_h[p] = max(0.0, _buy_qty_h.get(p, 0) - vol)
            total_sell_wins = sum(v.get("sell_wins", 0) for v in fills_by_pair.values())
            total_sell_losses = sum(v.get("sell_losses", 0) for v in fills_by_pair.values())
            total_sells = total_sell_wins + total_sell_losses
            fill_win_rate = round(total_sell_wins / total_sells * 100, 2) if total_sells > 0 else 0
            total_sells_h = sell_wins_h + sell_losses_h
            fill_win_rate_hydra_only = round(sell_wins_h / total_sells_h * 100, 2) if total_sells_h > 0 else 0

            asset_prices = self._get_asset_prices()
            total_realized_pnl_usd = 0.0
            total_unrealized_pnl_usd = 0.0
            total_realized_pnl_usd_h = 0.0
            pnl_by_pair: Dict[str, Dict[str, float]] = {}
            for pair in self.pairs:
                # Full-history realized for both the right-sidebar per-pair
                # card and the "all trades" top P&L view.
                realized_full = self._compute_pair_realized_pnl(pair, hydra_only=False)
                # Hydra-only realized for the toggle-on top P&L view.
                realized_h = self._compute_pair_realized_pnl(pair, hydra_only=True)
                engine = self.engines.get(pair)
                ep = engine.prices[-1] if engine and engine.prices else 0
                unrealized = (engine.position.size * (ep - engine.position.avg_entry)
                              if engine and engine.position.size > 0 else 0)
                quote = pair.split("/")[1] if "/" in pair else "USD"
                quote_usd = asset_prices.get(quote, 1.0)
                pnl_by_pair[pair] = {
                    "realized": round(realized_full, 8),
                    "unrealized": round(unrealized, 8),
                    "net": round(realized_full + unrealized, 8),
                    "net_usd": round((realized_full + unrealized) * quote_usd, 2),
                }
                total_realized_pnl_usd += realized_full * quote_usd
                total_realized_pnl_usd_h += realized_h * quote_usd
                total_unrealized_pnl_usd += unrealized * quote_usd
            total_pnl_usd = total_realized_pnl_usd + total_unrealized_pnl_usd
            total_pnl_usd_h = total_realized_pnl_usd_h + total_unrealized_pnl_usd

            journal_stats = {
                "total_fills": total_fills,
                "fills_by_pair": fills_by_pair,
                "fill_win_rate": fill_win_rate,
                "pnl_by_pair": pnl_by_pair,
                "total_realized_pnl_usd": round(total_realized_pnl_usd, 2),
                "total_unrealized_pnl_usd": round(total_unrealized_pnl_usd, 2),
                "total_pnl_usd": round(total_pnl_usd, 2),
                # v2.20.0 — Hydra-only variants for the dashboard toggle.
                # excludes journal entries with source='kraken_backfill'.
                "total_fills_hydra_only": total_fills_hydra_only,
                "fill_win_rate_hydra_only": fill_win_rate_hydra_only,
                "total_realized_pnl_usd_hydra_only": round(total_realized_pnl_usd_h, 2),
                "total_pnl_usd_hydra_only": round(total_pnl_usd_h, 2),
            }
        except Exception as e:
            print(f"  [WARN] journal_stats computation failed: {type(e).__name__}: {e}")

        return {
            "type": "state_update",
            "tick": tick,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(elapsed, 1),
            "remaining": 0 if self.duration == 0 else round(self.duration - elapsed, 1),
            "balance": bal if "error" not in bal else {},
            "balance_usd": balance_usd,
            "portfolio_drawdown": {
                "peak_usd": round(self._portfolio_peak_usd, 2),
                "current_pct": round(self._portfolio_current_drawdown_pct, 4),
                "max_pct": round(self._portfolio_max_drawdown_pct, 4),
            },
            "fee_tier": self._fee_tier_cache,
            "pairs": pairs_data,
            "order_journal": self.order_journal[-20:],
            "journal_stats": journal_stats,
            "running": self.running,
            "interval": self.interval,
            "mode": self.mode,
            "ai_brain": self.brain.get_stats() if self.brain else None,
        }

    def _print_tick_status(self, pair: str, state: dict):
        """Print concise tick status."""
        s = state["signal"]
        p = state["portfolio"]
        pos = state["position"]
        quote = pair.split("/")[1].upper() if "/" in pair else ""
        is_usd = quote in STABLE_QUOTES
        cur = "$" if is_usd else ""
        # BTC-quoted pairs trade at ~0.001–0.01; 4 decimals loses precision
        # (SOL/BTC ~0.00148 would render as "0.0015"). Use 8 decimals for
        # crypto-quoted pairs, 4 for stable-quoted pairs.
        pd = 4 if is_usd else 8

        signal_icon = {"BUY": "^", "SELL": "v", "HOLD": "-"}.get(s["action"], "?")

        print(f"  | {pair:<10} | {cur}{state['price']:>12,.{pd}f} | "
              f"{state['regime']:<10} -> {state['strategy']:<15} | "
              f"{signal_icon} {s['action']:<4} ({s['confidence']:.2f}) | "
              f"Eq: {cur}{p['equity']:>10,.{2 if is_usd else 8}f} | "
              f"P&L: {p['pnl_pct']:>+.2f}% | DD: {p['max_drawdown_pct']:.1f}%")

        if pos["size"] > 0:
            print(f"  |            | Pos: {pos['size']:.8f} @ {cur}{pos['avg_entry']:,.{pd}f} | "
                  f"Unrealized: {cur}{pos['unrealized_pnl']:>+,.{2 if is_usd else 8}f}")

        if state.get("ai_decision") and not state["ai_decision"].get("fallback"):
            ai = state["ai_decision"]
            print(f"  |  [AI] {ai['action']} → {ai['final_signal']} | {ai.get('summary', '')[:70]}")

        if state.get("last_trade"):
            t = state["last_trade"]
            _cur = "$" if is_usd else ""
            profit_str = f" | Profit: {_cur}{t['profit']:+,.{2 if is_usd else 8}f}" if t.get("profit") is not None else ""
            print(f"  |  >>> SIGNAL: {t['action']} {t['amount']:.8f} @ {_cur}{t['price']:,.{pd}f}{profit_str}")
            print(f"  |      Reason: {t['reason'][:75]}")

    def _print_banner(self):
        is_demo = getattr(self, "demo", False)
        if is_demo:
            trade_mode = "DEMO (offline synthetic)"
        elif self.paper:
            trade_mode = "PAPER"
        else:
            trade_mode = "LIVE"
        sizing_mode = self.mode.upper()
        brain_status = f"AI Brain: {self.brain.provider}/{self.brain.model}" if self.brain else "AI Brain: DISABLED (no API key)"
        print("")
        print("  HYDRA - Hyper-adaptive Dynamic Regime-switching Universal Agent")
        print("  ================================================================")
        if is_demo:
            print(f"  Trading: {trade_mode} | Sizing: {sizing_mode} | No Kraken / no API keys")
        else:
            cli_version = KrakenCLI.version()
            print(f"  Trading: {trade_mode} | Sizing: {sizing_mode} | Kraken CLI v{cli_version} (WSL)")
        print(f"  {brain_status}")
        if is_demo:
            print("  Offline demo — synthetic candles, paper fills, no exchange I/O.")
        elif self.paper:
            print("  Paper trading — no real money at risk.")
        else:
            print("  WARNING: Real trades with real money. Dead man's switch active.")
        print("")

    def _print_final_report(self):
        print(f"\n\n  {'='*80}")
        print(f"  HYDRA FINAL PERFORMANCE REPORT")
        print(f"  {'='*80}")

        for pair in self.pairs:
            engine = self.engines[pair]
            print(engine.get_performance_report())
            print()

        # Get final balance from exchange (skip offline demo — no WSL).
        print("  FINAL EXCHANGE BALANCE:")
        print(f"  {'-'*40}")
        is_demo = getattr(self, "demo", False)
        if is_demo:
            print("  (offline --demo: no exchange balance)")
            bal = {"error": "demo"}
        else:
            bal = KrakenCLI.balance()
        if "error" not in bal:
            for asset, amount in bal.items():
                print(f"    {asset}: {amount}")

        # Order journal
        if self.order_journal:
            print(f"\n  ORDER JOURNAL ({len(self.order_journal)} entries)")
            print(f"  {'-'*70}")
            for entry in self.order_journal[-20:]:
                lifecycle = entry.get("lifecycle") or {}
                state = lifecycle.get("state", "?")
                status_icon = "OK" if state == "FILLED" else ("~~" if state == "PARTIALLY_FILLED" else "XX")
                t_pair = entry.get("pair", "?")
                t_cur = "$" if (t_pair.split("/")[1].upper() if "/" in t_pair else "") in STABLE_QUOTES else ""
                intent = entry.get("intent") or {}
                amount = intent.get("amount", 0)
                price = lifecycle.get("avg_fill_price") or intent.get("limit_price") or 0
                print(f"  [{status_icon}] {entry.get('placed_at','?')} | "
                      f"{entry.get('side','?'):<4} {amount:.8f} {t_pair:<10} "
                      f"@ {t_cur}{price:>10,.{4 if t_cur else 8}f} | {state}")
                if lifecycle.get("terminal_reason"):
                    print(f"        reason: {lifecycle['terminal_reason']}")
        else:
            print(f"\n  No orders placed during session.")

        # Export journal + competition results (ephemeral; gitignored).
        # Offline --demo skips by default so first-run smoke leaves no
        # residual files; set HYDRA_DEMO_EXPORT=1 to keep the dumps.
        if getattr(self, "demo", False) and os.environ.get("HYDRA_DEMO_EXPORT") != "1":
            print("\n  (offline --demo: session files not written to disk)")
        else:
            ts = int(time.time())
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_file = os.path.join(base_dir, f"hydra_orders_{ts}.json")
            try:
                with open(log_file, "w") as f:
                    json.dump(self.order_journal, f, indent=2)
                print(f"\n  Order journal exported to: {log_file}")
            except Exception as e:
                print(f"\n  [WARN] Could not export order journal: {e}")
            self._export_competition_results(base_dir, ts)

        print(f"\n  Past performance does not guarantee future results. Not financial advice.")
        print(f"  {'='*80}")

    def _compute_pair_realized_pnl(self, pair: str,
                                   hydra_only: bool = False) -> float:
        """Compute realized P&L for a pair from the order journal.

        Uses average-cost-basis accounting: each sell's cost is valued at
        the running weighted-average buy price, so only *closed* round-trip
        profit/loss is reflected. Unsold inventory cost stays out of
        realized P&L — it belongs in unrealized (pos_size * (price - avg_entry)).

        **Stable-quote netting** (cross-pair fix, v2.20.0):
        Stable quotes are equivalent ($1 by CLAUDE.md invariant
        `STABLE_QUOTES = {USD, USDC, USDT}`). A SOL bought via SOL/USDC
        and sold via SOL/USD shares the same SOL inventory; siloing the
        cost basis per pair manufactures fictitious P&L. So when the
        requested pair has a stable quote, we walk all journal entries
        whose base matches AND whose quote is also in STABLE_QUOTES,
        treating prices as USD-equivalent. Entries with non-stable quote
        (e.g. SOL/BTC) stay per-pair — BTC is a real quote.

        `hydra_only`: when True, excludes journal entries with
        `source == "kraken_backfill"` — i.e. trades NOT placed by Hydra
        (user-action / pre-Hydra reconstructed). Used by the dashboard
        "Hydra-only" toggle. When False, full history including backfilled
        entries.

        Only counts FILLED / PARTIALLY_FILLED entries — PLACED,
        PLACEMENT_FAILED, CANCELLED_UNFILLED, REJECTED are skipped.

        Accurate across resumes because it reads directly from on-disk
        journal state, not engine balances which get pooled and re-split.
        """
        FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
        if "/" in pair:
            req_base, req_quote = pair.split("/", 1)
        else:
            req_base, req_quote = pair, "USD"
        req_is_stable = req_quote in STABLE_QUOTES

        def _matches(entry_pair: str) -> bool:
            if "/" not in entry_pair:
                return entry_pair == pair
            eb, eq = entry_pair.split("/", 1)
            if req_is_stable:
                return eb == req_base and eq in STABLE_QUOTES
            return entry_pair == pair

        def _source_ok(e: dict) -> bool:
            if not hydra_only:
                return True
            return (e.get("source") or "") != "kraken_backfill"

        entries = [
            e for e in self.order_journal
            if _matches(e.get("pair") or "") and _source_ok(e)
        ]
        entries.sort(key=lambda e: e.get("placed_at") or "")

        total_buy_cost = 0.0
        total_buy_vol = 0.0
        realized = 0.0
        for entry in entries:
            lifecycle = entry.get("lifecycle") or {}
            if lifecycle.get("state") not in FILL_STATES:
                continue
            vol = lifecycle.get("vol_exec") or 0
            price = lifecycle.get("avg_fill_price")
            if price is None:
                # Legacy migrated entries that lack avg_fill_price fall
                # back to the placement intent. Post-PR entries always
                # carry avg_fill_price from the execution stream.
                intent = entry.get("intent") or {}
                price = intent.get("limit_price") or 0
            if vol <= 0 or price <= 0:
                continue
            side = entry.get("side")
            try:
                fee = float(lifecycle.get("fee_quote") or 0.0)
            except (TypeError, ValueError):
                fee = 0.0
            if side == "BUY":
                # Fee-true cost basis: exchange fee increases acquisition cost.
                total_buy_cost += vol * price + max(0.0, fee)
                total_buy_vol += vol
            elif side == "SELL":
                avg_buy = (total_buy_cost / total_buy_vol) if total_buy_vol > 0 else 0
                cost_of_sold = vol * avg_buy
                # Fee-true proceeds: sell fee reduces realized P&L.
                realized += vol * price - cost_of_sold - max(0.0, fee)
                # Reduce the running buy pool by the sold quantity
                total_buy_cost = max(0.0, total_buy_cost - cost_of_sold)
                total_buy_vol = max(0.0, total_buy_vol - vol)
        return realized

    def _export_competition_results(self, base_dir: str, ts: int):
        """Export a competition_results.json for submission proof."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        pair_results = {}
        total_pnl_usd = 0.0
        total_trades = 0
        asset_prices = self._get_asset_prices()

        for pair in self.pairs:
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0
            quote = pair.split("/")[1]
            quote_usd = asset_prices.get(quote, 1.0)

            # Per-pair P&L from trade history (accurate across resumes).
            # Engine balances get pooled and re-split on each --resume, so
            # equity - initial_balance only reflects the current session.
            # Trade history gives the true per-pair realized performance.
            realized_pnl = self._compute_pair_realized_pnl(pair)
            unrealized_pnl = engine.position.size * (price - engine.position.avg_entry) if engine.position.size > 0 else 0
            pair_pnl = realized_pnl + unrealized_pnl
            pair_pnl_usd = pair_pnl * quote_usd
            total_pnl_usd += pair_pnl_usd
            total_trades += engine.total_trades
            win_rate = (engine.win_count / (engine.win_count + engine.loss_count) * 100) if (engine.win_count + engine.loss_count) > 0 else 0

            pair_results[pair] = {
                "realized_pnl": round(realized_pnl, 8),
                "unrealized_pnl": round(unrealized_pnl, 8),
                "net_pnl": round(pair_pnl, 8),
                "net_pnl_usd": round(pair_pnl_usd, 4),
                "max_drawdown_pct": round(engine.max_drawdown, 4),
                "total_trades": engine.total_trades,
                "wins": engine.win_count,
                "losses": engine.loss_count,
                "win_rate_pct": round(win_rate, 2),
                "sharpe": round(engine._calc_sharpe(), 4),
                "final_price": round(price, 8),
                "position_size": round(engine.position.size, 8),
            }

        # Aggregate cumulative P&L from competition start (survives --resume).
        start_balance = self._competition_start_balance if self._competition_start_balance is not None else self.initial_balance
        current_total_equity_usd = 0.0
        for pair in self.pairs:
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0
            equity = engine.balance + engine.position.size * price
            quote = pair.split("/")[1]
            quote_usd = asset_prices.get(quote, 1.0)
            current_total_equity_usd += equity * quote_usd
        cumulative_pnl_usd = current_total_equity_usd - start_balance

        results = {
            "agent": "HYDRA",
            "version": "2.29.0",
            "mode": self.mode,
            "paper": self.paper,
            "timestamp_start": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat() if self.start_time else None,
            "timestamp_end": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(elapsed, 1),
            "pairs": self.pairs,
            "competition_start_balance": round(start_balance, 4),
            "current_total_equity": round(current_total_equity_usd, 4),
            "total_initial_balance": self.initial_balance,
            "total_net_pnl": round(cumulative_pnl_usd, 4),
            "total_pnl_pct": round((cumulative_pnl_usd / start_balance) * 100, 4) if start_balance > 0 else 0,
            "total_trades": total_trades,
            "pair_results": pair_results,
            "order_journal": self.order_journal,
        }

        results_file = os.path.join(base_dir, f"competition_results_{ts}.json")
        try:
            with open(results_file, "w") as f:
                json.dump(results, f, indent=2)
            print(f"  Competition results exported to: {results_file}")
        except Exception as e:
            print(f"  [WARN] Could not export competition results: {e}")


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO PAIR DISCOVERY (--pairs auto)
# ═══════════════════════════════════════════════════════════════

def discover_portfolio_pairs(default_quote: str = "USD") -> List[str]:
    """Resolve every held Kraken asset into a tradable stable-quoted pair.

    Universe = the default cores (BTC/quote, ETH/quote, ZEC/quote — v2.29,
    always seeded so the highest-conviction pairs trade regardless of what
    is currently held) plus one satellite pair per additional held base
    asset. Held SOL is a satellite like any other asset — it trades freely
    (all balance is operational) but is no longer a default core. Quote
    selection per satellite:

      1. `HYDRA_AUTO_QUOTE` env (USD | USDC | USDT), if set, wins outright.
      2. Otherwise prefer BASE/USDC (idle USDC earns yield) **when the
         account actually holds USDC** — a USDC-quoted engine with no USDC
         can sell inventory but never buy, so preferring it without funds
         just parks the pair.
      3. Fall back to BASE/{default_quote} (USD essential for fill rate and
         for USD-only listings like NIGHT/USD).
      4. Skip the asset when Kraken lists neither candidate, or when the
         holding is below the pair's ordermin (unsellable dust) — nothing
         actionable exists for it.

    Staked/bonded holdings (.S/.B suffixes) are excluded: they cannot be
    sold and must not spawn engines. Fails soft: any balance/pair-info
    error returns just the triangle so a Kraken hiccup cannot brick boot.
    """
    from hydra_pair_registry import default_registry, STABLE_QUOTES

    cores = [f"BTC/{default_quote}", f"ETH/{default_quote}",
             f"ZEC/{default_quote}"]
    if default_quote != "USD":
        # Not every core base has every stable-quoted listing on Kraken
        # (e.g. ZEC/USDC does not exist). Verify and fall back per-core to
        # BASE/USD; on a constants fetch error keep the requested quote
        # (fail soft — boot must not brick on a Kraken hiccup).
        try:
            listed = KrakenCLI.load_pair_constants(cores) or {}
            cores = [c if c in listed else f"{c.split('/')[0]}/USD"
                     for c in cores]
        except Exception as e:
            print(f"  [AUTO-PAIRS] core listing check failed ({e!r}) — "
                  f"keeping {default_quote}-quoted cores")
    bal = KrakenCLI.balance()
    if not isinstance(bal, dict) or "error" in bal:
        print(f"  [AUTO-PAIRS] balance fetch failed ({bal!r}) — "
              f"falling back to the default core pairs")
        return cores

    # Aggregate spot holdings by canonical asset name.
    holdings: Dict[str, float] = {}
    for asset, amount in bal.items():
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        if amount <= 0 or KrakenCLI._is_staked(asset):
            continue
        canon = KrakenCLI._normalize_asset(asset)
        holdings[canon] = holdings.get(canon, 0.0) + amount

    forced_quote = (os.environ.get("HYDRA_AUTO_QUOTE") or "").upper().strip()
    if forced_quote and forced_quote not in STABLE_QUOTES:
        print(f"  [AUTO-PAIRS] ignoring invalid HYDRA_AUTO_QUOTE="
              f"{forced_quote!r} (must be one of {sorted(STABLE_QUOTES)})")
        forced_quote = ""
    usdc_funded = holdings.get("USDC", 0.0) > 0.5  # costmin threshold

    satellites: List[str] = []
    reg = default_registry()
    for base, amount in sorted(holdings.items()):
        if base in STABLE_QUOTES or base in ("BTC", "ETH", "ZEC"):
            continue  # stables fund quotes; BTC/ETH/ZEC are the cores
        if forced_quote:
            quote_order = [forced_quote]
        elif usdc_funded:
            quote_order = ["USDC", default_quote]
        else:
            quote_order = [default_quote, "USDC"]
        candidates = []
        for q in quote_order:
            if q != base and f"{base}/{q}" not in candidates:
                candidates.append(f"{base}/{q}")
        constants = KrakenCLI.load_pair_constants(candidates)
        chosen = None
        for cand in candidates:
            info = constants.get(cand) or constants.get(
                reg.get(cand).cli_format if reg.get(cand) else cand
            )
            if not info:
                continue
            ordermin = float(info.get("ordermin") or 0.0)
            if amount < ordermin:
                print(f"  [AUTO-PAIRS] {base}: holding {amount} below "
                      f"ordermin {ordermin} for {cand} — dust, skipping")
                break
            chosen = cand
            break
        if chosen:
            satellites.append(chosen)
            print(f"  [AUTO-PAIRS] {base}: {amount} held → {chosen}")
        else:
            print(f"  [AUTO-PAIRS] {base}: no tradable stable-quoted pair "
                  f"on Kraken — skipping")

    return cores + satellites


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="HYDRA — Live Regime-Adaptive Trading Agent for Kraken CLI",
    )
    parser.add_argument("--pairs", type=str, default="BTC/USD,ETH/USD,ZEC/USD",
                        help="Comma-separated trading pairs (default: BTC/USD,ETH/USD,ZEC/USD; "
                             "v2.29 dropped the SOL triangle — pass "
                             "SOL/USD,SOL/BTC,BTC/USD explicitly to restore it). Pass 'auto' "
                             "to discover every held Kraken asset and trade it against USDC "
                             "(preferred, earns yield) or USD (fill-rate / USD-only listings) "
                             "— see discover_portfolio_pairs.")
    parser.add_argument("--balance", type=float, default=100.0,
                        help="Reference balance for position sizing (default: 100)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between ticks (default: 300)")
    parser.add_argument("--candle-interval", type=int, default=60, choices=[1, 5, 15, 30, 60],
                        help="OHLC candle period in minutes (default: 60 — the timeframe the "
                             "hold-through rails and friction hurdle were calibrated on)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Total duration in seconds (default: 0 = run forever, Ctrl+C to stop)")
    parser.add_argument("--ws-port", type=int, default=8765,
                        help="WebSocket port for dashboard (default: 8765)")
    parser.add_argument("--mode", type=str, default="conservative",
                        choices=["conservative", "competition"],
                        help="Sizing mode: conservative (quarter-Kelly) or competition (half-Kelly)")
    parser.add_argument("--paper", action="store_true",
                        help="Paper trading via kraken-cli paper (no real money; still needs WSL + kraken-cli)")
    parser.add_argument("--demo", action="store_true",
                        help="Offline demo: synthetic candles + paper fills; no API keys, no WSL, no exchange I/O")
    parser.add_argument("--reset-params", action="store_true",
                        help="Reset learned tuning parameters to defaults")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last session snapshot (engines + coordinator state)")
    parser.add_argument("--user", type=str, default=None,
                        help="Run agent as specific user (fetches API keys from DB)")

    args = parser.parse_args()
    if args.pairs.strip().lower() == "auto":
        if args.demo:
            print("  [AUTO-PAIRS] --demo has no exchange account; using the default core pairs")
            pairs = ["BTC/USD", "ETH/USD", "ZEC/USD"]
        else:
            from hydra_config import DEFAULT_QUOTE
            _env_q = os.environ.get("HYDRA_QUOTE", "").strip().upper()
            _dq = _env_q if _env_q in STABLE_QUOTES else DEFAULT_QUOTE
            print(f"\n  [AUTO-PAIRS] Discovering held assets (quote preference: "
                  f"USDC if funded, else {_dq})...")
            pairs = discover_portfolio_pairs(default_quote=_dq)
    else:
        pairs = [p.strip() for p in args.pairs.split(",")]
    candle_interval = args.candle_interval

    if args.interval is not None:
        tick_interval = args.interval
    else:
        # Demo defaults to a fast loop so first-run smoke feels alive.
        tick_interval = 2 if args.demo else 300

    if args.demo:
        print(f"\n  HYDRA — Offline DEMO mode. Synthetic market data; no keys / no WSL.")
    elif args.paper:
        print(f"\n  HYDRA — Paper trading mode. No real money at risk.")
    else:
        print(f"\n  WARNING: HYDRA will execute REAL trades on Kraken.")
    print(f"  Pairs: {', '.join(pairs)}")
    print(f"  Mode: {args.mode} | Balance ref: ${args.balance}")
    print(f"  Candles: {candle_interval}m | Tick: {tick_interval}s")
    print(f"  Duration: {args.duration}s")
    if not args.paper and not args.demo:
        print(f"  Dead man's switch will be active.")
    if args.user and not args.demo:
        import hydra_auth
        keys = hydra_auth.get_api_keys_by_username(args.user, "kraken")
        if keys:
            os.environ["KRAKEN_API_KEY"] = keys["api_key"]
            os.environ["KRAKEN_API_SECRET"] = keys["api_secret"]
            print(f"  [AUTH] Injected Kraken API keys for user '{args.user}'")
        else:
            print(f"  [AUTH] WARNING: No API keys found for user '{args.user}'. Falling back to default env.")
    print()

    agent = HydraAgent(
        pairs=pairs,
        initial_balance=args.balance,
        interval_seconds=tick_interval,
        duration_seconds=args.duration,
        ws_port=args.ws_port,
        mode=args.mode,
        paper=args.paper or args.demo,
        candle_interval=candle_interval,
        reset_params=args.reset_params,
        resume=args.resume and not args.demo,
        demo=args.demo,
    )
    agent.run()


if __name__ == "__main__":
    main()
