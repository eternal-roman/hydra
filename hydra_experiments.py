#!/usr/bin/env python3
"""
HYDRA Backtest — Experiments Framework (Phase 3 of v2.10.0 backtest platform).

Layer 3 atop hydra_backtest (Layer 1) + hydra_backtest_metrics (Layer 2):
adds the persistent *experiment* concept — a named hypothesis, its backtest
result, optional AI review, and searchable metadata. Backed by flat JSON
in `.hydra-experiments/` (gitignored).

See docs/BACKTEST_SPEC.md §5.4, §6.3.

Public API
----------
Experiment                  — dataclass: hypothesis + config + result + review
ExperimentStore             — persistent CRUD + find_best + prune + audit log
PRESET_LIBRARY              — in-code default presets
load_presets()              — read .hydra-experiments/presets.json
                              (merges user edits over in-code defaults)
resolve_preset(name, pairs) — preset name → concrete overrides dict
                              (handles "ideal" by reading tuner files)
new_experiment(...)         — build an Experiment record
run_experiment(exp, store)  — execute it and persist result
sweep_experiment(...)       — serial param sweep → list of Experiments
compare(experiments)        — ComparisonReport: ranked table + bootstrap p

Design invariants
-----------------
- Stdlib only (no deps).
- Every filesystem write is atomic (write-to-temp + os.replace).
- ExperimentStore NEVER reads/writes live journal / snapshot files (I3).
- Audit events are append-only JSON lines (one line per event).
- Preset file on disk is user-editable: a malformed file falls back to
  in-code defaults and logs a warning; does not crash.
- Experiment IDs are uuid4 hex — never reused.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hydra_backtest import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    BacktestRunner,
    finalize_stamps,
    make_quick_config,
    _iso_utc_now,
)
from hydra_backtest_metrics import (
    WalkForwardReport,
    MonteCarloReport,
    OutOfSampleReport,
    monte_carlo_resample,
    out_of_sample_gap,
    walk_forward,
)


DEFAULT_STORE_ROOT = Path(".hydra-experiments")
PRESETS_FILENAME = "presets.json"
EXPERIMENTS_DIRNAME = "experiments"
AUDIT_LOG = "audit.log"
REVIEW_HISTORY = "review_history.jsonl"

# Known BacktestConfig fields — used to filter dict on load so unknown stamps
# (e.g. from a future schema) don't raise KeyError on reconstruction.
_CONFIG_FIELDS = {
    "name", "description", "hypothesis",
    "pairs", "initial_balance_per_pair", "candle_interval",
    "mode", "param_overrides_json",
    "coordinator_enabled",
    "data_source", "data_source_params_json",
    "start_time", "end_time",
    "fill_model", "maker_fee_bps",
    "real_time_factor", "random_seed", "max_ticks",
    "git_sha", "param_hash", "hydra_version", "created_at",
}


# ═══════════════════════════════════════════════════════════════
# Preset library
# ═══════════════════════════════════════════════════════════════

# In-code defaults. Disk copy (see load_presets) can override. Human-editable.
# Keys match HydraEngine attribute names (see hydra_engine.DEFAULT_PARAMS +
# PositionSizer + HydraEngine.apply_tuned_params seams).
PRESET_LIBRARY: Dict[str, Dict[str, Any]] = {
    "default": {
        "description": "Current live params (no overrides).",
        "overrides": {},
    },
    "ideal": {
        "description": "Best per-pair params learned by the live tuner. "
                       "Resolved at run-time from hydra_params_*.json.",
        "overrides": {},   # filled by resolve_preset()
        "resolve_from_tuner": True,
    },
    "divergent": {
        "description": "Deliberately contrary to current live params — lower gates, wider RSI.",
        "overrides": {
            "min_confidence_threshold": 0.55,
            "momentum_rsi_lower": 25.0,
            "momentum_rsi_upper": 75.0,
            "kelly_multiplier": 0.5,
        },
    },
    "aggressive": {
        "description": "Competition mode + lowered conf threshold, 3/4-Kelly sizing.",
        "overrides": {
            "min_confidence_threshold": 0.60,
            "kelly_multiplier": 0.75,
            "max_position_pct": 0.50,
        },
        "mode": "competition",
    },
    "defensive": {
        "description": "High conf threshold, narrower RSI, quarter-Kelly.",
        "overrides": {
            "min_confidence_threshold": 0.75,
            "momentum_rsi_lower": 35.0,
            "momentum_rsi_upper": 65.0,
            "kelly_multiplier": 0.25,
        },
    },
    "regime_trending": {
        "description": "Tuned for TREND_UP / TREND_DOWN regimes.",
        "overrides": {
            "trend_ema_ratio": 1.003,
            "momentum_rsi_lower": 28.0,
        },
    },
    "regime_ranging": {
        "description": "Tuned for RANGING regime — tighter mean-reversion bands.",
        "overrides": {
            "mean_reversion_rsi_buy": 30.0,
            "mean_reversion_rsi_sell": 70.0,
        },
    },
    "regime_volatile": {
        "description": "Tuned for VOLATILE regime — lower vol mult, smaller sizing.",
        "overrides": {
            "volatile_atr_mult": 1.5,
            "kelly_multiplier": 0.15,
        },
    },
}


def _safe_pair_filename(pair: str) -> str:
    """Mirror hydra_tuner.py's sanitization so we read the same params file."""
    return re.sub(r"[^A-Za-z0-9]", "_", pair)


