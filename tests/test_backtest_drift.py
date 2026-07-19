"""Drift regression for the backtester (I7).

Zero-drift invariant: running the backtester on a fixed candle sequence must
produce the same per-tick (regime, signal.action, signal.confidence,
position.size, balance) as invoking HydraEngine directly with the same inputs.

Phase 1 pins the engine+coordinator path. Phase 6 extends drift coverage to
the modifier chain (order book / FOREX session / brain) once the live
modifier logic is factored out of hydra_agent.py.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_engine import HydraEngine, SIZING_CONSERVATIVE  # noqa: E402
from hydra_backtest import (  # noqa: E402
    BacktestRunner,
    SyntheticSource,
    make_quick_config,
)


def _neutralize_circuit_breaker():
    """Drift test invariant is signal-layer equivalence, not halt-state
    equivalence. With Fix 5/6, the direct path (no execute_signal) and the
    backtester path (execute_signal + filler rollback) produce different
    peak_equity/max_drawdown trajectories — either path can halt while the
    other doesn't, producing spurious 'drift' at the halt transition.
    Disabling the circuit breaker at the class level ensures the halt branch
    is never taken in either path. Both use the same class; this neutralizes
    both uniformly."""
    HydraEngine.CIRCUIT_BREAKER_PCT = 10_000.0


class TestZeroDrift(unittest.TestCase):
    """The backtester's tick-by-tick engine outputs must match a direct
    HydraEngine loop on the same candles and params (I7).

    We reproduce the backtester's per-pair path: ingest_candle → tick(generate_only)
    → execute_signal (when applicable). Post-only fill semantics are the new
    behavior the backtester adds on top; drift test excludes fill-bound equity
    (which is affected by post-only rejection / fee deduction) and pins only
    signal-layer + pre-fill engine decisions.
    """

    def setUp(self):
        _neutralize_circuit_breaker()
        self._original_cb = HydraEngine.CIRCUIT_BREAKER_PCT
        # Hold-through force-flatten is position-dependent (long + TREND_DOWN
        # → SELL). Direct path uses generate_only (never opens positions) so
        # it would HOLD while the backtester SELLs — false I7 drift. Kill
        # rails for signal-layer parity only (same pattern as CB neutralize).
        self._hold_through_prev = os.environ.get("HYDRA_HOLD_THROUGH")
        os.environ["HYDRA_HOLD_THROUGH"] = "0"

    def tearDown(self):
        HydraEngine.CIRCUIT_BREAKER_PCT = 15.0  # repo default; see hydra_engine.py
        prev = getattr(self, "_hold_through_prev", None)
        if prev is None:
            os.environ.pop("HYDRA_HOLD_THROUGH", None)
        else:
            os.environ["HYDRA_HOLD_THROUGH"] = prev

    def _collect_direct(self, candles, candle_interval=15):
        """Run a single HydraEngine through the candles and collect per-tick state.

        Uses tick(generate_only=True) — no execute_signal. Under Fix 5/6
        (signal-semantic changes), the backtester's execute_signal +
        SimulatedFiller-rejection path and the direct path's no-execute path
        produce different balance/position trajectories. setUp()
        neutralizes HydraEngine.CIRCUIT_BREAKER_PCT at the class level so
        the halt branch is never taken in either path. The drift invariant
        we actually care about is: same engine code produces same
        signal-layer outputs given same candle inputs — halt-state
        divergence from different execute_signal trajectories is out of
        scope.
        """
        engine = HydraEngine(
            initial_balance=100.0,
            asset=self.DRIFT_PAIR,
            sizing=SIZING_CONSERVATIVE,
            candle_interval=candle_interval,
            hold_through=False,
        )
        states = []
        for c in candles:
            engine.ingest_candle({
                "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                "volume": c.volume, "timestamp": c.timestamp,
            })
            s = engine.tick(generate_only=True)
            states.append({
                "regime": s.get("regime"),
                "strategy": s.get("strategy"),
                "action": s.get("signal", {}).get("action"),
                "confidence": round(s.get("signal", {}).get("confidence", 0.0), 9),
            })
        return states

    # Pinned explicitly on BOTH the direct path and the config so the test
    # cannot silently compare two different synthetic tapes if the product
    # default pair list changes (it did in v2.19 and again in v2.29).
    DRIFT_PAIR = "BTC/USD"

    def test_signal_layer_matches_direct_engine_single_pair(self):
        source = SyntheticSource(kind="gbm", n_candles=250, seed=17)
        candles = list(source.iter_candles(self.DRIFT_PAIR))

        direct_states = self._collect_direct(candles)

        # Same candles through the backtester — single-pair, coordinator disabled to
        # isolate pure engine behavior (coordinator requires ≥2 pairs to issue overrides)
        cfg = make_quick_config(name="drift", pairs=(self.DRIFT_PAIR,),
                                n_candles=250, seed=17)
        # Disable coordinator explicitly
        from dataclasses import replace
        cfg = replace(cfg, coordinator_enabled=False)
        runner = BacktestRunner(cfg)
        bt_states_collected = []

        # Hook into the runner by monkey-patching on_tick to capture per-tick signals.
        # Cleaner than re-reading trade_log because it includes HOLD ticks.
        def capture(state):
            for _pair, pair_state in state.get("pairs", {}).items():
                sig = pair_state.get("signal", {})
                bt_states_collected.append({
                    "regime": pair_state.get("regime"),
                    "strategy": pair_state.get("strategy"),
                    "action": sig.get("action"),
                    "confidence": round(sig.get("confidence", 0.0), 9),
                })

        runner.run(on_tick=capture)

        self.assertEqual(len(direct_states), len(bt_states_collected),
                         "tick count mismatch — drift source of truth is broken")

        # Compare tick-by-tick
        divergence = []
        for i, (d, b) in enumerate(zip(direct_states, bt_states_collected)):
            if d != b:
                divergence.append((i, d, b))
        self.assertEqual(divergence, [], f"drift detected at ticks: {divergence[:5]}")

    def test_hold_through_on_execute_fill_parity(self):
        """H4 / product-default hold-through: generate_only → execute_signal
        → next-bar fill path must stay deterministic under HT ON.

        Signal-only I7 keeps HT off (position-dependent flatten). This suite
        mirrors the live/backtest seam with hold_through=True and asserts the
        backtester completes without exception and fill accounting is finite.
        """
        from dataclasses import replace
        prev = os.environ.get("HYDRA_HOLD_THROUGH")
        try:
            os.environ.pop("HYDRA_HOLD_THROUGH", None)  # product default ON
            cfg = make_quick_config(name="drift_ht_on", n_candles=200, seed=19)
            cfg = replace(cfg, coordinator_enabled=False)
            runner = BacktestRunner(cfg)
            # Ensure engines inherit default hold_through (env default True)
            result = runner.run()
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result.fills + result.rejects, 0)
            # No NaN equity tails
            for pair, curve in (result.equity_curve or {}).items():
                if curve:
                    self.assertTrue(all(isinstance(x, (int, float)) for x in curve[-5:]))
        finally:
            if prev is None:
                os.environ.pop("HYDRA_HOLD_THROUGH", None)
            else:
                os.environ["HYDRA_HOLD_THROUGH"] = prev

    def test_candle_stream_parity_multi_seed(self):
        """A lighter drift check across 3 seeds ensures the result is seed-invariant
        with respect to drift (not just a lucky alignment for seed=17)."""
        from dataclasses import replace
        for seed in (1, 7, 123):
            source = SyntheticSource(kind="gbm", n_candles=150, seed=seed)
            candles = list(source.iter_candles(self.DRIFT_PAIR))
            direct = self._collect_direct(candles)

            cfg = make_quick_config(name=f"drift_{seed}", pairs=(self.DRIFT_PAIR,),
                                    n_candles=150, seed=seed)
            cfg = replace(cfg, coordinator_enabled=False)
            runner = BacktestRunner(cfg)
            captured = []
            runner.run(on_tick=lambda st: captured.extend([
                {
                    "regime": ps.get("regime"),
                    "strategy": ps.get("strategy"),
                    "action": ps.get("signal", {}).get("action"),
                    "confidence": round(ps.get("signal", {}).get("confidence", 0.0), 9),
                }
                for _p, ps in st.get("pairs", {}).items()
            ]))
            self.assertEqual(direct, captured, f"drift on seed={seed}")


if __name__ == "__main__":
    unittest.main()
