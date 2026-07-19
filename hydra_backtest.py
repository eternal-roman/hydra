#!/usr/bin/env python3
"""
HYDRA Backtest — Core replay engine.

Replays historical candles through the live HydraEngine + CrossPairCoordinator.
Zero logic drift: reuses engine math verbatim; only I/O is mocked (candles from
a CandleSource instead of a WS stream, SimulatedFiller instead of Kraken REST
placement).

See docs/BACKTEST_SPEC.md for the full architecture and invariants.

Public API:
    from hydra_backtest import BacktestRunner, BacktestConfig

    config = BacktestConfig(
        name="tight-rsi-in-vol",
        pairs=("BTC/USD",),
        param_overrides={"BTC/USD": {"momentum_rsi_upper": 75}},
        data_source="synthetic",
        data_source_params={"kind": "mean_reverting", "n_candles": 2000},
    )
    result = BacktestRunner(config).run()
    print(result.metrics.sharpe, result.metrics.total_return_pct)
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import subprocess
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from hydra_engine import (
    Candle,
    CrossPairCoordinator,
    HydraEngine,
    SIZING_COMPETITION,
    SIZING_CONSERVATIVE,  # noqa: F401 — re-exported for callers
)

HYDRA_VERSION = "2.28.1"

# Reasonable defaults; enforced at config construction and runtime.
DEFAULT_MAX_TICKS = 200_000
DEFAULT_WORKER_POOL_SIZE = 2


# ═══════════════════════════════════════════════════════════════
# Config / Result dataclasses
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BacktestConfig:
    """Immutable configuration for one backtest run.

    `param_overrides` is keyed by pair, values applied via HydraEngine.apply_tuned_params.
    Stamps (git_sha, param_hash, hydra_version) auto-filled by `finalize_stamps`.
    """

    name: str
    description: str = ""
    hypothesis: str = ""

    pairs: Tuple[str, ...] = ("BTC/USD",)
    initial_balance_per_pair: float = 100.0
    candle_interval: int = 60  # minutes (matches live default; rails calibrated at 1h)

    mode: str = "conservative"  # "conservative" | "competition"
    param_overrides_json: str = "{}"  # JSON-encoded Dict[pair, Dict[param, value]]; frozen-safe

    coordinator_enabled: bool = True

    data_source: str = "synthetic"  # "kraken" | "csv" | "synthetic"
    data_source_params_json: str = "{}"  # JSON-encoded; frozen-safe

    start_time: Optional[str] = None  # ISO 8601 UTC; None = implicit from source
    end_time: Optional[str] = None

    fill_model: str = "realistic"  # "optimistic" | "realistic" | "pessimistic"
    maker_fee_bps: float = 16.0  # Kraken default maker tier ~0.16%

    real_time_factor: float = 0.0  # 0 = max speed; 1 = live cadence; 60 = 60× live
    random_seed: int = 42
    max_ticks: int = DEFAULT_MAX_TICKS

    # Stamps
    git_sha: str = ""
    param_hash: str = ""
    hydra_version: str = ""
    created_at: str = ""

    # ---- helpers to handle JSON-encoded fields (dataclass is frozen) ----

    @property
    def param_overrides(self) -> Dict[str, Dict[str, float]]:
        return json.loads(self.param_overrides_json)

    @property
    def data_source_params(self) -> Dict[str, Any]:
        return json.loads(self.data_source_params_json)


def finalize_stamps(cfg: BacktestConfig) -> BacktestConfig:
    """Return a new BacktestConfig with git_sha, param_hash, hydra_version, created_at filled."""
    git_sha = cfg.git_sha or _safe_git_sha()
    param_hash = cfg.param_hash or _compute_param_hash(cfg)
    hydra_version = cfg.hydra_version or HYDRA_VERSION
    created_at = cfg.created_at or _iso_utc_now()
    return BacktestConfig(
        **{
            **asdict(cfg),
            "git_sha": git_sha,
            "param_hash": param_hash,
            "hydra_version": hydra_version,
            "created_at": created_at,
        }
    )


def _safe_git_sha() -> str:
    """Best-effort git SHA; returns 'unknown' on any failure (C9 in spec)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=2.0
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _compute_param_hash(cfg: BacktestConfig) -> str:
    """Stable SHA256 over the configuration fields that affect engine behavior.

    Excludes stamps themselves to avoid self-referential hashing.
    """
    canonical = {
        "pairs": list(cfg.pairs),
        "initial_balance_per_pair": cfg.initial_balance_per_pair,
        "candle_interval": cfg.candle_interval,
        "mode": cfg.mode,
        "param_overrides": cfg.param_overrides,
        "coordinator_enabled": cfg.coordinator_enabled,
        "data_source": cfg.data_source,
        "data_source_params": cfg.data_source_params,
        "start_time": cfg.start_time,
        "end_time": cfg.end_time,
        "fill_model": cfg.fill_model,
        "maker_fee_bps": cfg.maker_fee_bps,
        "random_seed": cfg.random_seed,
    }
    blob = json.dumps(canonical, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class BacktestMetrics:
    """Summary metrics for a backtest. Advanced metrics (bootstrap CI, walk-forward,
    regime-conditioned) land in Phase 2 / `hydra_backtest_metrics.py`.
    """

    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0

    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_ticks: float = 0.0

    fill_rate: float = 0.0
    fills: int = 0
    rejects: int = 0

    # Per-regime breakdown (filled by Phase 2 for full stats; basic slot here)
    pnl_by_regime: Dict[str, float] = field(default_factory=dict)
    trades_by_regime: Dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Result of one backtest run. Mutable during execution; snapshot at end."""

    config: BacktestConfig
    status: str = "pending"  # pending | running | complete | cancelled | failed
    started_at: str = ""
    completed_at: Optional[str] = None
    wall_clock_seconds: float = 0.0

    equity_curve: Dict[str, List[float]] = field(default_factory=dict)  # pair -> per-tick equity
    regime_ribbon: Dict[str, List[str]] = field(default_factory=dict)
    signal_log: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    trade_log: List[Dict[str, Any]] = field(default_factory=list)

    metrics: BacktestMetrics = field(default_factory=BacktestMetrics)
    per_pair_metrics: Dict[str, BacktestMetrics] = field(default_factory=dict)

    candles_processed: int = 0
    fills: int = 0
    rejects: int = 0
    brain_calls: int = 0
    brain_overrides: int = 0

    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": asdict(self.config),
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "wall_clock_seconds": self.wall_clock_seconds,
            "equity_curve": self.equity_curve,
            "regime_ribbon": self.regime_ribbon,
            "signal_log": self.signal_log,
            "trade_log": self.trade_log,
            "metrics": asdict(self.metrics),
            "per_pair_metrics": {k: asdict(v) for k, v in self.per_pair_metrics.items()},
            "candles_processed": self.candles_processed,
            "fills": self.fills,
            "rejects": self.rejects,
            "brain_calls": self.brain_calls,
            "brain_overrides": self.brain_overrides,
            "errors": self.errors,
        }


# ═══════════════════════════════════════════════════════════════
# Candle sources
# ═══════════════════════════════════════════════════════════════

class CandleSource(ABC):
    """Abstract candle provider.

    `iter_candles(pair)` yields Candle objects in chronological order.
    All sources MUST produce deterministic output for a given seed/range
    to preserve reproducibility (I12).
    """

    @abstractmethod
    def iter_candles(self, pair: str) -> Iterator[Candle]:
        ...

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        ...


class SyntheticSource(CandleSource):
    """Generates deterministic synthetic price series. No external I/O.

    Kinds:
      * "gbm"            — geometric Brownian motion
      * "mean_reverting" — Ornstein–Uhlenbeck on log-price
      * "flat"           — constant close (useful for degenerate testing)
    """

    def __init__(
        self,
        kind: str = "gbm",
        n_candles: int = 1000,
        start_price: float = 100.0,
        volatility: float = 0.02,  # per-candle log stdev
        drift: float = 0.0,
        seed: int = 42,
    ) -> None:
        self.kind = kind
        self.n_candles = n_candles
        self.start_price = start_price
        self.volatility = volatility
        self.drift = drift
        self.seed = seed

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        # Seed per-pair so distinct pairs get distinct series but remain
        # reproducible across processes (stdlib hash() is PYTHONHASHSEED-
        # salted; zlib.adler32 is stable). v2.27.6 / I12.
        import zlib
        pair_seed = self.seed ^ (zlib.adler32(pair.encode("utf-8")) & 0xFFFF)
        rng = random.Random(pair_seed)
        price = self.start_price
        ts = int(time.time()) - self.n_candles * 60 * 15
        for i in range(self.n_candles):
            if self.kind == "gbm":
                # d(ln P) = drift - 0.5*vol^2 + vol*N(0,1)
                shock = rng.gauss(0.0, 1.0)
                log_return = self.drift - 0.5 * self.volatility ** 2 + self.volatility * shock
                next_price = price * math.exp(log_return)
            elif self.kind == "mean_reverting":
                # OU process reverting to start_price on log scale
                theta = 0.05
                log_mean = math.log(self.start_price)
                shock = rng.gauss(0.0, 1.0)
                log_price = math.log(price)
                log_price = log_price + theta * (log_mean - log_price) + self.volatility * shock
                next_price = math.exp(log_price)
            elif self.kind == "flat":
                next_price = price
            else:
                raise ValueError(f"Unknown synthetic kind: {self.kind}")

            # Build OHLC from the two-point (price, next_price) span with a bit of wick noise
            high = max(price, next_price) * (1 + abs(rng.gauss(0.0, self.volatility * 0.3)))
            low = min(price, next_price) * (1 - abs(rng.gauss(0.0, self.volatility * 0.3)))
            volume = max(0.0, rng.gauss(100.0, 20.0))
            yield Candle(
                open=price,
                high=high,
                low=low,
                close=next_price,
                volume=volume,
                timestamp=float(ts + i * 60 * 15),
            )
            price = next_price

    def describe(self) -> Dict[str, Any]:
        return {
            "source": "synthetic",
            "kind": self.kind,
            "n_candles": self.n_candles,
            "start_price": self.start_price,
            "volatility": self.volatility,
            "drift": self.drift,
            "seed": self.seed,
        }


class CsvSource(CandleSource):
    """Loads candles from a CSV file. Expected columns: timestamp,open,high,low,close,volume."""

    def __init__(self, path: str) -> None:
        self.path = path

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        with open(self.path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                yield Candle(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    timestamp=float(row["timestamp"]),
                )

    def describe(self) -> Dict[str, Any]:
        return {"source": "csv", "path": self.path}


class KrakenHistoricalSource(CandleSource):
    """Fetches historical OHLC from the Kraken CLI with a local disk cache.

    Respects the live 2s rate limit by delegating to `KrakenCLI.ohlc()` which
    already enforces it (I10). Cache lives in `.hydra-experiments/candle_cache/`.
    Cache never invalidates — historical candles are immutable by definition.
    """

    def __init__(
        self,
        interval: int = 15,
        cache_dir: str = ".hydra-experiments/candle_cache",
        kraken_cli: Optional[Any] = None,
    ) -> None:
        self.interval = interval
        self.cache_dir = cache_dir
        self._kraken_cli = kraken_cli  # injectable for tests
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, pair: str) -> str:
        safe = pair.replace("/", "_")
        return os.path.join(self.cache_dir, f"{safe}_{self.interval}.json")

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        path = self._cache_path(pair)
        rows = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rows = json.load(fh)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                print(f"  [BACKTEST] cache unreadable for {pair} ({type(e).__name__}: {e}); refetching")
                rows = None
        if rows is None:
            cli = self._kraken_cli
            if cli is None:
                # Lazy import to avoid import-time dependency in backtest unit tests
                from hydra_agent import KrakenCLI
                cli = KrakenCLI
            rows = cli.ohlc(pair, interval=self.interval) or []
            # Cache atomically
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            os.replace(tmp, path)
        for r in rows:
            yield Candle(
                open=float(r.get("open", 0)),
                high=float(r.get("high", 0)),
                low=float(r.get("low", 0)),
                close=float(r.get("close", 0)),
                volume=float(r.get("volume", 0)),
                timestamp=float(r.get("timestamp", 0)),
            )

    def describe(self) -> Dict[str, Any]:
        return {"source": "kraken", "interval": self.interval, "cache_dir": self.cache_dir}


class SqliteSource(CandleSource):
    """Reads candles from the canonical hydra_history.sqlite store.
    Default source as of v2.20.0."""

    def __init__(self, db_path: str, grain_sec: int,
                 start_ts: int, end_ts: int):
        self.db_path = db_path
        self.grain_sec = grain_sec
        self.start_ts = start_ts
        self.end_ts = end_ts

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        from hydra_history_store import HistoryStore
        store = HistoryStore(self.db_path)
        for r in store.fetch(pair, self.grain_sec, self.start_ts, self.end_ts):
            yield Candle(open=r.open, high=r.high, low=r.low, close=r.close,
                         volume=r.volume, timestamp=float(r.ts))

    def describe(self) -> Dict[str, Any]:
        return {"source": "sqlite", "db_path": self.db_path,
                "grain_sec": self.grain_sec,
                "start_ts": self.start_ts, "end_ts": self.end_ts}


def make_candle_source(cfg: BacktestConfig) -> CandleSource:
    """Factory: build the right CandleSource from a BacktestConfig."""
    params = cfg.data_source_params
    if cfg.data_source == "sqlite":
        return SqliteSource(
            db_path=params["db_path"],
            grain_sec=params["grain_sec"],
            start_ts=int(params["start_ts"]),
            end_ts=int(params["end_ts"]),
        )
    if cfg.data_source == "synthetic":
        return SyntheticSource(
            kind=params.get("kind", "gbm"),
            n_candles=params.get("n_candles", 1000),
            start_price=params.get("start_price", 100.0),
            volatility=params.get("volatility", 0.02),
            drift=params.get("drift", 0.0),
            seed=params.get("seed", cfg.random_seed),
        )
    if cfg.data_source == "csv":
        return CsvSource(path=params["path"])
    if cfg.data_source == "kraken":
        return KrakenHistoricalSource(
            interval=params.get("interval", cfg.candle_interval),
            cache_dir=params.get("cache_dir", ".hydra-experiments/candle_cache"),
        )
    raise ValueError(f"Unknown data_source: {cfg.data_source}")


# ═══════════════════════════════════════════════════════════════
# Simulated fill model (post-only)
# ═══════════════════════════════════════════════════════════════

@dataclass
class PendingOrder:
    """One in-flight post-only limit order awaiting fill against the next candle.

    The runner maintains at most one pending order per pair at a time (mirrors live).
    """

    pair: str
    side: str  # "BUY" | "SELL"
    limit_price: float
    size: float
    placed_tick: int
    pre_trade_snapshot: Dict[str, Any]  # for rollback if fill model rejects


@dataclass
class SimulatedFill:
    """Result of a single fill attempt."""

    filled: bool
    fill_price: float = 0.0
    fee_paid: float = 0.0
    reason: str = ""


class SimulatedFiller:
    """Post-only fill model. Never lookahead beyond the *next* candle.

    Models (configurable per-run):
      * optimistic:  fill if next candle's wick touches limit price
      * realistic:   fill if next candle's body spends ≥30% of the span past limit
                     (i.e., price genuinely lingered near the limit, not a spike)
      * pessimistic: fill only if next candle's close crosses the limit
    """

    def __init__(self, model: str = "realistic", maker_fee_bps: float = 16.0) -> None:
        if model not in ("optimistic", "realistic", "pessimistic"):
            raise ValueError(f"Unknown fill_model: {model}")
        self.model = model
        self.maker_fee_bps = maker_fee_bps

    def try_fill(self, order: PendingOrder, next_candle: Candle) -> SimulatedFill:
        """Decide if `order` fills against `next_candle` under this model."""
        side = order.side
        px = order.limit_price
        c = next_candle

        # Non-finite OHLC (malformed CSV row, degenerate synthetic params)
        # would sail through the comparisons below — NaN compares False on
        # every branch — and could fee a phantom fill. Reject explicitly.
        if not all(math.isfinite(v) for v in (c.open, c.high, c.low, c.close)):
            return SimulatedFill(False, reason="malformed_candle: non-finite OHLC")

        # Quick reject: price range didn't touch limit at all
        if side == "BUY" and c.low > px:
            return SimulatedFill(False, reason="no_touch: next.low > limit")
        if side == "SELL" and c.high < px:
            return SimulatedFill(False, reason="no_touch: next.high < limit")

        # Sufficient-penetration test for realistic / pessimistic
        if self.model == "optimistic":
            filled = True
        elif self.model == "realistic":
            filled = _body_penetrates(c, side, px, threshold=0.30)
        else:  # pessimistic
            if side == "BUY":
                filled = c.close <= px
            else:
                filled = c.close >= px

        if not filled:
            return SimulatedFill(False, reason=f"{self.model}_gate_failed")

        fee = abs(order.size) * px * (self.maker_fee_bps / 10_000.0)
        return SimulatedFill(filled=True, fill_price=px, fee_paid=fee, reason=self.model)


def _body_penetrates(candle: Candle, side: str, limit: float, threshold: float) -> bool:
    """Did the candle body spend ≥`threshold` of its span past `limit`?"""
    body_low = min(candle.open, candle.close)
    body_high = max(candle.open, candle.close)
    body_span = body_high - body_low
    if body_span <= 0:
        return True  # doji; treat as touched
    if side == "BUY":
        # Body needs to be at or below the limit for `threshold` of its span
        if body_high <= limit:
            return True
        depth = (limit - body_low) / body_span
        return depth >= threshold
    # SELL: body needs to be at or above the limit for `threshold` of its span
    if body_low >= limit:
        return True
    depth = (body_high - limit) / body_span
    return depth >= threshold


# ═══════════════════════════════════════════════════════════════
# BacktestRunner — the replay loop
# ═══════════════════════════════════════════════════════════════

class BacktestRunner:
    """Replay historical candles through live engine code. Zero logic drift.

    One runner → one BacktestResult. Safe to run multiple instances in parallel;
    each owns its own HydraEngine instances (I2).
    """

    def __init__(
        self,
        config: BacktestConfig,
        sources_override: Optional[Dict[str, "CandleSource"]] = None,
    ) -> None:
        self.config = finalize_stamps(config)
        # sources_override is consulted in place of make_candle_source() — used by
        # hydra_backtest_metrics.walk_forward to feed candle slices without
        # duplicating _loop. None (default) preserves live path.
        self._sources_override = sources_override
        self._build_engines_and_coord()

    # ---- internal setup ----

    def _build_engines_and_coord(self) -> None:
        cfg = self.config
        sizing_preset = SIZING_COMPETITION if cfg.mode == "competition" else SIZING_CONSERVATIVE
        self.engines: Dict[str, HydraEngine] = {}
        for pair in cfg.pairs:
            engine = HydraEngine(
                initial_balance=cfg.initial_balance_per_pair,
                asset=pair,
                sizing=sizing_preset,
                candle_interval=cfg.candle_interval,
            )
            overrides = cfg.param_overrides.get(pair, {})
            if overrides:
                engine.apply_tuned_params(overrides)
            self.engines[pair] = engine
        self._seed_trend_overlay()
        self.coordinator: Optional[CrossPairCoordinator] = (
            CrossPairCoordinator(list(cfg.pairs)) if cfg.coordinator_enabled else None
        )
        self.filler = SimulatedFiller(cfg.fill_model, cfg.maker_fee_bps)
        self._pending: Dict[str, Optional[PendingOrder]] = {p: None for p in cfg.pairs}

    def _seed_trend_overlay(self) -> None:
        """Seed each engine's daily-close series from PRE-WINDOW history so
        the trend overlay is warm from tick 1 (mirrors the live agent's
        daily REST seed at boot). Sqlite sources only — synthetic/csv runs
        have no pre-history, the overlay reports None, and behavior is
        identical to pre-overlay (fail-open by design)."""
        cfg = self.config
        if cfg.data_source != "sqlite":
            return
        try:
            params = cfg.data_source_params
            start_ts = int(params["start_ts"])
            db_path = params["db_path"]
            grain = int(params.get("grain_sec") or 3600)
            from hydra_history_store import HistoryStore
            store = HistoryStore(db_path)
            lookback = start_ts - 430 * 86400  # MAX_DAILY_CLOSES + margin
            for pair in cfg.pairs:
                daily: Dict[int, float] = {}
                for r in store.fetch(pair, grain, lookback, start_ts):
                    daily[int(r.ts // 86400)] = r.close  # last bar of the day wins
                if daily:
                    self.engines[pair].seed_daily_closes([
                        {"timestamp": day * 86400, "close": close}
                        for day, close in sorted(daily.items())
                    ])
        except Exception:
            # Seeding is an enhancement, never a blocker — the overlay
            # fails open without it.
            pass

    # ---- public API ----

    def run(
        self,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_token: Optional[threading.Event] = None,
    ) -> BacktestResult:
        """Execute the backtest end-to-end.

        Args:
            on_tick: optional callback invoked with a compact state dict on each
                     tick (for observer streaming). Expensive; pass None for perf.
            cancel_token: threading.Event; runner checks `.is_set()` each tick
                          and returns partial result with status='cancelled'.
        """
        cfg = self.config
        result = BacktestResult(
            config=cfg,
            status="running",
            started_at=_iso_utc_now(),
        )
        for pair in cfg.pairs:
            result.equity_curve[pair] = []
            result.regime_ribbon[pair] = []
            result.signal_log[pair] = []

        t0 = time.time()
        try:
            self._loop(result, on_tick, cancel_token)
        except Exception as e:
            result.status = "failed"
            result.errors.append({
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            })
        finally:
            result.wall_clock_seconds = time.time() - t0
            result.completed_at = _iso_utc_now()
            if result.status == "running":
                result.status = "complete"
            self._finalize_metrics(result)
        return result

    # ---- internal loop ----

    def _loop(
        self,
        result: BacktestResult,
        on_tick: Optional[Callable[[Dict[str, Any]], None]],
        cancel_token: Optional[threading.Event],
    ) -> None:
        cfg = self.config
        sources = self._sources_override if self._sources_override is not None else (
            {p: make_candle_source(cfg) for p in cfg.pairs}
        )
        iterators = {p: iter(sources[p].iter_candles(p)) for p in cfg.pairs}

        tick = 0
        pair_sleep_seconds = (cfg.candle_interval * 60) / cfg.real_time_factor if cfg.real_time_factor > 0 else 0.0

        while tick < cfg.max_ticks:
            if cancel_token is not None and cancel_token.is_set():
                result.status = "cancelled"
                return

            # Gather next candle per pair. If any source is exhausted, terminate.
            current_candles: Dict[str, Candle] = {}
            exhausted = False
            for pair, it in iterators.items():
                try:
                    current_candles[pair] = next(it)
                except StopIteration:
                    exhausted = True
                    break
            if exhausted:
                break

            # 1) Try to fill any pending orders against the *current* candle.
            # Pending order was placed on previous tick; this tick's OHLC is
            # its first chance to fill (post-only semantics).
            for pair, order in list(self._pending.items()):
                if order is None:
                    continue
                fill = self.filler.try_fill(order, current_candles[pair])
                engine = self.engines[pair]
                if fill.filled:
                    # v2.27.6: rewrite books to fill price (live PR-C parity)
                    # then deduct maker fee. trade_log is appended only here
                    # so rejected intents never skew holding-time stats.
                    side = str(order.side).upper()
                    applied = engine.true_up_fill(
                        side=side,
                        amount=float(order.size),
                        fill_price=float(fill.fill_price),
                        pre_trade_snapshot=order.pre_trade_snapshot,
                        reason=f"backtest_fill:{fill.reason}",
                        strategy="MOMENTUM",
                        confidence=0.0,
                    )
                    if not applied:
                        # Fallback if snapshot missing: keep optimistic books.
                        pass
                    # Fee floor mirrors live _deduct_fill_fee (balance never negative).
                    engine.balance = max(0.0, engine.balance - fill.fee_paid)
                    result.fills += 1
                    # Realized P&L: _apply_sell_fill records the closed
                    # trade (with profit) on engine.trades — surface it so
                    # Monte Carlo / bootstrap CI have real per-trade
                    # profits to resample (previously hardcoded None,
                    # which silently disabled both).
                    realized_profit = None
                    if applied and side == "SELL" and engine.trades:
                        realized_profit = engine.trades[-1].profit
                        if realized_profit is not None:
                            # Net of the maker fee the engine books don't carry.
                            realized_profit = float(realized_profit) - float(fill.fee_paid)
                    result.trade_log.append({
                        "tick": tick,
                        "pair": pair,
                        "side": side,
                        "price": float(fill.fill_price),
                        "amount": float(order.size),
                        "value": float(fill.fill_price) * float(order.size),
                        "reason": f"backtest_fill:{fill.reason}",
                        "confidence": 0.0,
                        "strategy": "MOMENTUM",
                        "profit": realized_profit,
                        "timestamp": getattr(current_candles[pair], "timestamp", None),
                        "fee_paid": float(fill.fee_paid),
                    })
                else:
                    # Post-only miss → full rollback to pre-trade snapshot.
                    engine.restore_position(order.pre_trade_snapshot)
                    result.rejects += 1
                self._pending[pair] = None

            # 2) Ingest candles + generate signals (engine tick with generate_only=True
            #    preserves the execute_signal seam used in live).
            engine_states: Dict[str, Dict[str, Any]] = {}
            for pair in cfg.pairs:
                engine = self.engines[pair]
                engine.ingest_candle({
                    "open": current_candles[pair].open,
                    "high": current_candles[pair].high,
                    "low": current_candles[pair].low,
                    "close": current_candles[pair].close,
                    "volume": current_candles[pair].volume,
                    "timestamp": current_candles[pair].timestamp,
                })
                state = engine.tick(generate_only=True)
                engine_states[pair] = state

            # 3) Cross-pair coordinator (same call order as live, hydra_agent.py:1957-1975)
            if self.coordinator is not None:
                for pair, state in engine_states.items():
                    self.coordinator.update(pair, state.get("regime", "RANGING"))
                try:
                    overrides = self.coordinator.get_overrides(engine_states)
                except Exception:
                    overrides = {}
                for pair, override in overrides.items():
                    state = engine_states.get(pair)
                    if not state or "signal" not in state:
                        continue
                    sig = state["signal"]
                    sig["action"] = override.get("signal", sig["action"])
                    sig["confidence"] = override.get("confidence_adj", sig["confidence"])
                    sig["reason"] = f"[CROSS-PAIR] {override.get('reason', '')}"
                    state["cross_pair_override"] = override

            # 4) (Phase 6) order-book, forex, brain modifiers — intentionally not
            # applied in Phase 1. Agent-mount phase will pipe the live modifier
            # chain through here. Drift test pins Phase-1 behavior to pure
            # engine + coordinator, which is exactly what this path covers.

            # 5) Execute signals and queue post-only orders for next-candle fill.
            for pair, state in engine_states.items():
                sig = state.get("signal", {})
                action = sig.get("action", "HOLD")
                if action == "HOLD":
                    continue
                engine = self.engines[pair]
                pre_snap = engine.snapshot_position()
                trade = engine.execute_signal(
                    action,
                    float(sig.get("confidence", 0.0)),
                    reason=sig.get("reason", ""),
                    strategy=state.get("strategy", "MOMENTUM"),
                    size_multiplier=1.0,
                )
                if trade is None:
                    continue
                self._pending[pair] = PendingOrder(
                    pair=pair,
                    side=trade.action,
                    limit_price=trade.price,
                    size=trade.amount,
                    placed_tick=tick,
                    pre_trade_snapshot=pre_snap,
                )
                # Intent is pending only — confirmed fills append to trade_log
                # on next-bar fill (v2.27.6; avoids reject skew on holding stats).

            # 6) Record per-tick series for result + UI streaming.
            for pair, state in engine_states.items():
                engine = self.engines[pair]
                price = engine.prices[-1] if engine.prices else 0.0
                equity = engine.balance + engine.position.size * price
                result.equity_curve[pair].append(equity)
                result.regime_ribbon[pair].append(state.get("regime", "RANGING"))
                sig = state.get("signal", {})
                result.signal_log[pair].append({
                    "tick": tick,
                    "action": sig.get("action", "HOLD"),
                    "confidence": sig.get("confidence", 0.0),
                })

            result.candles_processed = tick + 1

            # 7) Progress callback (compact dashboard-state-shape dict).
            if on_tick is not None:
                try:
                    on_tick(self._build_progress_state(tick, engine_states, result))
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")  # observer errors must not kill the replay

            # 8) Pace if requested (observer-mode; normally 0 for max-speed).
            if pair_sleep_seconds > 0:
                time.sleep(pair_sleep_seconds)

            tick += 1

    def _build_progress_state(
        self,
        tick: int,
        engine_states: Dict[str, Dict[str, Any]],
        result: BacktestResult,
    ) -> Dict[str, Any]:
        """Shape mirrors live dashboard state enough for observer modal reuse."""
        pairs_state: Dict[str, Any] = {}
        for pair, state in engine_states.items():
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0.0
            pairs_state[pair] = {
                "price": price,
                "regime": state.get("regime"),
                "strategy": state.get("strategy"),
                "signal": state.get("signal", {}),
                "position": {
                    "size": engine.position.size,
                    "avg_entry": engine.position.avg_entry,
                    "unrealized_pnl": engine.position.unrealized_pnl,
                },
                "portfolio": {
                    "equity": engine.balance + engine.position.size * price,
                    "balance": engine.balance,
                    "pnl_pct": ((engine.balance + engine.position.size * price) - engine.initial_balance)
                               / max(engine.initial_balance, 1e-9) * 100.0,
                    "max_drawdown_pct": engine.max_drawdown,
                },
                "performance": {
                    "total_trades": engine.total_trades,
                    "win_count": engine.win_count,
                    "loss_count": engine.loss_count,
                },
            }
        return {
            "tick": tick,
            "experiment_name": self.config.name,
            "pairs": pairs_state,
            "candles_processed": result.candles_processed,
            "fills": result.fills,
            "rejects": result.rejects,
        }

    # ---- metrics ----

    def _finalize_metrics(self, result: BacktestResult) -> None:
        cfg = self.config
        per_pair: Dict[str, BacktestMetrics] = {}
        agg_equity: List[float] = []
        total_trades_all = 0
        wins_all = 0
        losses_all = 0
        gross_profit_all = 0.0
        gross_loss_all = 0.0

        for pair, engine in self.engines.items():
            m = _compute_basic_metrics(
                engine=engine,
                equity_curve=result.equity_curve.get(pair, []),
                candle_interval_min=cfg.candle_interval,
            )
            # avg_holding_ticks: FIFO-pair BUY→SELL tick spans from the
            # trade_log. Pair-scoped so cross-pair strategies don't muddle
            # the stat. Zero if no complete round-trip trade occurred.
            m.avg_holding_ticks = _avg_holding_ticks(result.trade_log, pair)
            per_pair[pair] = m
            total_trades_all += engine.total_trades
            wins_all += engine.win_count
            losses_all += engine.loss_count
            gross_profit_all += engine.gross_profit
            gross_loss_all += engine.gross_loss

        # Aggregate equity = sum across pairs at each tick
        if result.equity_curve:
            n = min(len(v) for v in result.equity_curve.values())
            agg_equity = [
                sum(result.equity_curve[p][i] for p in result.equity_curve) for i in range(n)
            ]

        agg = BacktestMetrics()
        if agg_equity:
            starting = sum(cfg.initial_balance_per_pair for _ in cfg.pairs)
            ending = agg_equity[-1]
            agg.total_return_pct = (ending - starting) / max(starting, 1e-9) * 100.0
            agg.annualized_return_pct = _annualize_return(agg.total_return_pct, len(agg_equity), cfg.candle_interval)
            agg.sharpe = _sharpe_from_equity(agg_equity, cfg.candle_interval)
            agg.sortino = _sortino_from_equity(agg_equity, cfg.candle_interval)
            agg.max_drawdown_pct = _max_dd_pct(agg_equity)

        agg.total_trades = total_trades_all
        agg.win_count = wins_all
        agg.loss_count = losses_all
        denom_trades = wins_all + losses_all
        agg.win_rate_pct = (wins_all / denom_trades * 100.0) if denom_trades > 0 else 0.0
        # When there are no losing trades we used to emit math.inf here, which
        # sanitises to None on JSON save and then blows up compare() on reload.
        # Cap at a finite sentinel (999.0) so every downstream consumer sees a
        # real number. Any real profit_factor over ~10 is already "too good to
        # be true", so 999 reads as "∞" without dragging non-finite floats
        # through serialisation / stats code.
        agg.profit_factor = (gross_profit_all / gross_loss_all) if gross_loss_all > 0 else (
            999.0 if gross_profit_all > 0 else 0.0
        )
        agg.avg_win = (gross_profit_all / wins_all) if wins_all > 0 else 0.0
        agg.avg_loss = (gross_loss_all / losses_all) if losses_all > 0 else 0.0
        fills_total = result.fills
        rejects_total = result.rejects
        agg.fills = fills_total
        agg.rejects = rejects_total
        agg.fill_rate = (fills_total / (fills_total + rejects_total)) if (fills_total + rejects_total) > 0 else 0.0
        # Aggregate avg_holding_ticks: mean across pairs weighted by pair's
        # completed round-trip count. Unweighted mean would let a rarely-
        # traded pair skew the portfolio stat.
        agg.avg_holding_ticks = _aggregate_avg_holding(result.trade_log)

        result.metrics = agg
        result.per_pair_metrics = per_pair


# ═══════════════════════════════════════════════════════════════
# Metric helpers (basic; advanced metrics ship in Phase 2)
# ═══════════════════════════════════════════════════════════════

def _annualize_return(total_return_pct: float, n_ticks: int, candle_interval_min: int) -> float:
    if n_ticks <= 0:
        return 0.0
    periods_per_year = (365 * 24 * 60) / candle_interval_min
    # Geometric annualization from cumulative return
    try:
        gross = 1.0 + total_return_pct / 100.0
        if gross <= 0:
            return -100.0
        ann = gross ** (periods_per_year / n_ticks) - 1.0
        return ann * 100.0
    except (ValueError, OverflowError):
        return 0.0


def _returns_from_equity(equity: List[float]) -> List[float]:
    rets: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            rets.append(0.0)
        else:
            rets.append((equity[i] - prev) / prev)
    return rets


def _sharpe_from_equity(equity: List[float], candle_interval_min: int) -> float:
    rets = _returns_from_equity(equity)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    periods_per_year = (365 * 24 * 60) / candle_interval_min
    return (mean / sd) * math.sqrt(periods_per_year)


def _sortino_from_equity(equity: List[float], candle_interval_min: int) -> float:
    rets = _returns_from_equity(equity)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    if not downside:
        # Finite sentinel — see profit_factor note. 999.0 reads as "∞" for
        # downstream consumers without polluting JSON with None-on-reload.
        return 999.0 if mean > 0 else 0.0
    ds_var = sum(r * r for r in downside) / len(downside)
    ds_sd = math.sqrt(ds_var)
    if ds_sd <= 0:
        return 0.0
    periods_per_year = (365 * 24 * 60) / candle_interval_min
    return (mean / ds_sd) * math.sqrt(periods_per_year)


def _max_dd_pct(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _avg_holding_ticks(trade_log: List[Dict[str, Any]], pair: str) -> float:
    """Mean tick span between BUY and matching SELL for `pair`.

    FIFO pairing within the pair — a BUY at tick t is closed by the next
    SELL at tick t' ≥ t. Open BUYs (no matching SELL) are ignored. Returns
    0.0 if no complete round-trip exists.
    """
    spans: List[int] = []
    open_buy_ticks: List[int] = []
    for entry in trade_log:
        if entry.get("pair") != pair:
            continue
        side = entry.get("side")
        tk = entry.get("tick")
        if not isinstance(tk, int):
            continue
        if side == "BUY":
            open_buy_ticks.append(tk)
        elif side == "SELL" and open_buy_ticks:
            entry_tick = open_buy_ticks.pop(0)
            spans.append(tk - entry_tick)
    if not spans:
        return 0.0
    return sum(spans) / len(spans)


def _aggregate_avg_holding(trade_log: List[Dict[str, Any]]) -> float:
    """Completed-round-trip-weighted mean of avg_holding_ticks across pairs."""
    per_pair_counts: Dict[str, Tuple[float, int]] = {}
    per_pair_open: Dict[str, List[int]] = {}
    for entry in trade_log:
        pair = entry.get("pair", "")
        tk = entry.get("tick")
        if not pair or not isinstance(tk, int):
            continue
        open_list = per_pair_open.setdefault(pair, [])
        if entry.get("side") == "BUY":
            open_list.append(tk)
        elif entry.get("side") == "SELL" and open_list:
            entry_tick = open_list.pop(0)
            span = tk - entry_tick
            mean_so_far, count = per_pair_counts.get(pair, (0.0, 0))
            new_count = count + 1
            new_mean = (mean_so_far * count + span) / new_count
            per_pair_counts[pair] = (new_mean, new_count)
    if not per_pair_counts:
        return 0.0
    total_weight = sum(c for (_, c) in per_pair_counts.values())
    if total_weight == 0:
        return 0.0
    weighted = sum(mean * c for (mean, c) in per_pair_counts.values())
    return weighted / total_weight


def _compute_basic_metrics(
    engine: HydraEngine, equity_curve: List[float], candle_interval_min: int
) -> BacktestMetrics:
    m = BacktestMetrics()
    if not equity_curve:
        return m
    starting = engine.initial_balance
    ending = equity_curve[-1]
    m.total_return_pct = (ending - starting) / max(starting, 1e-9) * 100.0
    m.annualized_return_pct = _annualize_return(m.total_return_pct, len(equity_curve), candle_interval_min)
    m.sharpe = _sharpe_from_equity(equity_curve, candle_interval_min)
    m.sortino = _sortino_from_equity(equity_curve, candle_interval_min)
    m.max_drawdown_pct = max(engine.max_drawdown, _max_dd_pct(equity_curve))
    m.total_trades = engine.total_trades
    m.win_count = engine.win_count
    m.loss_count = engine.loss_count
    denom = engine.win_count + engine.loss_count
    m.win_rate_pct = (engine.win_count / denom * 100.0) if denom > 0 else 0.0
    # Finite sentinel 999.0 when there are no losing trades — see note on the
    # aggregate path above. Keeps metrics strictly-numeric across save/reload.
    m.profit_factor = (engine.gross_profit / engine.gross_loss) if engine.gross_loss > 0 else (
        999.0 if engine.gross_profit > 0 else 0.0
    )
    m.avg_win = (engine.gross_profit / engine.win_count) if engine.win_count > 0 else 0.0
    m.avg_loss = (engine.gross_loss / engine.loss_count) if engine.loss_count > 0 else 0.0
    return m


# ═══════════════════════════════════════════════════════════════
# Convenience helpers
# ═══════════════════════════════════════════════════════════════

def new_experiment_id() -> str:
    return str(uuid.uuid4())


def make_quick_config(
    *,
    name: str = "quick",
    pairs: Tuple[str, ...] = ("BTC/USD",),
    n_candles: int = 500,
    kind: str = "gbm",
    seed: int = 42,
    mode: str = "conservative",
    overrides: Optional[Dict[str, Dict[str, float]]] = None,
) -> BacktestConfig:
    """Build a backtest config with synthetic data — handy for tests and demos."""
    return finalize_stamps(BacktestConfig(
        name=name,
        pairs=pairs,
        mode=mode,
        data_source="synthetic",
        data_source_params_json=json.dumps({
            "kind": kind,
            "n_candles": n_candles,
            "seed": seed,
            "volatility": 0.02,
        }),
        param_overrides_json=json.dumps(overrides or {}),
        random_seed=seed,
    ))


if __name__ == "__main__":  # simple CLI demo
    cfg = make_quick_config(name="cli-demo", n_candles=600)
    print(f"[backtest] running demo: {cfg.name}  git={cfg.git_sha[:7]}  hash={cfg.param_hash[:8]}")
    res = BacktestRunner(cfg).run()
    m = res.metrics
    print(f"[backtest] status={res.status}  ticks={res.candles_processed}  wall={res.wall_clock_seconds:.2f}s")
    print(f"[backtest] return={m.total_return_pct:.2f}%  sharpe={m.sharpe:.2f}  "
          f"maxDD={m.max_drawdown_pct:.2f}%  trades={m.total_trades}  win%={m.win_rate_pct:.1f}")