def _read_tuner_params(pair: str, search_dir: Optional[Path] = None) -> Dict[str, float]:
    """Load `hydra_params_{safe_pair}.json` from the project root (or search_dir).

    Returns an empty dict if the file is absent or unreadable — caller decides
    whether to treat that as a no-op or a warning.
    """
    base = search_dir or Path(__file__).resolve().parent
    path = base / f"hydra_params_{_safe_pair_filename(pair)}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        params = raw.get("params", {})
        # Coerce to float so JSON round-trip stays stable
        return {k: float(v) for k, v in params.items() if v is not None}
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        return {}


def load_presets(store_root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return the effective preset library: in-code defaults merged with
    user-editable disk copy (disk takes priority).

    On first call (disk file absent), writes PRESET_LIBRARY out atomically so
    the user has something to edit.
    """
    root = store_root or DEFAULT_STORE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    path = root / PRESETS_FILENAME

    # Bootstrap disk copy if missing
    if not path.exists():
        _atomic_write_json(path, PRESET_LIBRARY)
        return dict(PRESET_LIBRARY)

    try:
        disk = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [WARN] preset file unreadable ({type(e).__name__}: {e}); using in-code defaults")
        return dict(PRESET_LIBRARY)

    # Merge: disk entries replace in-code entries of the same key; unknown
    # in-code entries are preserved (so removing a preset from disk doesn't
    # silently make it unavailable for first-time users).
    merged: Dict[str, Dict[str, Any]] = dict(PRESET_LIBRARY)
    if isinstance(disk, dict):
        for k, v in disk.items():
            if isinstance(v, dict):
                merged[k] = v
    return merged


def resolve_preset(
    name: str,
    pairs: Tuple[str, ...],
    store_root: Optional[Path] = None,
    tuner_search_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve a preset name → a concrete overrides dict usable as
    BacktestConfig.param_overrides.

    Format:
        {pair: {param: value, ...}}

    Special handling:
      "ideal" — reads tuner's `hydra_params_*.json` per pair; missing files
                fall back to no overrides for that pair.
      non-ideal presets — applied identically to every pair.
    """
    presets = load_presets(store_root)
    if name not in presets:
        raise KeyError(f"unknown preset: {name!r}. Available: {sorted(presets)}")
    spec = presets[name]

    per_pair: Dict[str, Dict[str, float]] = {}
    if spec.get("resolve_from_tuner"):
        for pair in pairs:
            ov = _read_tuner_params(pair, search_dir=tuner_search_dir)
            if ov:
                per_pair[pair] = ov
        return per_pair

    ov = dict(spec.get("overrides", {}))
    for pair in pairs:
        per_pair[pair] = dict(ov)
    return per_pair


# ═══════════════════════════════════════════════════════════════
# Experiment record
# ═══════════════════════════════════════════════════════════════

@dataclass
class Experiment:
    """One named hypothesis + its backtest + optional AI review.

    The `review` field is opaque `Dict[str, Any]` in Phase 3 — it becomes
    `ReviewDecision` in Phase 7; storing it as a plain dict avoids a
    circular import against hydra_reviewer (which depends on this module).
    """

    id: str
    created_at: str
    name: str
    hypothesis: str = ""
    triggered_by: str = "cli"       # "human" | "brain:analyst" | "brain:risk" |
                                    # "brain:strategist" | "cli" | "reviewer"
    parent_id: Optional[str] = None
    base_preset: Optional[str] = None
    overrides: Dict[str, Any] = field(default_factory=dict)

    config: Optional[BacktestConfig] = None
    result: Optional[BacktestResult] = None
    review: Optional[Dict[str, Any]] = None

    status: str = "pending"         # pending | running | complete | failed | cancelled
    tags: List[str] = field(default_factory=list)

    # Optional analytics companions — populated by run_experiment when requested
    mc_report: Optional[MonteCarloReport] = None
    wf_report: Optional[WalkForwardReport] = None
    oos_report: Optional[OutOfSampleReport] = None

    # --- persistence ---

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "name": self.name,
            "hypothesis": self.hypothesis,
            "triggered_by": self.triggered_by,
            "parent_id": self.parent_id,
            "base_preset": self.base_preset,
            "overrides": self.overrides,
            "status": self.status,
            "tags": list(self.tags),
            "review": self.review,
            "config": asdict(self.config) if self.config else None,
            "result": self.result.to_dict() if self.result else None,
            "mc_report": asdict(self.mc_report) if self.mc_report else None,
            "wf_report": asdict(self.wf_report) if self.wf_report else None,
            "oos_report": asdict(self.oos_report) if self.oos_report else None,
        }
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Experiment":
        cfg = None
        if d.get("config"):
            # Filter to known fields so a future schema extension doesn't break loads.
            cfg_dict = {k: v for k, v in d["config"].items() if k in _CONFIG_FIELDS}
            if "pairs" in cfg_dict and isinstance(cfg_dict["pairs"], list):
                cfg_dict["pairs"] = tuple(cfg_dict["pairs"])
            cfg = BacktestConfig(**cfg_dict)

        result = _result_from_dict(d["result"]) if d.get("result") else None
        mc_report = _report_from_dict(MonteCarloReport, d["mc_report"]) if d.get("mc_report") else None
        wf_report = _report_from_dict(WalkForwardReport, d["wf_report"]) if d.get("wf_report") else None
        oos_report = _report_from_dict(OutOfSampleReport, d["oos_report"]) if d.get("oos_report") else None

        return cls(
            id=d["id"],
            created_at=d["created_at"],
            name=d["name"],
            hypothesis=d.get("hypothesis", ""),
            triggered_by=d.get("triggered_by", "cli"),
            parent_id=d.get("parent_id"),
            base_preset=d.get("base_preset"),
            overrides=d.get("overrides", {}),
            config=cfg,
            result=result,
            review=d.get("review"),
            status=d.get("status", "pending"),
            tags=d.get("tags", []),
            mc_report=mc_report,
            wf_report=wf_report,
            oos_report=oos_report,
        )


