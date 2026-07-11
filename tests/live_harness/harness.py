"""Hydra live-execution test harness core.

Drives HydraAgent._place_order across every scenario in scenarios.py and
reports pass/fail per scenario with diagnostic output on failure.

Four run modes:
  smoke    — import + construction check only, no scenarios
  mock     — every mock-safe scenario (default)
  validate — live Kraken with --validate forced on order calls
  live     — live Kraken with real post-only orders + immediate cancel

Usage:
  python tests/live_harness/harness.py --mode smoke
  python tests/live_harness/harness.py --mode mock
  python tests/live_harness/harness.py --mode validate
  python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
  python tests/live_harness/harness.py --mode mock --scenario H3
  python tests/live_harness/harness.py --mode mock --json report.json

Exit codes:
  0 — all scenarios passed
  1 — one or more scenarios failed
  2 — harness setup error (missing deps, isolation failure, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hydra_agent import HydraAgent  # noqa: E402
from hydra_kraken_cli import KrakenCLI
from hydra_streams import FakeExecutionStream, FakeTickerStream
from hydra_engine import HydraEngine  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# Scenario dataclass
# ─────────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    """A single harness test.

    code:   short tag like 'H3' or 'F1'
    name:   human-readable name
    category: H / F / E / S / R / H_prime / L
    modes:  set of run modes this scenario belongs to
    fn:     callable that takes a Harness instance and runs the test;
            raises on failure, returns quietly on success
    """
    code: str
    name: str
    category: str
    modes: frozenset[str]
    fn: Callable[["Harness"], None]


@dataclass
class ScenarioResult:
    scenario: Scenario
    passed: bool
    duration_s: float
    error: Optional[str] = None
    traceback: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────

class Harness:
    """Owns the agent lifecycle, isolation guarantees, and scenario runner."""

    VALID_MODES = frozenset({"smoke", "mock", "validate", "live"})

    def __init__(self, mode: str, live_confirmed: bool = False, verbose: bool = False):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}; must be one of {sorted(self.VALID_MODES)}")
        if mode == "live" and not live_confirmed:
            raise RuntimeError(
                "Live mode requires --i-understand-this-places-real-orders flag. "
                "Refusing to place real Kraken orders without explicit confirmation."
            )
        self.mode = mode
        self.verbose = verbose
        self._pre_harness_env: dict[str, str] = {}

    # ───────── Isolation ─────────

    def isolate_environment(self) -> None:
        """Unset LLM API keys so HydraBrain is never constructed, and temporarily
        move aside any real on-disk state files so HydraAgent construction is
        hermetic (does not pick up the operator's live trade log / snapshot).

        In mock mode, also monkey-patches time.sleep to a no-op so the harness
        runs in seconds instead of ~90s (Hydra's rate-limit sleeps are only
        needed for real API calls, which mock mode never makes). validate/live
        modes keep real sleeps so Kraken rate limits are respected.
        """
        # Save then unset brain env vars
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
            if key in os.environ:
                self._pre_harness_env[key] = os.environ.pop(key)

        # Block hydra_companions.config._load_env_once from re-populating
        # the env via .env on first lazy import (which happens during
        # HydraAgent.__init__). Without this, scenario #1 sees a clean env
        # but scenarios #2+ inherit a re-populated XAI/ANTHROPIC key from
        # the operator's .env and HydraBrain construction succeeds — which
        # the per-scenario `assert agent.brain is None` then catches as a
        # failure. See Audit 2026-04-18.
        self._set_no_dotenv_prev = os.environ.get("HYDRA_NO_DOTENV")
        os.environ["HYDRA_NO_DOTENV"] = "1"

        # The harness exercises order-placement PLUMBING (journal, rollback,
        # WS event application), not entry economics. The v2.27 friction
        # expectancy gate would veto BUYs on the low-volatility mock tape
        # before they ever reach _place_order, so disable it for the run.
        self._friction_gate_prev = os.environ.get("HYDRA_FRICTION_GATE_DISABLED")
        os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"

        # Hold-through default ON rides mid-TREND_UP SELLs (except extreme
        # overbought). Harness SELL scenarios seed positions and call
        # execute_signal under TREND_UP — rails would return engine_rejected
        # and never reach _place_order. Kill rails for plumbing tests only.
        self._hold_through_prev = os.environ.get("HYDRA_HOLD_THROUGH")
        os.environ["HYDRA_HOLD_THROUGH"] = "0"

        # Stash real on-disk state files so they don't leak into the harness.
        # HydraAgent.__init__ runs the legacy journal migrator AND merges
        # the rolling order journal, either of which would otherwise pull
        # production trades into a test agent. Rename -> restore is atomic
        # and leaves no window where the file is partially valid.
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._stashed_state_files: list[tuple[str, str]] = []
        stash_names = (
            "hydra_session_snapshot.json",
            "hydra_order_journal.json",
            # Legacy shape — migrator would move it on __init__, so stash it too
            "hydra_trades_live.json",
            # Migrator audit sidecar — avoid leaking across test runs
            "hydra_trades_live.json.migrated",
        )
        for fname in stash_names:
            path = os.path.join(base_dir, fname)
            if os.path.exists(path):
                stash = path + ".harness_stash"
                if os.path.exists(stash):
                    os.remove(stash)
                os.rename(path, stash)
                self._stashed_state_files.append((path, stash))
                self._vprint(f"  [ISOLATE] stashed {fname} -> .harness_stash")

        # Fast mock mode: monkey-patch time.sleep to a no-op. Mock mode never
        # makes real Kraken calls, so rate-limit sleeps are pure wall-clock
        # waste. This drops mock mode from ~90s to ~2s — critical for CI.
        if self.mode == "mock":
            self._original_time_sleep = time.sleep
            time.sleep = lambda *_args, **_kwargs: None
            # Also patch hydra_agent's module-level time binding so anything
            # that did `from time import sleep` gets the fast version.
            import hydra_agent
            hydra_agent.time.sleep = lambda *_args, **_kwargs: None

    def restore_environment(self) -> None:
        """Restore env vars, time.sleep, and any stashed state files on exit."""
        for key, val in self._pre_harness_env.items():
            os.environ[key] = val
        self._pre_harness_env.clear()
        # Restore HYDRA_NO_DOTENV to whatever it was before isolation.
        prev = getattr(self, "_set_no_dotenv_prev", None)
        if prev is None:
            os.environ.pop("HYDRA_NO_DOTENV", None)
        else:
            os.environ["HYDRA_NO_DOTENV"] = prev
        # Restore the friction-gate kill switch to its pre-isolation value.
        prev = getattr(self, "_friction_gate_prev", None)
        if prev is None:
            os.environ.pop("HYDRA_FRICTION_GATE_DISABLED", None)
        else:
            os.environ["HYDRA_FRICTION_GATE_DISABLED"] = prev
        # Restore hold-through env to its pre-isolation value.
        prev = getattr(self, "_hold_through_prev", None)
        if prev is None:
            os.environ.pop("HYDRA_HOLD_THROUGH", None)
        else:
            os.environ["HYDRA_HOLD_THROUGH"] = prev
        # Restore real time.sleep if we patched it
        if hasattr(self, "_original_time_sleep"):
            time.sleep = self._original_time_sleep
            import hydra_agent
            hydra_agent.time.sleep = self._original_time_sleep
            del self._original_time_sleep
        # Restore stashed state files; remove anything the harness wrote in
        # their place so the operator's real files come back clean.
        for path, stash in getattr(self, "_stashed_state_files", []):
            try:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(stash):
                    os.rename(stash, path)
            except OSError as e:
                print(f"  [ISOLATE] WARNING: failed to restore {os.path.basename(path)}: {e}",
                      file=sys.stderr)
        self._stashed_state_files = []

    def isolate_tuner(self, agent: HydraAgent) -> None:
        """Monkey-patch each ParameterTracker._save method on the agent to a no-op.

        This prevents the harness from writing to hydra_params_*.json files.
        Safe because the harness does not care about save persistence."""
        for pair, tracker in agent.trackers.items():
            tracker._save = lambda: None
            self._vprint(f"  [ISOLATE] tuner._save patched for {pair}")

    def isolate_broadcaster(self, agent: HydraAgent) -> None:
        """Ensure the dashboard broadcaster never starts. HydraAgent.__init__
        does not call .start() so this is already true, but we defensively
        patch .start() to a no-op in case of future changes."""
        if hasattr(agent, "broadcaster") and agent.broadcaster is not None:
            agent.broadcaster.start = lambda: None
            self._vprint("  [ISOLATE] broadcaster.start patched to no-op")

    # ───────── Agent factory ─────────

    def new_agent(self, pairs: list[str] = None, paper: bool = False,
                   initial_balance: float = 200.0) -> HydraAgent:
        """Create a clean HydraAgent for a scenario with all isolation guarantees
        applied.

        Default pairs is ['SOL/USDC'] — a single pair minimizes setup time and
        most scenarios only need one.

        The live ExecutionStream is swapped out for FakeExecutionStream so no
        `kraken ws executions` subprocess is spawned during tests. Scenarios
        can still exercise lifecycle-event application by calling
        `agent.execution_stream.inject_event(...)` followed by
        `agent._apply_execution_event(e)` on each drained event, or by
        calling the agent's tick-loop drain pathway directly.
        """
        if pairs is None:
            pairs = ["SOL/USDC"]
        agent = HydraAgent(
            pairs=pairs,
            initial_balance=initial_balance,
            interval_seconds=60,
            duration_seconds=0,
            ws_port=0,  # won't be bound since broadcaster.start() is never called
            mode="competition",
            paper=paper,
            candle_interval=15,
            reset_params=True,  # don't load real tuned params
            resume=False,
        )
        self.isolate_tuner(agent)
        self.isolate_broadcaster(agent)
        # Swap in the fake stream — guaranteed no subprocess, instant events.
        agent.execution_stream = FakeExecutionStream()
        # Swap in fake ticker stream — no subprocess, deterministic data.
        agent.ticker_stream = FakeTickerStream(pairs=agent.pairs)
        agent.ticker_stream.start()
        # Inject default bid/ask for all pairs
        for pair in agent.pairs:
            price = 100.0 if "USDC" in pair else 0.0012
            agent.ticker_stream.inject(pair, {"bid": price, "ask": price * 1.001, "last": price})
        assert agent.brain is None, "Brain should be None (env vars unset during isolation)"
        return agent

    def seed_candles(self, agent: HydraAgent, pair: str, base_price: float = 100.0,
                     n: int = 60) -> None:
        """Feed n synthetic uptrend candles into a pair's engine so it has
        enough history for signals and so `engine.prices` is non-empty (required
        by _maybe_execute at hydra_engine.py:914)."""
        engine = agent.engines[pair]
        for i in range(n):
            price = base_price * (1 + 0.001 * i)  # gentle uptrend
            engine.ingest_candle({
                "timestamp": float(1700000000 + i * 300),
                "open": price * 0.999,
                "high": price * 1.001,
                "low": price * 0.998,
                "close": price,
                "volume": 100.0,
            })

    # ───────── Scenario runner ─────────

    def run_scenarios(self, scenarios: list[Scenario],
                       scenario_filter: Optional[str] = None) -> list[ScenarioResult]:
        """Run all scenarios that match the current mode (and optional filter)."""
        results: list[ScenarioResult] = []
        filtered = [
            s for s in scenarios
            if self.mode in s.modes and (scenario_filter is None or s.code == scenario_filter)
        ]
        if not filtered:
            print(f"  [HARNESS] No scenarios match mode={self.mode!r} filter={scenario_filter!r}")
            return results

        print(f"\n  Running {len(filtered)} scenario(s) in mode={self.mode!r}")
        print(f"  {'=' * 70}")
        for scenario in filtered:
            result = self._run_one(scenario)
            results.append(result)
            icon = "PASS" if result.passed else "FAIL"
            print(f"  [{icon}] {scenario.code} {scenario.name}  ({result.duration_s:.2f}s)")
            if not result.passed:
                for line in (result.error or "").splitlines():
                    print(f"         {line}")
        return results

    def _run_one(self, scenario: Scenario) -> ScenarioResult:
        start = time.time()
        try:
            scenario.fn(self)
            return ScenarioResult(scenario=scenario, passed=True, duration_s=time.time() - start)
        except AssertionError as e:
            return ScenarioResult(
                scenario=scenario, passed=False, duration_s=time.time() - start,
                error=str(e), traceback=traceback.format_exc(),
            )
        except Exception as e:
            return ScenarioResult(
                scenario=scenario, passed=False, duration_s=time.time() - start,
                error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc(),
            )

    # ───────── Reporting ─────────

    @staticmethod
    def summarize(results: list[ScenarioResult]) -> tuple[int, int]:
        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        return passed, failed

    @staticmethod
    def print_summary(results: list[ScenarioResult]) -> None:
        passed, failed = Harness.summarize(results)
        total = passed + failed
        print(f"\n  {'=' * 70}")
        print(f"  Live Harness: {passed}/{total} passed, {failed} failed")
        print(f"  {'=' * 70}")
        if failed:
            print(f"\n  Failures:")
            for r in results:
                if not r.passed:
                    print(f"    [{r.scenario.code}] {r.scenario.name}")
                    for line in (r.error or "").splitlines()[:3]:
                        print(f"        {line}")

    @staticmethod
    def json_report(results: list[ScenarioResult]) -> dict:
        return {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "scenarios": [
                {
                    "code": r.scenario.code,
                    "name": r.scenario.name,
                    "category": r.scenario.category,
                    "passed": r.passed,
                    "duration_s": round(r.duration_s, 4),
                    "error": r.error,
                }
                for r in results
            ],
        }

    def _vprint(self, msg: str) -> None:
        if self.verbose:
            print(msg)


# ─────────────────────────────────────────────────────────────────
# Helper: execute wrapper that mirrors the tick-loop at hydra_agent.py:876-909
# ─────────────────────────────────────────────────────────────────

def harness_execute(agent: HydraAgent, pair: str, action: str,
                    confidence: float, reason: str = "harness test",
                    state_overrides: Optional[dict] = None) -> dict:
    """Reproduces the tick loop's place-order wrapper. Returns a report
    dict suitable for post-scenario assertions.

    Flow (mirrors hydra_agent.py tick body):
      1. Snapshot engine state
      2. Call engine.execute_signal -> mutates engine, returns Trade object
      3. If trade, build a minimal `state` dict (with _pre_trade_snapshot),
         call agent._place_order
      4. If _place_order returns False, restore engine state from snapshot

    Returns a dict with:
      outcome: 'success' | 'failed_and_rolled_back' | 'engine_rejected'
      pre_snap: the snapshot dict (for rollback verification)
      trade: the Trade object or None
      trade_dict: the dict passed to _place_order, or None
      last_journal_entry: agent.order_journal[-1] or None
      journal_count_before/after: used to detect multiple appends

    state_overrides lets a scenario pre-seed decision context (strategy,
    regime, cross_pair_override, etc.) that the journal entry should
    capture. Defaults to MOMENTUM / TREND_UP to match historical defaults.
    """
    engine = agent.engines[pair]
    count_before = len(agent.order_journal)
    pre_snap = engine.snapshot_position()

    trade = engine.execute_signal(
        action=action, confidence=confidence, reason=reason, strategy="MOMENTUM",
    )
    if trade is None:
        return {
            "outcome": "engine_rejected",
            "pre_snap": pre_snap,
            "trade": None,
            "trade_dict": None,
            "last_journal_entry": None,
            "journal_count_before": count_before,
            "journal_count_after": len(agent.order_journal),
        }

    trade_dict = {
        "action": trade.action,
        "amount": trade.amount,
        "price": trade.price,
        "reason": trade.reason,
        "confidence": trade.confidence,
        "params_at_entry": getattr(trade, "params_at_entry", None),
    }
    state = {
        "strategy": "MOMENTUM",
        "regime": "TREND_UP",
        "_pre_trade_snapshot": pre_snap,
    }
    if state_overrides:
        state.update(state_overrides)
    success = agent._place_order(pair, trade_dict, state)
    if not success:
        engine.restore_position(pre_snap)

    # Mirror the tick-loop: drain any queued execution events and apply
    # them to the journal + engine. In paper mode, _place_paper_order
    # synthesizes an immediate FILLED event which this drain promotes from
    # PLACED to FILLED. In live-mocked mode no WS events arrive, so the
    # entry stays PLACED until a scenario manually injects one.
    drained_events = agent.execution_stream.drain_events()
    for ev in drained_events:
        agent._apply_execution_event(ev)

    return {
        "outcome": "success" if success else "failed_and_rolled_back",
        "pre_snap": pre_snap,
        "trade": trade,
        "trade_dict": trade_dict,
        "last_journal_entry": agent.order_journal[-1] if agent.order_journal else None,
        "journal_count_before": count_before,
        "journal_count_after": len(agent.order_journal),
        "drained_events": drained_events,
    }


# ─────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hydra live-execution test harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=sorted(Harness.VALID_MODES), default="mock",
                        help="Run mode (default: mock)")
    parser.add_argument("--scenario", default=None,
                        help="Run only the scenario with this code (e.g. H3)")
    parser.add_argument("--i-understand-this-places-real-orders", action="store_true",
                        dest="live_confirmed",
                        help="Required to run --mode live; acknowledges real-order risk")
    parser.add_argument("--json", default=None, metavar="FILE",
                        help="Write machine-readable JSON report to FILE")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    try:
        harness = Harness(mode=args.mode, live_confirmed=args.live_confirmed, verbose=args.verbose)
    except RuntimeError as e:
        print(f"  [ERROR] {e}", file=sys.stderr)
        return 2

    harness.isolate_environment()

    # Smoke mode: just import + construct an agent
    if args.mode == "smoke":
        try:
            agent = harness.new_agent(pairs=["SOL/USDC"])
            assert agent.brain is None
            assert agent.broadcaster is not None
            assert "SOL/USDC" in agent.engines
            assert len(agent.order_journal) == 0
            assert hasattr(agent, "execution_stream")
            assert agent.execution_stream.healthy
            print("  [SMOKE] Agent constructed, brain=None, engines ready, order_journal empty, stream healthy")
            print("  [SMOKE] OK")
            harness.restore_environment()
            return 0
        except Exception as e:
            print(f"  [SMOKE] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            harness.restore_environment()
            return 1

    # Import scenarios lazily so smoke mode doesn't pay the cost
    from tests.live_harness import scenarios as scenarios_module
    results = harness.run_scenarios(scenarios_module.ALL_SCENARIOS,
                                      scenario_filter=args.scenario)
    harness.restore_environment()

    Harness.print_summary(results)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(Harness.json_report(results), f, indent=2, default=str)
        print(f"  JSON report written to {args.json}")

    _, failed = Harness.summarize(results)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
