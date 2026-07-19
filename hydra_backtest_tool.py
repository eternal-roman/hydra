#!/usr/bin/env python3
"""
HYDRA Backtest — Agent Tool API (Phase 4 of v2.10.0 backtest platform).

Anthropic-compatible tool schemas + `BacktestToolDispatcher` so brain agents
(Analyst, Risk Manager) can run backtests mid-deliberation via tool-use.
See docs/BACKTEST_SPEC.md §6.4.

Public surface
--------------
BACKTEST_TOOLS
    List of Anthropic tool-use schemas. Drop this into any
    `anthropic.messages.create(tools=...)` call.

BacktestToolDispatcher
    .execute(tool_name, tool_input, caller) -> Dict
    Routes a tool-use call to the right handler; enforces quotas; audits.

QuotaTracker
    In-memory rate + concurrency limits per caller + global. Resets at
    UTC midnight. Thread-safe.

Design invariants
-----------------
- Every call is audited via ExperimentStore.audit_log with
  {caller, tool, hypothesis, allowed, reason} — read-only tools included.
- Brain agents are **create + read only**. No tool can delete or mutate an
  experiment post-creation (I8, audit-chain integrity).
- Dispatcher never raises to the brain: all errors return
  {"success": False, "error": "..."} so the LLM can recover.
- Phase 4 is synchronous: run_backtest blocks until the run completes.
  Phase 6's BacktestWorkerPool will re-wrap this to return an id first and
  the result later; the tool schema stays identical.
- All dispatcher public methods are thread-safe (QuotaTracker uses an
  internal lock; ExperimentStore uses RLock).
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hydra_backtest import _iso_utc_now
from hydra_experiments import (
    DEFAULT_STORE_ROOT,
    Experiment,
    ExperimentStore,
    build_config_from_preset,
    compare,
    load_presets,
    new_experiment,
    run_experiment,
    sweep_experiment,
)


# ═══════════════════════════════════════════════════════════════
# Tool schemas (Anthropic tool-use format)
# ═══════════════════════════════════════════════════════════════

# Keep in lockstep with hydra_experiments.PRESET_LIBRARY keys. Enumerated
# explicitly in the schema so the LLM gets compile-time help (cannot emit
# a bogus preset name).
_PRESET_NAMES = [
    "default", "ideal", "divergent", "aggressive", "defensive",
    "regime_trending", "regime_ranging", "regime_volatile",
]

BACKTEST_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "run_backtest",
        "description": (
            "Run a backtest experiment with a chosen preset and optional overrides. "
            "Returns an experiment_id and the result summary. "
            "Use this to validate a hypothesis (e.g., 'does tighter RSI improve "
            "Sharpe in volatile regimes?'). ALWAYS include a hypothesis — it is "
            "logged and reviewed by the AI observer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "enum": _PRESET_NAMES,
                    "description": "Starting preset.",
                },
                "hypothesis": {
                    "type": "string",
                    "description": "Why you're running this. Required for audit + review.",
                    "minLength": 8,
                },
                "overrides": {
                    "type": "object",
                    "description": (
                        "Per-pair param overrides merged on top of the preset: "
                        '{"BTC/USD": {"momentum_rsi_upper": 78.0}}'
                    ),
                },
                "pairs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pairs to test. Defaults to ['BTC/USD'].",
                },
                "n_candles": {
                    "type": "integer",
                    "minimum": 50,
                    "maximum": 20000,
                    "description": "Synthetic candle count for the test window.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["conservative", "competition"],
                },
                "seed": {
                    "type": "integer",
                    "description": "Random seed for determinism.",
                },
                "with_monte_carlo": {
                    "type": "boolean",
                    "description": "Run MC block-bootstrap CI on trade profits.",
                },
                "with_walk_forward": {
                    "type": "boolean",
                    "description": "Run walk-forward re-test across slices.",
                },
            },
            "required": ["preset", "hypothesis"],
        },
    },
    {
        "name": "list_presets",
        "description": "List all available preset names and descriptions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_experiments",
        "description": (
            "Browse prior experiments. Returns a compact summary list. "
            "Use tags/status filters to narrow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "status": {"type": "string",
                           "enum": ["pending", "running", "complete", "failed", "cancelled"]},
                "tag": {"type": "string"},
                "triggered_by": {"type": "string"},
            },
        },
    },
    {
        "name": "get_experiment",
        "description": "Fetch the full record for one experiment by id.",
        "input_schema": {
            "type": "object",
            "properties": {"experiment_id": {"type": "string"}},
            "required": ["experiment_id"],
        },
    },
    {
        "name": "compare_experiments",
        "description": (
            "Rank up to 8 experiments across return, Sharpe, max DD, profit "
            "factor; returns per-metric winners + pairwise p-values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "experiment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 8,
                },
            },
            "required": ["experiment_ids"],
        },
    },
    {
        "name": "find_best",
        "description": (
            "Find the single experiment with the highest value for a metric, "
            "gated on a minimum trade count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["sharpe", "total_return_pct", "profit_factor",
                             "sortino", "annualized_return_pct"],
                },
                "min_trades": {"type": "integer", "minimum": 0, "maximum": 10000},
                "tag": {"type": "string"},
            },
        },
    },
    {
        "name": "sweep_param",
        "description": (
            "Serial sweep: run one backtest per param value. Each call consumes "
            "len(values) against the daily quota. Reserve for narrow, explicit "
            "hypotheses — not exploratory fishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "enum": _PRESET_NAMES},
                "param": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 10,
                },
                "hypothesis": {"type": "string", "minLength": 8},
                "pair": {"type": "string"},
                "n_candles": {"type": "integer", "minimum": 50, "maximum": 20000},
            },
            "required": ["preset", "param", "values", "hypothesis"],
        },
    },
    {
        "name": "get_equity_curve",
        "description": (
            "Return the per-tick equity curve for one pair of one experiment. "
            "Useful for spotting when a strategy broke (drawdown cliff, early "
            "collapse, etc.). Large curves auto-downsample to `downsample_to`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "experiment_id": {"type": "string"},
                "pair": {"type": "string"},
                "downsample_to": {
                    "type": "integer",
                    "minimum": 20,
                    "maximum": 5000,
                },
            },
            "required": ["experiment_id", "pair"],
        },
    },
]


# Tools that count against the per-agent daily quota (they create compute work).
# Read-only queries (list/get/compare/find_best/equity_curve) are always free.
_QUOTA_COSTLY = {"run_backtest": 1, "sweep_param": None}  # None = len(values)


# ═══════════════════════════════════════════════════════════════
# Quota tracker
# ═══════════════════════════════════════════════════════════════

@dataclass
class QuotaUsage:
    daily_count: int = 0
    concurrent: int = 0
    day_key: str = ""   # YYYYMMDD UTC


class QuotaTracker:
    """In-memory rate + concurrency limits for brain-tool-use calls.

    Day boundary: UTC midnight. All counters reset at the first call of a
    new day. Thread-safe: all state mutations happen under an internal lock.

    Defaults per I11 in the spec:
      - 3 concurrent calls per caller persona
      - 10 costly calls / day per caller persona
      - 50 costly calls / day global
    """

    def __init__(
        self,
        per_caller_daily: int = 10,
        per_caller_concurrent: int = 3,
        global_daily: int = 50,
    ) -> None:
        self.per_caller_daily = per_caller_daily
        self.per_caller_concurrent = per_caller_concurrent
        self.global_daily = global_daily
        self._lock = threading.Lock()
        self._by_caller: Dict[str, QuotaUsage] = {}
        self._global = QuotaUsage()

    # ---- helpers ----

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _roll_if_new_day(self, usage: QuotaUsage, day: str) -> None:
        if usage.day_key != day:
            usage.daily_count = 0
            usage.day_key = day

    # ---- public API ----

    def can_acquire(self, caller: str, cost: int = 1) -> Tuple[bool, str]:
        """Check without acquiring. Returns (allowed, reason)."""
        with self._lock:
            day = self._today_key()
            c = self._by_caller.setdefault(caller, QuotaUsage())
            self._roll_if_new_day(c, day)
            self._roll_if_new_day(self._global, day)

            if c.concurrent >= self.per_caller_concurrent:
                return False, (f"concurrent cap ({self.per_caller_concurrent}) "
                               f"reached for {caller}")
            if c.daily_count + cost > self.per_caller_daily:
                return False, (f"daily cap ({self.per_caller_daily}) would be "
                               f"exceeded for {caller} (need {cost}, used {c.daily_count})")
            if self._global.daily_count + cost > self.global_daily:
                return False, (f"global daily cap ({self.global_daily}) would be "
                               f"exceeded (need {cost}, used {self._global.daily_count})")
            return True, ""

    def acquire(self, caller: str, cost: int = 1) -> Tuple[bool, str]:
        """Atomically check + increment counters. Returns (allowed, reason)."""
        with self._lock:
            day = self._today_key()
            c = self._by_caller.setdefault(caller, QuotaUsage())
            self._roll_if_new_day(c, day)
            self._roll_if_new_day(self._global, day)

            if c.concurrent >= self.per_caller_concurrent:
                return False, (f"concurrent cap ({self.per_caller_concurrent}) "
                               f"reached for {caller}")
            if c.daily_count + cost > self.per_caller_daily:
                return False, (f"daily cap ({self.per_caller_daily}) would be "
                               f"exceeded for {caller}")
            if self._global.daily_count + cost > self.global_daily:
                return False, (f"global daily cap ({self.global_daily}) would be "
                               f"exceeded")

            c.concurrent += 1
            c.daily_count += cost
            self._global.daily_count += cost
            return True, ""

    def release(self, caller: str) -> None:
        """Decrement concurrent counter. Daily counters persist until day roll."""
        with self._lock:
            c = self._by_caller.get(caller)
            if c and c.concurrent > 0:
                c.concurrent -= 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "global_daily": self._global.daily_count,
                "global_daily_cap": self.global_daily,
                "per_caller": {
                    caller: {
                        "daily_count": u.daily_count,
                        "concurrent": u.concurrent,
                    }
                    for caller, u in self._by_caller.items()
                },
                "caps": {
                    "per_caller_daily": self.per_caller_daily,
                    "per_caller_concurrent": self.per_caller_concurrent,
                },
            }


# ═══════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════

# Standard error payload — kept flat so the LLM can read it directly.
def _error(msg: str, **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"success": False, "error": msg}
    d.update(extra)
    return d


def _ok(data: Any, **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"success": True, "data": data}
    d.update(extra)
    return d


def _summarize_experiment(exp: Experiment) -> Dict[str, Any]:
    """Compact view for list/run_backtest responses. Full record via
    get_experiment."""
    m = exp.result.metrics if exp.result else None
    return {
        "id": exp.id,
        "name": exp.name,
        "status": exp.status,
        "created_at": exp.created_at,
        "triggered_by": exp.triggered_by,
        "hypothesis": exp.hypothesis,
        "tags": list(exp.tags),
        "base_preset": exp.base_preset,
        "metrics": {
            "total_trades": m.total_trades if m else 0,
            "total_return_pct": round(m.total_return_pct, 4) if m else 0.0,
            "sharpe": round(m.sharpe, 4) if m else 0.0,
            "max_drawdown_pct": round(m.max_drawdown_pct, 4) if m else 0.0,
            "profit_factor": _finite_or(m.profit_factor, 0.0) if m else 0.0,
            "win_rate_pct": round(m.win_rate_pct, 2) if m else 0.0,
        },
    }


def _finite_or(v: float, fallback: float) -> float:
    import math
    return round(v, 4) if (v is not None and math.isfinite(v)) else fallback


def _downsample(values: List[float], target: int) -> List[float]:
    if target <= 0 or len(values) <= target:
        return list(values)
    step = len(values) / target
    return [values[int(i * step)] for i in range(target)]


class BacktestToolDispatcher:
    """Routes tool-use calls from the brain into backtest actions.

    Construct once at agent startup; inject into HydraBrain in Phase 5. The
    dispatcher owns an ExperimentStore + a QuotaTracker; neither leaks to
    the brain directly (brain is handed the dispatcher and asks via
    .execute()).

    Read-only queries (list/get/find_best/compare/equity_curve) bypass the
    quota — they're free compute. Costly operations (run_backtest /
    sweep_param) acquire then release; release always happens in a finally
    clause so an exception doesn't leak the concurrent slot.

    Allowed callers are a soft convention (any string works), but the
    quota is keyed on that string, so the brain should pass stable values
    like "brain:analyst", "brain:risk_manager", "human", "cli".
    """

    READ_ONLY_TOOLS = {
        "list_presets", "list_experiments", "get_experiment",
        "compare_experiments", "find_best", "get_equity_curve",
    }

    def __init__(
        self,
        store: Optional[ExperimentStore] = None,
        quota: Optional[QuotaTracker] = None,
        store_root: Optional[Path] = None,
        pool: Optional[Any] = None,
    ) -> None:
        root = store_root or DEFAULT_STORE_ROOT
        self.store = store if store is not None else ExperimentStore(root=root)
        self.quota = quota if quota is not None else QuotaTracker()
        # v2.27.6: when a BacktestWorkerPool is attached (agent mount),
        # run_backtest enqueues instead of blocking the brain/tick thread.
        self.pool = pool

    # ---- public ----

    def execute(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        caller: str = "unknown",
    ) -> Dict[str, Any]:
        """Dispatch a tool_use block. Returns the payload for a tool_result.

        Never raises. Errors become `{"success": False, "error": ...}`.
        """
        handler = self._handlers().get(tool_name)
        if handler is None:
            self._audit(caller, tool_name, tool_input, allowed=False,
                        reason="unknown tool")
            return _error(f"unknown tool: {tool_name!r}",
                          known_tools=sorted(self._handlers().keys()))

        # Quota gate (read-only tools skip it entirely).
        if tool_name in self.READ_ONLY_TOOLS:
            self._audit(caller, tool_name, tool_input, allowed=True,
                        reason="read-only")
            try:
                return handler(tool_input, caller)
            except Exception as e:
                return _error(f"{type(e).__name__}: {e}",
                              traceback=traceback.format_exc())

        cost = self._cost(tool_name, tool_input)
        ok, reason = self.quota.acquire(caller, cost=cost)
        if not ok:
            self._audit(caller, tool_name, tool_input, allowed=False,
                        reason=reason)
            return _error(f"quota denied: {reason}")

        self._audit(caller, tool_name, tool_input, allowed=True,
                    reason=f"acquired cost={cost}")
        try:
            return handler(tool_input, caller)
        except Exception as e:
            return _error(f"{type(e).__name__}: {e}",
                          traceback=traceback.format_exc())
        finally:
            self.quota.release(caller)

    # ---- handlers ----

    def _handlers(self) -> Dict[str, Callable[[Dict[str, Any], str], Dict[str, Any]]]:
        return {
            "run_backtest":        self._tool_run_backtest,
            "list_presets":        self._tool_list_presets,
            "list_experiments":    self._tool_list_experiments,
            "get_experiment":      self._tool_get_experiment,
            "compare_experiments": self._tool_compare_experiments,
            "find_best":           self._tool_find_best,
            "sweep_param":         self._tool_sweep_param,
            "get_equity_curve":    self._tool_get_equity_curve,
        }

    def _cost(self, tool_name: str, tool_input: Dict[str, Any]) -> int:
        if tool_name == "sweep_param":
            return max(1, len(tool_input.get("values") or []))
        return 1

    # ---- individual tools ----

    def _tool_run_backtest(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        preset = tool_input.get("preset")
        hypothesis = tool_input.get("hypothesis") or ""
        if not preset:
            return _error("preset is required")
        if not hypothesis or len(hypothesis) < 8:
            return _error("hypothesis is required and must be ≥ 8 characters")

        pairs = tuple(tool_input.get("pairs") or ["BTC/USD"])
        n_candles = int(tool_input.get("n_candles") or 500)
        seed = int(tool_input.get("seed") or 42)
        overrides = tool_input.get("overrides") or None
        mode = tool_input.get("mode") or None

        try:
            cfg, effective_ov = build_config_from_preset(
                preset=preset,
                pairs=pairs,
                n_candles=n_candles,
                seed=seed,
                extra_overrides=overrides,
                store_root=self.store.root,
            )
        except KeyError as e:
            return _error(f"unknown preset: {e}")
        except Exception as e:
            return _error(f"preset resolution failed: {e}")

        if mode and mode != cfg.mode:
            from dataclasses import replace
            cfg = replace(cfg, mode=mode)

        exp = new_experiment(
            name=f"brain:{preset}" if caller.startswith("brain:") else f"tool:{preset}",
            config=cfg,
            hypothesis=hypothesis,
            triggered_by=caller,
            base_preset=preset,
            overrides=effective_ov,
            tags=[f"preset:{preset}", f"caller:{caller}"],
        )
        # Prefer worker pool so live tick / brain never block on long runs.
        if self.pool is not None:
            try:
                exp_id = self.pool.submit_experiment(exp)
            except Exception as e:
                return _error(f"pool submit failed: {e}")
            return _ok({
                "status": "queued",
                "experiment_id": exp_id,
                "message": (
                    "Backtest queued on BacktestWorkerPool; poll get_experiment "
                    "for results (I1: never block the live tick)."
                ),
            })
        # Offline / unit-test path without a pool: still synchronous.
        run_experiment(
            exp,
            store=self.store,
            with_monte_carlo=bool(tool_input.get("with_monte_carlo")),
            with_walk_forward=bool(tool_input.get("with_walk_forward")),
        )
        return _ok(_summarize_experiment(exp))

    def _tool_list_presets(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        presets = load_presets(store_root=self.store.root)
        out = [
            {
                "name": name,
                "description": spec.get("description", ""),
                "mode": spec.get("mode", "conservative"),
                "override_keys": sorted((spec.get("overrides") or {}).keys()),
            }
            for name, spec in presets.items()
        ]
        return _ok(out)

    def _tool_list_experiments(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        limit = int(tool_input.get("limit") or 50)
        status = tool_input.get("status")
        tag = tool_input.get("tag")
        triggered_by = tool_input.get("triggered_by")

        def _pred(e: Experiment) -> bool:
            if status and e.status != status:
                return False
            if tag and tag not in e.tags:
                return False
            if triggered_by and e.triggered_by != triggered_by:
                return False
            return True

        rows = self.store.list(filter_fn=_pred, limit=limit)
        return _ok([_summarize_experiment(e) for e in rows])

    def _tool_get_experiment(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        eid = tool_input.get("experiment_id")
        if not eid:
            return _error("experiment_id is required")
        try:
            exp = self.store.load(eid)
        except KeyError:
            return _error("experiment not found", experiment_id=eid)
        return _ok(exp.to_dict())

    def _tool_compare_experiments(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        ids: List[str] = list(tool_input.get("experiment_ids") or [])
        if len(ids) < 2:
            return _error("at least 2 experiment_ids required")
        experiments: List[Experiment] = []
        missing: List[str] = []
        for eid in ids:
            try:
                experiments.append(self.store.load(eid))
            except KeyError:
                missing.append(eid)
        if missing:
            return _error("experiments missing", missing_ids=missing)
        report = compare(experiments)
        # Serialize the dataclass payload
        return _ok({
            "experiments": report.experiments,
            "winner_per_metric": report.winner_per_metric,
            "rows": [asdict(r) for r in report.rows],
            "pairwise_sharpe_p_values": {
                f"{a}__{b}": p
                for (a, b), p in report.pairwise_sharpe_p_values.items()
            },
        })

    def _tool_find_best(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        metric = tool_input.get("metric") or "sharpe"
        min_trades = int(tool_input.get("min_trades") or 50)
        tag = tool_input.get("tag")

        def _pred(e: Experiment) -> bool:
            return (tag in e.tags) if tag else True

        best = self.store.find_best(metric=metric, filter_fn=_pred, min_trades=min_trades)
        if best is None:
            return _ok(None, note=f"no experiment with {metric} ≥ threshold "
                                  f"and ≥ {min_trades} trades")
        return _ok(_summarize_experiment(best))

    def _tool_sweep_param(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        preset = tool_input.get("preset")
        param = tool_input.get("param")
        values = tool_input.get("values") or []
        hypothesis = tool_input.get("hypothesis") or ""
        pair = tool_input.get("pair")
        n_candles = int(tool_input.get("n_candles") or 500)

        if not preset or not param or not values:
            return _error("preset, param, and values are required")
        if not hypothesis or len(hypothesis) < 8:
            return _error("hypothesis is required and must be ≥ 8 characters")
        if len(values) > 10:
            return _error("sweep values limited to 10 per call")

        try:
            pairs = tuple([pair]) if pair else ("BTC/USD",)
            cfg, _ov = build_config_from_preset(
                preset=preset,
                pairs=pairs,
                n_candles=n_candles,
                store_root=self.store.root,
            )
        except KeyError as e:
            return _error(f"unknown preset: {e}")

        results = sweep_experiment(
            base_config=cfg,
            param=param,
            values=[float(v) for v in values],
            pair=pair or pairs[0],
            store=self.store,
            triggered_by=caller,
            tags=[f"preset:{preset}", f"caller:{caller}", f"param:{param}"],
        )
        return _ok([_summarize_experiment(e) for e in results])

    def _tool_get_equity_curve(
        self, tool_input: Dict[str, Any], caller: str
    ) -> Dict[str, Any]:
        eid = tool_input.get("experiment_id")
        pair = tool_input.get("pair")
        downsample_to = int(tool_input.get("downsample_to") or 500)
        if not eid or not pair:
            return _error("experiment_id and pair are required")
        try:
            exp = self.store.load(eid)
        except KeyError:
            return _error("experiment not found", experiment_id=eid)
        if exp.result is None:
            return _error("experiment has no result yet", status=exp.status)
        curve = exp.result.equity_curve.get(pair)
        if curve is None:
            return _error("pair not found in equity curve", available=list(exp.result.equity_curve))
        return _ok({
            "pair": pair,
            "length": len(curve),
            "values": _downsample(curve, downsample_to),
        })

    # ---- audit ----

    def _audit(
        self,
        caller: str,
        tool: str,
        tool_input: Dict[str, Any],
        allowed: bool,
        reason: str,
    ) -> None:
        """Append a single audit record for this call. Best-effort — if the
        store fails we don't propagate (audit noise must not kill the
        dispatcher).

        tool_input is intentionally logged — reviewers need the hypothesis
        and the override set to understand why the brain ran this test.
        Truncated lightly so a huge overrides dict doesn't bloat the log.
        """
        compact_input = {k: v for k, v in tool_input.items() if k != "overrides"}
        if "overrides" in tool_input:
            ov = tool_input.get("overrides") or {}
            compact_input["override_keys"] = sorted(ov.keys()) if isinstance(ov, dict) else []
        try:
            self.store.audit_log({
                "event": "tool_call",
                "caller": caller,
                "tool": tool,
                "allowed": allowed,
                "reason": reason,
                "input": compact_input,
            })
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")


# ═══════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="hydra-tool-smoke-"))
    print(f"[tool smoke] store: {tmp}")
    d = BacktestToolDispatcher(store_root=tmp)

    print("[tool smoke] list_presets")
    out = d.execute("list_presets", {}, caller="cli")
    print(f"  -> {len(out['data'])} presets")

    print("[tool smoke] run_backtest (divergent preset)")
    out = d.execute("run_backtest", {
        "preset": "divergent",
        "hypothesis": "Loosened gates should increase trade count at possibly lower Sharpe.",
        "n_candles": 200,
    }, caller="brain:analyst")
    print(f"  -> success={out['success']} "
          f"{out.get('data', {}).get('metrics', {})}")

    print("[tool smoke] list_experiments")
    out = d.execute("list_experiments", {"limit": 5}, caller="brain:analyst")
    print(f"  -> {len(out['data'])} entries")

    print("[tool smoke] quota snapshot")
    print(f"  -> {d.quota.snapshot()}")