def _result_from_dict(d: Dict[str, Any]) -> BacktestResult:
    """Reconstruct BacktestResult from its .to_dict() form. Config dict is
    filtered to known fields; metrics are rebuilt from asdict form."""
    cfg_dict = {k: v for k, v in d["config"].items() if k in _CONFIG_FIELDS}
    if "pairs" in cfg_dict and isinstance(cfg_dict["pairs"], list):
        cfg_dict["pairs"] = tuple(cfg_dict["pairs"])
    cfg = BacktestConfig(**cfg_dict)

    m = BacktestMetrics(**d.get("metrics", {}))
    per_pair = {p: BacktestMetrics(**v) for p, v in d.get("per_pair_metrics", {}).items()}

    r = BacktestResult(
        config=cfg,
        status=d.get("status", "complete"),
        started_at=d.get("started_at", ""),
        completed_at=d.get("completed_at"),
        wall_clock_seconds=d.get("wall_clock_seconds", 0.0),
        equity_curve=d.get("equity_curve", {}),
        regime_ribbon=d.get("regime_ribbon", {}),
        signal_log=d.get("signal_log", {}),
        trade_log=d.get("trade_log", []),
        metrics=m,
        per_pair_metrics=per_pair,
        candles_processed=d.get("candles_processed", 0),
        fills=d.get("fills", 0),
        rejects=d.get("rejects", 0),
        brain_calls=d.get("brain_calls", 0),
        brain_overrides=d.get("brain_overrides", 0),
        errors=d.get("errors", []),
    )
    return r


