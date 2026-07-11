#!/usr/bin/env python3
"""
HYDRA Backtest — Backend Bridge (Phase 6 of v2.10.0 backtest platform).

Wires Phase 1-4 backtest infrastructure into the live agent process:
  * `BacktestWorkerPool` — bounded daemon-thread pool that runs queued
    backtests off the live tick loop.
  * `mount_backtest_routes` — registers inbound WS handlers on an
    augmented DashboardBroadcaster (type-discriminated JSON messages).

See docs/BACKTEST_SPEC.md §6.6 and invariants I1-I6, I11.

Live-safety guarantees
----------------------
- Workers are always daemon threads (I4). The live agent can exit
  cleanly; the pool drops in-flight work at shutdown.
- Every worker run is wrapped in try/except; errors go to
  hydra_backtest_errors.log and never propagate to the agent (I5).
- Workers construct their own HydraEngine / CrossPairCoordinator
  instances via BacktestRunner — no reference to live engine state (I2).
- Each submit() creates a new cancel_token the worker respects on every
  tick — cancel() signals without waiting on the worker to check in.
- Queue size, max_workers, and per-experiment max_ticks provide bounded
  compute (I11).
- The entire module is no-op when the agent sets HYDRA_BACKTEST_DISABLED=1
  (kill switch, I6) — the caller simply skips mount + construction.

Broadcasting
------------
Messages are emitted via the broadcaster's new `broadcast_message(type,
payload)` path (Phase 6 DashboardBroadcaster refactor). Old dashboards
ignore unknown message shapes; the Phase 8 UI adds a type-switch so the
observer modal picks up progress/result/error streams.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hydra_backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    _iso_utc_now,
)
from hydra_experiments import (
    Experiment,
    ExperimentStore,
    new_experiment,
)

# ═══════════════════════════════════════════════════════════════
# Worker pool
# ═══════════════════════════════════════════════════════════════

ERROR_LOG_NAME = "hydra_backtest_errors.log"
DEFAULT_QUEUE_DEPTH = 20
DEFAULT_MAX_WORKERS = 2
MAX_WORKERS_HARD_CAP = 4     # I11 budget — bounded CPU per worker


@dataclass
class _PoolJob:
    """One queued unit of work. Stored experiment lives in the ExperimentStore;
    we carry only the id to keep the queue small."""
    experiment_id: str


class BacktestWorkerPool:
    """Bounded daemon-thread pool that runs backtests off the live tick loop.

    Thread-safety:
      - All methods may be called from any thread (brain/tool dispatcher,
        WS handler thread, live tick thread).
      - Internal state (`_cancel_tokens`, `_running`, status cache) is
        protected by `self._lock`.
      - The underlying queue.Queue is already thread-safe.

    Shutdown:
      - `shutdown()` sets the stop flag and enqueues a sentinel per worker
        to wake blocked `queue.get()` calls, then joins.
      - Workers check the shutdown flag at every tick via `cancel_token`
        so an in-flight experiment can be abandoned mid-run.
      - Daemon threads ensure the process can still exit cleanly even if
        a worker is stuck on an external I/O call.
    """

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        store: Optional[ExperimentStore] = None,
        broadcaster: Optional[Any] = None,
        reviewer: Optional[Any] = None,
        queue_depth: int = DEFAULT_QUEUE_DEPTH,
        error_log_dir: Optional[Path] = None,
        progress_every_n_ticks: int = 5,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be ≥ 1")
        if max_workers > MAX_WORKERS_HARD_CAP:
            # I11 budget — silently clamp rather than raise so callers
            # passing an env-based configured value don't crash the agent.
            print(f"[BACKTEST] max_workers={max_workers} exceeds hard cap "
                  f"{MAX_WORKERS_HARD_CAP}; clamping.", flush=True)
            max_workers = MAX_WORKERS_HARD_CAP

        self.max_workers = max_workers
        self.store = store if store is not None else ExperimentStore()
        self.broadcaster = broadcaster
        self.reviewer = reviewer                    # Phase 7; None for now
        self.progress_every_n_ticks = max(1, int(progress_every_n_ticks))
        self._queue: "queue.Queue[Optional[_PoolJob]]" = queue.Queue(maxsize=queue_depth)
        self._cancel_tokens: Dict[str, threading.Event] = {}
        self._status_cache: Dict[str, str] = {}     # experiment_id -> last known status
        self._completed_at: Dict[str, float] = {}
        self._PRUNE_AFTER_SEC = 60.0
        self._running = True
        self._lock = threading.Lock()
        self._workers: List[threading.Thread] = []
        self._error_log_dir = error_log_dir or Path(".")
        self._sentinel = _PoolJob(experiment_id="__SHUTDOWN__")

        for i in range(max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"backtest-worker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    # ─── submission ───

    def submit_config(
        self,
        config: BacktestConfig,
        triggered_by: str = "cli",
        hypothesis: str = "",
        tags: Optional[List[str]] = None,
    ) -> str:
        """Convenience: build an Experiment from a config and enqueue it."""
        exp = new_experiment(
            name=config.name,
            config=config,
            hypothesis=hypothesis,
            triggered_by=triggered_by,
            tags=tags,
        )
        return self.submit_experiment(exp)

    def submit_experiment(self, exp: Experiment) -> str:
        """Enqueue a pre-built Experiment. Returns its id.

        Raises `queue.Full` if the queue is saturated (live-safety: we
        refuse rather than block the caller's thread).
        """
        with self._lock:
            if not self._running:
                raise RuntimeError("worker pool is shut down")
            exp.status = "pending"
            self._cancel_tokens[exp.id] = threading.Event()
            self._status_cache[exp.id] = "pending"
        self.store.save(exp)
        self.store.audit_log({
            "event": "submit",
            "id": exp.id,
            "triggered_by": exp.triggered_by,
            "hypothesis": exp.hypothesis,
        })
        # queue.put with a short timeout — never indefinitely blocks the
        # caller. If saturated the exception bubbles up (~= backpressure).
        self._queue.put(_PoolJob(experiment_id=exp.id), timeout=1.0)
        return exp.id

    def cancel(self, experiment_id: str) -> bool:
        """Signal the worker to abandon this run at the next tick.

        Returns True if the token existed (queued or running), False if
        the id was unknown. Does NOT wait for the worker to acknowledge.
        """
        with self._lock:
            tok = self._cancel_tokens.get(experiment_id)
            if tok is None:
                return False
            tok.set()
        self.store.audit_log({"event": "cancel", "id": experiment_id})
        return True

    def status(self, experiment_id: str) -> Dict[str, Any]:
        with self._lock:
            cached = self._status_cache.get(experiment_id)
        if cached is None:
            try:
                exp = self.store.load(experiment_id)
                return {"id": experiment_id, "status": exp.status,
                        "cached": False}
            except KeyError:
                return {"id": experiment_id, "status": "unknown", "cached": False}
        return {"id": experiment_id, "status": cached, "cached": True}

    def snapshot(self) -> Dict[str, Any]:
        """Compact view of the pool for diagnostics + dashboard."""
        with self._lock:
            return {
                "running": self._running,
                "max_workers": self.max_workers,
                "queue_size": self._queue.qsize(),
                "queue_depth_cap": self._queue.maxsize,
                "in_flight": {eid: s for eid, s in self._status_cache.items()
                              if s == "running"},
                "worker_threads_alive": sum(1 for t in self._workers if t.is_alive()),
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting new work; signal all in-flight cancel tokens;
        enqueue one sentinel per worker to wake blocked queue.get calls;
        join up to `timeout` seconds each. Daemon threads guarantee we
        can exit even if a worker is wedged."""
        with self._lock:
            self._running = False
            # Signal every pending experiment so they return `cancelled`
            for tok in self._cancel_tokens.values():
                tok.set()
        for _ in self._workers:
            try:
                self._queue.put(self._sentinel, timeout=0.5)
            except queue.Full:
                # Queue full; the sentinel can wait for one worker to drain
                # a slot. At worst we rely on daemon teardown.
                pass
        for t in self._workers:
            t.join(timeout=timeout)

    # ─── internals ───

    def _worker_loop(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                if not self._running:
                    return
                continue
            if job is None or job.experiment_id == self._sentinel.experiment_id:
                self._queue.task_done()
                return
            try:
                self._run_one(job.experiment_id)
            except Exception as e:
                # Defense-in-depth; _run_one has its own try/except but a
                # bug in error handling must not kill the worker.
                self._log_error(job.experiment_id, e)
            finally:
                self._queue.task_done()

    def _run_one(self, experiment_id: str) -> None:
        try:
            exp = self.store.load(experiment_id)
        except KeyError:
            self._log_error(experiment_id,
                            RuntimeError(f"experiment vanished from store: {experiment_id}"))
            self._prune_terminal()
            return

        with self._lock:
            cancel_tok = self._cancel_tokens.setdefault(
                experiment_id, threading.Event()
            )
            self._status_cache[experiment_id] = "running"

        exp.status = "running"
        self.store.save(exp)
        self._broadcast_progress(experiment_id, None, 0, stage="started")

        tick_counter = {"n": 0}
        last_broadcast = {"t": 0.0}

        def on_tick(state: Dict[str, Any]) -> None:
            # Throttled progress stream: every N ticks OR every 500ms,
            # whichever comes first. Prevents WS spam on high-speed replays.
            tick_counter["n"] = state.get("tick", tick_counter["n"] + 1)
            now = time.time()
            if (tick_counter["n"] % self.progress_every_n_ticks == 0
                    or now - last_broadcast["t"] > 0.5):
                last_broadcast["t"] = now
                self._broadcast_progress(
                    experiment_id, state, tick_counter["n"], stage="running",
                )

        try:
            runner = BacktestRunner(exp.config)
            result = runner.run(on_tick=on_tick, cancel_token=cancel_tok)
            exp.result = result
            exp.status = result.status
            self.store.save(exp)

            with self._lock:
                self._status_cache[experiment_id] = result.status
                self._completed_at[experiment_id] = time.time()

            if result.status == "cancelled":
                self._broadcast_progress(experiment_id, None,
                                         tick_counter["n"], stage="cancelled")
            else:
                self._broadcast_result(experiment_id, exp)

            # Phase 7 wiring: run the reviewer if present. Failures here
            # must not mark the experiment failed — reviewer bugs are
            # orthogonal to the backtest's correctness.
            if self.reviewer is not None and result.status == "complete":
                try:
                    review = self.reviewer.review(exp)
                    exp.review = review
                    self.store.save(exp)
                    self._broadcast_review(experiment_id, review)
                except Exception as e:
                    self._log_error(experiment_id, e, note="reviewer")

        except Exception as e:
            exp.status = "failed"
            if exp.result is None:
                exp.result = BacktestResult(
                    config=exp.config, status="failed",
                    started_at=_iso_utc_now(),
                    completed_at=_iso_utc_now(),
                )
            exp.result.errors.append({
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            })
            self.store.save(exp)
            with self._lock:
                self._status_cache[experiment_id] = "failed"
                self._completed_at[experiment_id] = time.time()
            self._broadcast_error(experiment_id, str(e))
            self._log_error(experiment_id, e)
        finally:
            self._prune_terminal()

    def _prune_terminal(self) -> None:
        """Remove stale entries from _cancel_tokens, _status_cache, and
        _completed_at for experiments that finished more than
        _PRUNE_AFTER_SEC ago."""
        cutoff = time.time() - self._PRUNE_AFTER_SEC
        with self._lock:
            expired = [eid for eid, t in self._completed_at.items() if t < cutoff]
            for eid in expired:
                self._cancel_tokens.pop(eid, None)
                self._status_cache.pop(eid, None)
                self._completed_at.pop(eid, None)

    # ─── broadcasting ───

    def _broadcast(self, msg_type: str, payload: Dict[str, Any]) -> None:
        if self.broadcaster is None:
            return
        try:
            self.broadcaster.broadcast_message(msg_type, payload)
        except Exception as e:
            # Broadcaster faults never crash the worker.
            self._log_error(payload.get("experiment_id", "?"), e,
                            note=f"broadcast:{msg_type}")

    def _broadcast_progress(
        self, experiment_id: str, state: Optional[Dict[str, Any]],
        tick: int, stage: str,
    ) -> None:
        self._broadcast("backtest_progress", {
            "experiment_id": experiment_id,
            "tick": tick,
            "stage": stage,
            "dashboard_state": state,
        })

    def _broadcast_result(self, experiment_id: str, exp: Experiment) -> None:
        summary: Dict[str, Any] = {
            "experiment_id": experiment_id,
            "status": exp.status,
            "name": exp.name,
            "hypothesis": exp.hypothesis,
            "triggered_by": exp.triggered_by,
        }
        if exp.result is not None:
            m = exp.result.metrics
            import math as _m
            summary["metrics"] = {
                "total_trades": m.total_trades,
                "total_return_pct": round(m.total_return_pct, 4),
                "sharpe": round(m.sharpe, 4),
                "max_drawdown_pct": round(m.max_drawdown_pct, 4),
                "profit_factor": round(m.profit_factor, 4) if _m.isfinite(m.profit_factor) else None,
                "win_rate_pct": round(m.win_rate_pct, 2),
                "fills": exp.result.fills,
                "rejects": exp.result.rejects,
            }
        self._broadcast("backtest_result", summary)

    def _broadcast_review(self, experiment_id: str, review: Any) -> None:
        # Keep the review payload opaque at Phase 6 — Phase 7 defines the
        # schema and the dashboard parses it then.
        self._broadcast("backtest_review", {
            "experiment_id": experiment_id,
            "review": review if isinstance(review, dict) else str(review),
        })

    def _broadcast_error(self, experiment_id: str, message: str) -> None:
        self._broadcast("error", {
            "channel": "backtest",
            "experiment_id": experiment_id,
            "message": message,
        })

    # ─── error logging ───

    def _log_error(self, experiment_id: str, e: Exception, note: str = "") -> None:
        """Append a single record to hydra_backtest_errors.log.

        Best-effort — even a filesystem failure here must not crash the
        worker. Errors are also on the experiment's `result.errors` list,
        which is the durable audit surface.
        """
        try:
            path = self._error_log_dir / ERROR_LOG_NAME
            record = {
                "ts": _iso_utc_now(),
                "experiment_id": experiment_id,
                "note": note,
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")


# ═══════════════════════════════════════════════════════════════
# WS route mounting (inbound message handlers)
# ═══════════════════════════════════════════════════════════════

def mount_backtest_routes(
    broadcaster: Any,
    pool: BacktestWorkerPool,
    dispatcher: Optional[Any] = None,
) -> None:
    """Register inbound WS handlers on the augmented DashboardBroadcaster.

    Handlers receive `(payload_dict, caller_label)` and return an optional
    reply dict that the broadcaster sends back over the WS channel (as a
    JSON-encoded `{type, ...reply}` message).

    Registered message types (see docs/BACKTEST_SPEC.md §5.6):
      backtest_start           — run a backtest from the dashboard
      backtest_cancel          — cancel a queued/running experiment
      experiment_list_request  — "list_experiments via WS" for UI
      experiment_get_request   — single experiment by id
      experiment_delete        — DENIED (write path forbidden per I8)
      review_request           — stub for Phase 7 reviewer hook

    If `dispatcher` is provided, dashboard-initiated experiments go through
    it so they share the same audit + quota path the brain uses. Callers
    without a dispatcher fall back to direct pool.submit_config().
    """
    if broadcaster is None or not hasattr(broadcaster, "register_handler"):
        return

    def _start(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Expected payload: {config: BacktestConfig-dict, triggered_by?, hypothesis?}
        cfg_dict = payload.get("config")
        if not cfg_dict:
            return {"success": False, "error": "config required"}
        try:
            # Rebuild BacktestConfig from known fields — reuse the
            # Experiment loader's filter to stay forward-compatible.
            from hydra_experiments import _CONFIG_FIELDS
            cfg_fields = {k: v for k, v in cfg_dict.items() if k in _CONFIG_FIELDS}
            if "pairs" in cfg_fields and isinstance(cfg_fields["pairs"], list):
                cfg_fields["pairs"] = tuple(cfg_fields["pairs"])
            cfg = BacktestConfig(**cfg_fields)
        except Exception as e:
            return {"success": False, "error": f"invalid config: {e}"}
        try:
            eid = pool.submit_config(
                config=cfg,
                triggered_by=payload.get("triggered_by") or "dashboard",
                hypothesis=payload.get("hypothesis") or "dashboard-initiated",
                tags=payload.get("tags") or ["caller:dashboard"],
            )
            return {"success": True, "experiment_id": eid}
        except queue.Full:
            return {"success": False, "error": "queue saturated; retry"}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    def _cancel(payload: Dict[str, Any]) -> Dict[str, Any]:
        eid = payload.get("experiment_id")
        if not eid:
            return {"success": False, "error": "experiment_id required"}
        ok = pool.cancel(eid)
        return {"success": ok, "experiment_id": eid}

    def _list(payload: Dict[str, Any]) -> Dict[str, Any]:
        limit = int(payload.get("limit") or 50)
        try:
            rows = pool.store.list(limit=limit)
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "experiments": [_compact(e) for e in rows]}

    def _get(payload: Dict[str, Any]) -> Dict[str, Any]:
        eid = payload.get("experiment_id")
        if not eid:
            return {"success": False, "error": "experiment_id required"}
        try:
            exp = pool.store.load(eid)
        except KeyError:
            return {"success": False, "error": "not found"}
        return {"success": True, "experiment": exp.to_dict()}

    def _deny_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
        # I8: dashboard cannot delete experiments. This is a human-moderated
        # action; it goes through journal_maintenance.py or direct CLI.
        return {"success": False,
                "error": "delete is not exposed via WS; use the CLI tool"}

    def _compare(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Phase 10 dashboard compare view: takes 2-8 experiment ids and
        # returns per-metric winners + paired bootstrap p-values via the
        # Phase 3 `compare()` function.
        ids: List[str] = list(payload.get("experiment_ids") or [])
        if len(ids) < 2:
            return {"success": False, "error": "at least 2 experiment_ids required"}
        if len(ids) > 8:
            return {"success": False, "error": "compare supports max 8 experiments"}
        from hydra_experiments import compare as _compare_fn
        experiments = []
        missing = []
        for eid in ids:
            try:
                experiments.append(pool.store.load(eid))
            except KeyError:
                missing.append(eid)
        if missing:
            return {"success": False, "error": "experiments missing", "missing_ids": missing}
        from dataclasses import asdict as _asdict
        try:
            report = _compare_fn(experiments)
        except Exception as e:
            # Compare() already has per-field None guards; this catches any
            # unexpected shape (e.g. a corrupt experiment JSON from an older
            # version) and returns a readable message instead of leaking a
            # bare TypeError to the user.
            import traceback as _tb
            _tb.print_exc()
            return {
                "success": False,
                "error": f"Comparison could not be computed: {e.__class__.__name__}. "
                         f"One or more experiments have corrupt / legacy metrics "
                         f"(likely from before the v2.10 sanitiser fix). Re-run "
                         f"those experiments to refresh them, or pick a different "
                         f"set.",
            }
        return {
            "success": True,
            "experiments": report.experiments,
            "winner_per_metric": report.winner_per_metric,
            "rows": [_asdict(r) for r in report.rows],
            # Tuple keys don't JSON-serialize; flatten to "a__b" → p
            "pairwise_sharpe_p_values": {
                f"{a}__{b}": p
                for (a, b), p in report.pairwise_sharpe_p_values.items()
            },
        }

    def _review_request(payload: Dict[str, Any]) -> Dict[str, Any]:
        eid = payload.get("experiment_id")
        if not eid:
            return {"success": False, "error": "experiment_id required"}
        # Phase 7 reviewer will hook here. For now we just report no reviewer.
        if pool.reviewer is None:
            return {"success": False, "error": "reviewer not configured"}
        try:
            exp = pool.store.load(eid)
        except KeyError:
            return {"success": False, "error": "not found"}
        try:
            review = pool.reviewer.review(exp)
            exp.review = review
            pool.store.save(exp)
            pool._broadcast_review(eid, review)
            return {"success": True, "experiment_id": eid}
        except Exception as e:
            return {"success": False, "error": str(e)}

    broadcaster.register_handler("backtest_start", _start)
    broadcaster.register_handler("backtest_cancel", _cancel)
    broadcaster.register_handler("experiment_list_request", _list)
    broadcaster.register_handler("experiment_get_request", _get)
    broadcaster.register_handler("experiment_compare_request", _compare)
    broadcaster.register_handler("experiment_delete", _deny_delete)
    broadcaster.register_handler("review_request", _review_request)

    # ── Research tab WS routes ────────────────────────────────────────
    # Dataset coverage is read-only against hydra_history.sqlite.
    # Lab walk-forward runs on a daemon thread and streams progress (never
    # blocks the WS loop for multi-minute OOS work).

    def _research_dataset_coverage(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Read-only: per (pair, grain_sec) coverage of the canonical store."""
        try:
            from hydra_history_store import HistoryStore
            db_path = os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite")
            store = HistoryStore(db_path)
            rows = []
            for pair, grain_sec in store.list_pairs():
                c = store.coverage(pair, grain_sec)
                rows.append({
                    "pair": pair,
                    "grain_sec": grain_sec,
                    "candle_count": c.candle_count,
                    "first_ts": c.first_ts,
                    "last_ts": c.last_ts,
                    "gap_count": c.gap_count,
                    "max_gap_sec": c.max_gap_sec,
                })
            return {"success": True, "data": rows}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    def _research_lab_run(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Research Lab walk-forward — async via daemon thread, streams progress."""
        import uuid
        from hydra_history_store import HistoryStore
        from hydra_walk_forward import (
            run_walk_forward, WalkForwardSpec, FoldMetrics, build_quarterly_folds,
        )
        pair = payload.get("pair")
        if pair not in ("BTC/USD", "SOL/USD", "SOL/BTC"):
            return {"success": False, "error": "pair required (BTC/USD|SOL/USD|SOL/BTC)"}
        baseline_params = payload.get("baseline_params") or {}
        candidate_params = payload.get("candidate_params") or {}
        if not isinstance(baseline_params, dict) or not isinstance(candidate_params, dict):
            return {"success": False, "error": "baseline_params and candidate_params must be objects"}

        db_path = os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite")
        store = HistoryStore(db_path)
        cov = store.coverage(pair, 3600)
        if cov.first_ts is None:
            return {"success": False, "error": f"no history for {pair}"}

        job_id = uuid.uuid4().hex[:12]
        spec_dict = payload.get("spec") or {}
        spec = WalkForwardSpec(**{k: v for k, v in spec_dict.items()
                                  if k in ("fold_kind", "is_lookback_quarters", "min_oos_trades")})
        # Probe fold count up-front for the progress UI.
        n_folds = len(build_quarterly_folds(cov.first_ts, cov.last_ts, spec))

        def _broadcast(msg_type: str, data: Dict[str, Any]) -> None:
            try:
                broadcaster.broadcast_message(msg_type, {**data, "job_id": job_id})
            except Exception as e:
                print(f"  [LAB] broadcast error: {type(e).__name__}: {e}")

        def _runner_factory(side: str, overrides: Dict[str, float]):
            # OOS isolation (option 3): warmup-pad before oos_start so the
            # engine's warmup_candles=50 lookback is covered before scoring.
            _WARMUP_PAD_CANDLES = 60
            def _run(pair_arg, params, fold) -> "FoldMetrics":
                from hydra_backtest import BacktestConfig, BacktestRunner
                warmup_padded_start = max(
                    fold.is_start,
                    fold.oos_start - _WARMUP_PAD_CANDLES * 3600,
                )
                cfg = BacktestConfig(
                    name=f"lab-{job_id}-{side}-{fold.idx}",
                    pairs=(pair_arg,),
                    data_source="sqlite",
                    data_source_params_json=json.dumps({
                        "db_path": db_path, "grain_sec": 3600,
                        "start_ts": warmup_padded_start, "end_ts": fold.oos_end,
                    }),
                    param_overrides_json=json.dumps({pair_arg: overrides}),
                )
                result = BacktestRunner(cfg).run()
                m = result.metrics
                return FoldMetrics(
                    sharpe=m.sharpe,
                    total_return_pct=m.total_return_pct,
                    max_dd_pct=m.max_drawdown_pct,
                    fee_adj_return_pct=getattr(m, "fee_adj_return_pct",
                                              m.total_return_pct),
                    n_trades=m.total_trades,
                )
            return _run

        # Walk-forward calls runner(pair, params, fold) once per fold per side.
        # We tag the params dicts with _lab_side to route inside the shared
        # wrapper. HydraEngine.apply_tuned_params ignores unknown keys, so
        # _lab_side is harmless when passed through to the engine.
        baseline_tagged = {**baseline_params, "_lab_side": "baseline"}
        candidate_tagged = {**candidate_params, "_lab_side": "candidate"}
        baseline_runner_fn = _runner_factory("baseline", baseline_params)
        candidate_runner_fn = _runner_factory("candidate", candidate_params)

        def _runner(pair_arg, params, fold) -> "FoldMetrics":
            side = params.get("_lab_side", "?")
            fn = baseline_runner_fn if side == "baseline" else candidate_runner_fn
            metrics = fn(pair_arg, params, fold)
            # Per-fold-per-side progress broadcast.
            _broadcast("research_lab_progress", {
                "pair": pair_arg, "fold_idx": fold.idx, "n_folds": n_folds,
                "side": side, "is_start": fold.is_start, "is_end": fold.is_end,
                "oos_start": fold.oos_start, "oos_end": fold.oos_end,
                "metrics": {
                    "sharpe": metrics.sharpe,
                    "total_return_pct": metrics.total_return_pct,
                    "max_dd_pct": metrics.max_dd_pct,
                    "fee_adj_return_pct": metrics.fee_adj_return_pct,
                    "n_trades": metrics.n_trades,
                },
            })
            return metrics

        def _worker():
            try:
                _broadcast("research_lab_progress", {
                    "phase": "started", "pair": pair, "n_folds": n_folds,
                    "baseline_params": baseline_params,
                    "candidate_params": candidate_params,
                })
                result = run_walk_forward(
                    pair=pair, history_start_ts=cov.first_ts,
                    history_end_ts=cov.last_ts,
                    baseline_params=baseline_tagged,
                    candidate_params=candidate_tagged,
                    spec=spec, runner=_runner,
                )
                _broadcast("research_lab_result", {
                    "phase": "done",
                    "pair": pair,
                    "skipped_folds": result.skipped_folds,
                    "n_folds_completed": len(result.folds),
                    "wilcoxon": {
                        m: {
                            "verdict": v.verdict,
                            "p_value": v.p_value,
                            "candidate_wins": v.candidate_wins,
                            "n": v.n,
                            "median_delta": v.median_delta,
                        } for m, v in result.wilcoxon.items()
                    },
                    "folds": [
                        {
                            "idx": fr.fold.idx,
                            "oos_start": fr.fold.oos_start,
                            "oos_end": fr.fold.oos_end,
                            "deltas": fr.deltas,
                            "baseline": {
                                "sharpe": fr.baseline.sharpe,
                                "total_return_pct": fr.baseline.total_return_pct,
                                "n_trades": fr.baseline.n_trades,
                            },
                            "candidate": {
                                "sharpe": fr.candidate.sharpe,
                                "total_return_pct": fr.candidate.total_return_pct,
                                "n_trades": fr.candidate.n_trades,
                            },
                        }
                        for fr in result.folds
                    ],
                })
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                _broadcast("research_lab_result", {
                    "phase": "error",
                    "error": f"{type(e).__name__}: {e}",
                })

        threading.Thread(target=_worker, name=f"LabRun-{job_id}",
                         daemon=True).start()
        return {"success": True, "job_id": job_id, "n_folds": n_folds, "pair": pair}

    def _research_params_current(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return the tunable param schema (bounds + current values per pair).

        Reads PARAM_BOUNDS from hydra_tuner and the per-pair current params
        from hydra_params_<pair>.json. Falls back to DEFAULT_PARAMS when no
        per-pair file exists. Pure read; no mutation."""
        try:
            from hydra_tuner import PARAM_BOUNDS, DEFAULT_PARAMS, ParameterTracker
            pair = payload.get("pair") or "BTC/USD"
            tracker = ParameterTracker(pair)
            current = tracker.get_tunable_params()
            schema = {}
            for name, (lo, hi) in PARAM_BOUNDS.items():
                schema[name] = {
                    "min": lo,
                    "max": hi,
                    "default": DEFAULT_PARAMS.get(name),
                    "current": current.get(name, DEFAULT_PARAMS.get(name)),
                    # Step size: 0.001 for fine ratios, 0.01 for confidences,
                    # 1.0 for RSI thresholds. Heuristic from the magnitude
                    # of (hi - lo).
                    "step": 0.001 if (hi - lo) < 0.05 else (0.01 if (hi - lo) < 0.5 else 1.0),
                }
            return {"success": True, "pair": pair, "data": schema}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    broadcaster.register_handler("research_dataset_coverage", _research_dataset_coverage)
    broadcaster.register_handler("research_lab_run", _research_lab_run)
    broadcaster.register_handler("research_params_current", _research_params_current)


def _compact(exp: Experiment) -> Dict[str, Any]:
    """Minimal experiment summary for WS payloads."""
    m = exp.result.metrics if exp.result else None
    import math as _m
    def _safe(v, digits=4):
        """Round a field, but persisted experiments can round-trip non-finite
        floats back as None — handle both here."""
        if v is None:
            return None
        try:
            return round(v, digits) if _m.isfinite(v) else None
        except TypeError:
            return None
    return {
        "id": exp.id,
        "name": exp.name,
        "status": exp.status,
        "created_at": exp.created_at,
        "triggered_by": exp.triggered_by,
        "base_preset": exp.base_preset,
        "tags": list(exp.tags),
        "metrics": ({
            "total_trades": m.total_trades,
            "total_return_pct": _safe(m.total_return_pct),
            "sharpe": _safe(m.sharpe),
            "max_drawdown_pct": _safe(m.max_drawdown_pct),
            "profit_factor": _safe(m.profit_factor),
        } if m else None),
    }


# ═══════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import tempfile
    from hydra_backtest import make_quick_config

    tmp = Path(tempfile.mkdtemp(prefix="hydra-server-smoke-"))
    print(f"[server smoke] store: {tmp}")
    store = ExperimentStore(root=tmp)

    class _MockBroadcaster:
        def __init__(self):
            self.msgs: List[Dict[str, Any]] = []
            self.handlers: Dict[str, Callable] = {}
        def broadcast_message(self, msg_type, payload):
            self.msgs.append({"type": msg_type, **payload})
        def register_handler(self, msg_type, fn):
            self.handlers[msg_type] = fn

    bc = _MockBroadcaster()
    pool = BacktestWorkerPool(max_workers=2, store=store, broadcaster=bc, error_log_dir=tmp)
    mount_backtest_routes(bc, pool)

    cfg = make_quick_config(name="smoke", n_candles=80, seed=1)
    eid = pool.submit_config(cfg, triggered_by="cli", hypothesis="smoke run")
    print(f"[server smoke] submitted: {eid}")

    # Poll for completion (pool is async)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if pool.status(eid)["status"] in ("complete", "failed", "cancelled"):
            break
        time.sleep(0.2)

    final = pool.status(eid)
    print(f"[server smoke] status: {final}")
    progress_msgs = [m for m in bc.msgs if m["type"] == "backtest_progress"]
    result_msgs = [m for m in bc.msgs if m["type"] == "backtest_result"]
    print(f"[server smoke] progress messages: {len(progress_msgs)}")
    print(f"[server smoke] result messages: {len(result_msgs)}")
    print(f"[server smoke] registered handlers: {sorted(bc.handlers.keys())}")

    pool.shutdown()
    print("[server smoke] done.")