def _report_from_dict(cls: type, d: Dict[str, Any]) -> Any:
    """Generic dataclass reconstruction — used for MC / WF / OOS reports.

    Dataclasses with nested dataclass fields need per-field reconstruction;
    Python's asdict() round-trips them as dicts and plain constructors don't
    re-hydrate. For the three report types here, fields are either scalars,
    lists, or dict; nested dataclasses (MonteCarloCI, WalkForwardSlice) are
    reconstructed by matching class names.
    """
    from hydra_backtest_metrics import MonteCarloCI, WalkForwardSlice

    if cls is MonteCarloReport:
        return MonteCarloReport(
            n_iter=d.get("n_iter", 0),
            block_len=d.get("block_len", 20),
            total_return_ci=MonteCarloCI(**d["total_return_ci"]),
            sharpe_ci=MonteCarloCI(**d["sharpe_ci"]),
            max_drawdown_ci=MonteCarloCI(**d["max_drawdown_ci"]),
            profit_factor_ci=MonteCarloCI(**d["profit_factor_ci"]),
        )
    if cls is WalkForwardReport:
        slices = [WalkForwardSlice(**s) for s in d.get("slices", [])]
        return WalkForwardReport(
            n_windows=d.get("n_windows", 0),
            train_pct=d.get("train_pct", 0.6),
            test_pct=d.get("test_pct", 0.4),
            slices=slices,
            mean_sharpe=d.get("mean_sharpe", 0.0),
            std_sharpe=d.get("std_sharpe", 0.0),
            sharpe_stability=d.get("sharpe_stability", 0.0),
            improved_slices=d.get("improved_slices", 0),
            improvement_pct_per_slice=d.get("improvement_pct_per_slice", []),
        )
    if cls is OutOfSampleReport:
        return OutOfSampleReport(**d)
    raise ValueError(f"unknown report class {cls}")


# ═══════════════════════════════════════════════════════════════
# Experiment construction
# ═══════════════════════════════════════════════════════════════

def new_experiment_id() -> str:
    return uuid.uuid4().hex


def new_experiment(
    name: str,
    config: BacktestConfig,
    hypothesis: str = "",
    triggered_by: str = "cli",
    parent_id: Optional[str] = None,
    base_preset: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
) -> Experiment:
    """Create a pending Experiment record. Does NOT run the backtest."""
    return Experiment(
        id=new_experiment_id(),
        created_at=_iso_utc_now(),
        name=name,
        hypothesis=hypothesis,
        triggered_by=triggered_by,
        parent_id=parent_id,
        base_preset=base_preset,
        overrides=overrides or {},
        config=finalize_stamps(config),
        tags=list(tags or []),
    )


def build_config_from_preset(
    preset: str,
    pairs: Tuple[str, ...] = ("SOL/USD",),
    n_candles: int = 1000,
    seed: int = 42,
    extra_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    store_root: Optional[Path] = None,
    tuner_search_dir: Optional[Path] = None,
) -> Tuple[BacktestConfig, Dict[str, Dict[str, float]]]:
    """Convenience: resolve a preset + produce a BacktestConfig ready to run.

    `extra_overrides` (per-pair → param dict) are merged on top of preset
    overrides (extras win). Returns (config, effective_overrides).
    """
    presets = load_presets(store_root)
    if preset not in presets:
        raise KeyError(f"unknown preset {preset!r}; available: {sorted(presets)}")

    ov = resolve_preset(preset, pairs, store_root=store_root, tuner_search_dir=tuner_search_dir)
    if extra_overrides:
        for p, extra in extra_overrides.items():
            merged = dict(ov.get(p, {}))
            merged.update(extra)
            ov[p] = merged

    mode = presets[preset].get("mode", "conservative")
    cfg = make_quick_config(
        name=f"preset:{preset}",
        pairs=pairs,
        n_candles=n_candles,
        seed=seed,
        mode=mode,
        overrides=ov,
    )
    return cfg, ov


# ═══════════════════════════════════════════════════════════════
# Execution
# ═══════════════════════════════════════════════════════════════

def run_experiment(
    exp: Experiment,
    store: Optional["ExperimentStore"] = None,
    with_monte_carlo: bool = False,
    with_walk_forward: bool = False,
    with_oos_gap: bool = False,
    mc_iter: int = 300,
    wf_n_windows: int = 3,
    oos_in_sample_pct: float = 0.8,
) -> Experiment:
    """Run the backtest + optional analytics companions. Persists to `store`
    if provided. Exceptions are captured on the Experiment — never propagated
    (live-safety pattern: experiment crashes must not affect caller).
    """
    if exp.config is None:
        raise ValueError("Experiment has no config")

    exp.status = "running"
    if store is not None:
        store.save(exp)
        store.audit_log({"event": "start", "id": exp.id, "name": exp.name,
                         "triggered_by": exp.triggered_by})

    try:
        runner = BacktestRunner(exp.config)
        result = runner.run()
        exp.result = result
        exp.status = result.status

        if with_monte_carlo and result.trade_log:
            profits = [
                float(t.get("profit", 0.0)) for t in result.trade_log
                if t.get("profit") not in (None, 0.0)
            ]
            if profits:
                exp.mc_report = monte_carlo_resample(
                    profits,
                    n_iter=mc_iter,
                    candle_interval_min=exp.config.candle_interval,
                    starting_equity=exp.config.initial_balance_per_pair * len(exp.config.pairs),
                    seed=exp.config.random_seed,
                )

        if with_walk_forward:
            exp.wf_report = walk_forward(exp.config, n_windows=wf_n_windows)

        if with_oos_gap:
            exp.oos_report = out_of_sample_gap(exp.config, in_sample_pct=oos_in_sample_pct)

    except Exception as e:
        exp.status = "failed"
        if exp.result is None:
            exp.result = BacktestResult(config=exp.config, status="failed",
                                        started_at=_iso_utc_now(),
                                        completed_at=_iso_utc_now())
        exp.result.errors.append({
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        })

    if store is not None:
        store.save(exp)
        store.audit_log({"event": "finish", "id": exp.id, "status": exp.status,
                         "metrics": _metric_summary(exp)})

    return exp


def _metric_summary(exp: Experiment) -> Dict[str, Any]:
    """Compact metric dump for the audit log — avoids ballooning the log file."""
    if exp.result is None:
        return {}
    m = exp.result.metrics
    return {
        "total_trades": m.total_trades,
        "total_return_pct": round(m.total_return_pct, 4),
        "sharpe": round(m.sharpe, 4),
        "max_drawdown_pct": round(m.max_drawdown_pct, 4),
        "fill_rate": round(m.fill_rate, 4),
    }


# ═══════════════════════════════════════════════════════════════
# ExperimentStore
# ═══════════════════════════════════════════════════════════════

class ExperimentStore:
    """Flat-file persistence for Experiment records. Safe for concurrent
    readers; a single in-process lock serializes writers to prevent audit-log
    interleaving. Not meant for cross-process concurrent writers — Phase 6
    will add a worker-pool-level queue for that.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else DEFAULT_STORE_ROOT
        self._exp_dir = self.root / EXPERIMENTS_DIRNAME
        self._audit_path = self.root / AUDIT_LOG
        self._review_history_path = self.root / REVIEW_HISTORY
        # RLock: delete() acquires the lock, then audit_log() re-acquires it
        # from the same thread for the append. Non-reentrant Lock deadlocks.
        self._lock = threading.RLock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self._exp_dir.mkdir(parents=True, exist_ok=True)
        if not self._audit_path.exists():
            self._audit_path.touch()
        if not self._review_history_path.exists():
            self._review_history_path.touch()

    # ---- CRUD ----

    def save(self, exp: Experiment) -> None:
        path = self._exp_dir / f"{exp.id}.json"
        with self._lock:
            _atomic_write_json(path, exp.to_dict())

    def load(self, exp_id: str) -> Experiment:
        path = self._exp_dir / f"{exp_id}.json"
        if not path.exists():
            raise KeyError(f"experiment not found: {exp_id}")
        data = json.loads(path.read_text())
        return Experiment.from_dict(data)

    def exists(self, exp_id: str) -> bool:
        return (self._exp_dir / f"{exp_id}.json").exists()

    def delete(self, exp_id: str) -> bool:
        path = self._exp_dir / f"{exp_id}.json"
        if not path.exists():
            return False
        with self._lock:
            path.unlink()
            self.audit_log({"event": "delete", "id": exp_id})
        return True

    # ---- Query ----

    def iter_experiments(self) -> Iterator[Experiment]:
        """Lazy iterator — returns Experiments sorted by created_at desc.

        Yields even if a single file is malformed (logs to audit, skips that
        file). Keeps the store usable when one experiment's JSON was hand-edited.
        """
        files = list(self._exp_dir.glob("*.json"))
        records: List[Tuple[str, Path]] = []
        for f in files:
            try:
                # Only peek at created_at; avoids materializing every Experiment
                # to sort when there are thousands of records.
                with f.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                records.append((raw.get("created_at", ""), f))
            except (json.JSONDecodeError, OSError):
                self.audit_log({"event": "load_error", "file": f.name})
                continue

        records.sort(key=lambda r: r[0], reverse=True)
        for _created, f in records:
            try:
                yield Experiment.from_dict(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError, KeyError):
                self.audit_log({"event": "load_error", "file": f.name})
                continue

    def list(
        self,
        filter_fn: Optional[callable] = None,
        limit: int = 100,
    ) -> List[Experiment]:
        """Return up to `limit` experiments, optionally filtered by a
        predicate. Sorted by created_at desc.
        """
        out: List[Experiment] = []
        for exp in self.iter_experiments():
            if filter_fn is not None and not filter_fn(exp):
                continue
            out.append(exp)
            if len(out) >= limit:
                break
        return out

    def find_best(
        self,
        metric: str = "sharpe",
        filter_fn: Optional[callable] = None,
        min_trades: int = 50,
    ) -> Optional[Experiment]:
        """Find experiment with the highest `metric` value, gated on
        min_trades (prevents a 1-trade fluke from being selected).

        Returns None if no experiment meets the threshold.
        """
        best: Optional[Experiment] = None
        best_val = -math.inf
        for exp in self.iter_experiments():
            if filter_fn is not None and not filter_fn(exp):
                continue
            if exp.result is None:
                continue
            if exp.result.metrics.total_trades < min_trades:
                continue
            val = getattr(exp.result.metrics, metric, None)
            if val is None or not isinstance(val, (int, float)):
                continue
            if math.isfinite(val) and val > best_val:
                best_val = val
                best = exp
        return best

    def prune(
        self,
        older_than_days: int = 30,
        keep_tags: Optional[List[str]] = None,
    ) -> int:
        """Delete experiments older than `older_than_days` that don't carry
        any tag in `keep_tags`. Returns count removed.
        """
        keep = set(keep_tags or [])
        cutoff = time.time() - older_than_days * 86400.0
        removed = 0
        for exp in list(self.iter_experiments()):
            # Parse ISO created_at → epoch. Fallback: keep (safer than discarding)
            ts = _parse_iso_utc(exp.created_at)
            if ts is None or ts >= cutoff:
                continue
            if any(t in keep for t in exp.tags):
                continue
            if self.delete(exp.id):
                removed += 1
        return removed

    # ---- Audit ----

    def audit_log(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", _iso_utc_now())
        # Sanitize — an experiment metric with inf/nan would otherwise
        # produce a JSONL line stdlib json.loads() can't re-parse on read.
        line = json.dumps(_sanitize_json(event), sort_keys=True) + "\n"
        with self._lock:
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def read_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not self._audit_path.exists():
            return []
        lines = self._audit_path.read_text().splitlines()[-limit:]
        out: List[Dict[str, Any]] = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def log_review(self, exp_id: str, review: Dict[str, Any]) -> None:
        """Append a review record to review_history.jsonl. Phase 7 wiring."""
        record = {"exp_id": exp_id, "ts": _iso_utc_now(), "review": review}
        # Sanitize — reviewer output can contain non-finite floats from MC
        # evidence on pathological inputs, which would otherwise break the
        # round-trip when self_retrospective() reads the log back.
        with self._lock:
            with self._review_history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_sanitize_json(record), sort_keys=True) + "\n")


# ═══════════════════════════════════════════════════════════════
# Sweep + compare
# ═══════════════════════════════════════════════════════════════

def sweep_experiment(
    base_config: BacktestConfig,
    param: str,
    values: List[float],
    pair: Optional[str] = None,
    store: Optional[ExperimentStore] = None,
    triggered_by: str = "cli",
    tags: Optional[List[str]] = None,
) -> List[Experiment]:
    """Serial sweep of `param` over `values` on `pair` (defaults to first pair).

    Each value produces one Experiment; all share the same base except for the
    swept param's override. Returns the list of completed experiments.

    Parallelization happens at Phase 6 via BacktestWorkerPool — serial here
    keeps the library usable without the agent process mounted.
    """
    if not values:
        return []
    target_pair = pair or base_config.pairs[0]
    out: List[Experiment] = []
    for v in values:
        overrides = dict(base_config.param_overrides)
        pair_ov = dict(overrides.get(target_pair, {}))
        pair_ov[param] = float(v)
        overrides[target_pair] = pair_ov
        # Clear stamps so finalize_stamps recomputes param_hash against the
        # new overrides — replace() otherwise copies the parent hash verbatim.
        cfg = replace(
            base_config,
            param_overrides_json=json.dumps(overrides),
            param_hash="",
            created_at="",
        )
        exp = new_experiment(
            name=f"sweep:{param}={v}",
            config=cfg,
            hypothesis=f"Sweep {param} on {target_pair} → {v}",
            triggered_by=triggered_by,
            overrides={target_pair: {param: v}},
            tags=(tags or []) + ["sweep", f"param:{param}"],
        )
        run_experiment(exp, store=store)
        out.append(exp)
    return out


@dataclass
class ComparisonRow:
    experiment_id: str
    name: str
    total_trades: int
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    profit_factor: float


@dataclass
class ComparisonReport:
    experiments: List[str]           # experiment IDs in input order
    rows: List[ComparisonRow]
    winner_per_metric: Dict[str, str]  # metric name → winning experiment_id
    # Bootstrap p-values for diff-of-means on cross-tick equity differences
    pairwise_sharpe_p_values: Dict[Tuple[str, str], float] = field(default_factory=dict)


def _finite_or_none(v: Any) -> Optional[float]:
    """True if `v` is a real finite number. Handles None safely — persisted
    experiments may round-trip non-finite floats back as None via
    `_sanitize_json`."""
    if v is None:
        return None
    try:
        return v if math.isfinite(v) else None
    except TypeError:
        return None


def _compare_metric_winner(rows: List[ComparisonRow], metric: str, higher_better: bool) -> str:
    """Return experiment_id with the best value for `metric`. Non-finite and
    zero-trade rows are eligible only if there are no alternatives."""
    eligible = [r for r in rows
                if r.total_trades > 0 and _finite_or_none(getattr(r, metric)) is not None]
    pool = eligible if eligible else rows
    # Pick the first row that has a comparable value so the seed isn't None.
    best = next((r for r in pool if _finite_or_none(getattr(r, metric)) is not None), pool[0])
    for r in pool:
        v = _finite_or_none(getattr(r, metric))
        cur = _finite_or_none(getattr(best, metric))
        if v is None:
            continue
        if cur is None or (higher_better and v > cur) or (not higher_better and v < cur):
            best = r
    return best.experiment_id


def compare(experiments: List[Experiment]) -> ComparisonReport:
    """Rank a set of experiments across terminal equity, Sharpe, max DD, and
    profit factor. Returns `ComparisonReport` with per-metric winners.

    Bootstrap p-values on per-tick returns require synchronized equity curves
    (same candle count per pair). When curves diverge in length (e.g. sweeps
    over different data_source_params), p-values default to 1.0 for that pair.
    """
    if not experiments:
        return ComparisonReport(experiments=[], rows=[], winner_per_metric={})

    rows: List[ComparisonRow] = []
    for e in experiments:
        if e.result is None:
            rows.append(ComparisonRow(
                experiment_id=e.id, name=e.name,
                total_trades=0, total_return_pct=0.0,
                sharpe=0.0, max_drawdown_pct=0.0, profit_factor=0.0,
            ))
            continue
        m = e.result.metrics
        # Persisted metrics can round-trip non-finite floats back as None
        # (see _sanitize_json). Normalise every comparable field through
        # _finite_or_none → 0.0 so ComparisonRow stays strictly numeric.
        def _num(v):
            f = _finite_or_none(v)
            return f if f is not None else 0.0
        rows.append(ComparisonRow(
            experiment_id=e.id, name=e.name,
            total_trades=m.total_trades or 0,
            total_return_pct=_num(m.total_return_pct),
            sharpe=_num(m.sharpe),
            max_drawdown_pct=_num(m.max_drawdown_pct),
            profit_factor=_num(m.profit_factor),
        ))

    winners = {
        "total_return_pct": _compare_metric_winner(rows, "total_return_pct", higher_better=True),
        "sharpe":           _compare_metric_winner(rows, "sharpe", higher_better=True),
        "max_drawdown_pct": _compare_metric_winner(rows, "max_drawdown_pct", higher_better=False),
        "profit_factor":    _compare_metric_winner(rows, "profit_factor", higher_better=True),
    }

    # Pairwise bootstrap p-values on per-tick return diffs (Sharpe proxy).
    # Only compute when curves align; otherwise store 1.0 (non-significant).
    ps: Dict[Tuple[str, str], float] = {}
    for i, a in enumerate(experiments):
        for b in experiments[i + 1:]:
            ps[(a.id, b.id)] = _paired_return_p_value(a, b)
    return ComparisonReport(
        experiments=[e.id for e in experiments],
        rows=rows,
        winner_per_metric=winners,
        pairwise_sharpe_p_values=ps,
    )


def _paired_return_p_value(a: Experiment, b: Experiment, n_iter: int = 500, seed: int = 42) -> float:
    """Paired bootstrap on per-tick return differences. 1.0 if curves aren't aligned."""
    if a.result is None or b.result is None:
        return 1.0
    ea = _flatten_equity(a.result)
    eb = _flatten_equity(b.result)
    if len(ea) != len(eb) or len(ea) < 3:
        return 1.0
    ra = _rets(ea)
    rb = _rets(eb)
    diffs = [x - y for x, y in zip(ra, rb)]
    if not diffs:
        return 1.0
    import random as _random
    rng = _random.Random(seed)
    n = len(diffs)
    # Bootstrap mean-diff distribution under H0 (center the diffs)
    centered = [d - statistics.fmean(diffs) for d in diffs]
    observed = statistics.fmean(diffs)
    more_extreme = 0
    for _ in range(n_iter):
        sample_mean = sum(centered[rng.randint(0, n - 1)] for _ in range(n)) / n
        if abs(sample_mean) >= abs(observed):
            more_extreme += 1
    return more_extreme / n_iter


def _flatten_equity(result: BacktestResult) -> List[float]:
    """Sum per-pair equity at each tick. Assumes all pairs share tick count.
    Persisted equity curves can contain None for ticks where a non-finite
    value was sanitised on save — treat those as 0.0 so downstream stats
    stay numeric."""
    curves = list(result.equity_curve.values())
    if not curves:
        return []
    n = min(len(c) for c in curves)
    out: List[float] = []
    for i in range(n):
        total = 0.0
        for p in range(len(curves)):
            v = curves[p][i]
            if v is not None and isinstance(v, (int, float)) and math.isfinite(v):
                total += float(v)
        out.append(total)
    return out


def _rets(equity: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        cur = equity[i]
        # Defensive: None / non-numeric / non-finite values become a flat 0
        # tick instead of propagating a TypeError up through the bootstrap.
        if (prev is None or cur is None
                or not isinstance(prev, (int, float)) or not isinstance(cur, (int, float))
                or not math.isfinite(prev) or not math.isfinite(cur)
                or prev <= 0):
            out.append(0.0)
        else:
            out.append((cur - prev) / prev)
    return out


# ═══════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to `path` atomically (temp file + os.replace).

    Non-finite floats (inf/nan) are pre-sanitized to null — json.dump's
    `default=` hook only fires on non-serializable types, and Python floats
    including inf are serializable (as 'Infinity', which breaks strict JSON
    parsers). So we walk the structure ourselves.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_sanitize_json(data), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        raise


def _sanitize_json(obj: Any) -> Any:
    """Recursively replace inf/nan floats with None so the result is
    strict-JSON-parseable; preserve all other structure."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def _parse_iso_utc(s: str) -> Optional[float]:
    """ISO 8601 UTC → epoch seconds. Returns None on failure."""
    if not s:
        return None
    try:
        t = time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return time.mktime(t) - time.timezone
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import tempfile as _tempfile
    tmp = _tempfile.mkdtemp(prefix="hydra-exp-smoke-")
    print(f"[exp smoke] store root: {tmp}")
    store = ExperimentStore(root=Path(tmp))

    # Preset-driven run
    cfg, ov = build_config_from_preset("divergent", pairs=("SOL/USD",), n_candles=300, seed=1)
    exp = new_experiment(
        name="smoke-divergent",
        config=cfg,
        hypothesis="Loosened gates → more trades, sharpe likely lower",
        base_preset="divergent",
        overrides=ov,
    )
    run_experiment(exp, store=store, with_monte_carlo=True, mc_iter=50)
    print(f"[exp smoke] {exp.name} status={exp.status} "
          f"trades={exp.result.metrics.total_trades} sharpe={exp.result.metrics.sharpe:.3f}")

    # Sweep
    print("[exp smoke] sweep confidence…")
    results = sweep_experiment(
        cfg, param="min_confidence_threshold",
        values=[0.55, 0.65, 0.75], store=store,
    )
    for r in results:
        print(f"  {r.name}: trades={r.result.metrics.total_trades}, "
              f"sharpe={r.result.metrics.sharpe:.3f}")

    # Compare
    report = compare(results)
    print(f"[exp smoke] winners: {report.winner_per_metric}")
    print(f"[exp smoke] audit log entries: {len(store.read_audit())}")
