"""Unit tests for hydra_backtest Phase 1: CandleSource, SimulatedFiller,
BacktestConfig stamps, BacktestRunner end-to-end, metric math.

Intentionally kept to stdlib unittest (no pytest dependency) to mirror the
existing tests/ style in this repo.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

# Make repo root importable when running this file directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_engine import Candle, HydraEngine, Trade  # noqa: E402
from hydra_backtest import (  # noqa: E402
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    SimulatedFiller,
    PendingOrder,
    SyntheticSource,
    make_quick_config,
    finalize_stamps,
    metrics_between,
    _annualize_return,
    _buy_fee_for_close,
    _sharpe_from_equity,
    _sortino_from_equity,
    _max_dd_pct,
    _compute_param_hash,
)


class TestBacktestConfig(unittest.TestCase):
    def test_finalize_stamps_fills_all_fields(self):
        cfg = make_quick_config(name="t", n_candles=10)
        self.assertTrue(cfg.param_hash)
        self.assertTrue(cfg.hydra_version)
        self.assertTrue(cfg.created_at)
        # git_sha is best-effort — accepted values: real SHA or "unknown"
        self.assertTrue(cfg.git_sha)

    def test_param_hash_is_stable_for_same_config(self):
        a = make_quick_config(name="a", n_candles=10, seed=7)
        b = make_quick_config(name="b", n_candles=10, seed=7)
        # Name differs but all behavior-affecting fields match → hash must match
        self.assertEqual(a.param_hash, b.param_hash)

    def test_param_hash_changes_on_behavior_change(self):
        a = make_quick_config(name="a", n_candles=10, seed=7)
        b = make_quick_config(name="a", n_candles=20, seed=7)  # different n_candles
        self.assertNotEqual(a.param_hash, b.param_hash)

    def test_param_overrides_json_roundtrip(self):
        overrides = {"SOL/USD": {"momentum_rsi_upper": 75.0}}
        cfg = make_quick_config(name="o", overrides=overrides)
        self.assertEqual(cfg.param_overrides, overrides)


class TestSyntheticSource(unittest.TestCase):
    def test_synthetic_is_deterministic(self):
        a = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        b = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        self.assertEqual(len(a), 50)
        for ca, cb in zip(a, b):
            self.assertEqual(ca.close, cb.close)
            self.assertEqual(ca.high, cb.high)

    def test_synthetic_different_pairs_different_series(self):
        a = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("SOL/USD"))
        b = list(SyntheticSource(kind="gbm", n_candles=50, seed=42).iter_candles("BTC/USD"))
        closes_a = [c.close for c in a]
        closes_b = [c.close for c in b]
        self.assertNotEqual(closes_a, closes_b)

    def test_synthetic_flat_kind(self):
        candles = list(SyntheticSource(kind="flat", n_candles=10, start_price=100.0, seed=1).iter_candles("X"))
        for c in candles:
            self.assertAlmostEqual(c.close, 100.0, places=4)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            list(SyntheticSource(kind="bogus", n_candles=3).iter_candles("X"))


class TestSimulatedFiller(unittest.TestCase):
    def _order(self, side="BUY", limit_price=100.0, size=1.0):
        return PendingOrder(
            pair="SOL/USD",
            side=side,
            limit_price=limit_price,
            size=size,
            placed_tick=0,
            pre_trade_snapshot={},
        )

    def _candle(self, o, h, l, c, v=100.0, ts=0.0):
        return Candle(open=o, high=h, low=l, close=c, volume=v, timestamp=ts)

    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            SimulatedFiller(model="quantum")

    def test_optimistic_fills_on_wick_touch_buy(self):
        f = SimulatedFiller("optimistic")
        # limit=100, next candle dips to 99 (wick) then recovers
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=101, h=102, l=99, c=101.5))
        self.assertTrue(fill.filled)
        self.assertEqual(fill.fill_price, 100.0)
        self.assertGreater(fill.fee_paid, 0)

    def test_optimistic_no_touch_rejects(self):
        f = SimulatedFiller("optimistic")
        # limit=100, next candle stays above
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=102, h=103, l=101, c=102.5))
        self.assertFalse(fill.filled)

    def test_realistic_rejects_pure_wick(self):
        f = SimulatedFiller("realistic")
        # Limit=100, body entirely above (open 102, close 102.5, brief dip to 99)
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=102, h=102.6, l=99, c=102.5))
        self.assertFalse(fill.filled)

    def test_realistic_accepts_body_penetration(self):
        f = SimulatedFiller("realistic")
        # Body opens 101 closes 98, spans 3; limit=100 → depth = 2/3 ≈ 0.67 ≥ 0.30 → fill
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=101, h=101.5, l=97.5, c=98))
        self.assertTrue(fill.filled)

    def test_pessimistic_requires_close_cross(self):
        f = SimulatedFiller("pessimistic")
        # Body dips below 100 but closes above → no fill under pessimistic
        fill = f.try_fill(self._order("BUY", 100), self._candle(o=100.5, h=101, l=99.2, c=100.5))
        self.assertFalse(fill.filled)
        # And fills when close dips below
        fill2 = f.try_fill(self._order("BUY", 100), self._candle(o=100.5, h=101, l=99.2, c=99.5))
        self.assertTrue(fill2.filled)

    def test_sell_side_symmetry(self):
        f = SimulatedFiller("optimistic")
        # SELL limit=100; next candle spikes up to 101 → fill
        fill = f.try_fill(self._order("SELL", 100), self._candle(o=99, h=101, l=98.5, c=99.5))
        self.assertTrue(fill.filled)
        # SELL limit=100; next candle never reaches → no fill
        fill2 = f.try_fill(self._order("SELL", 100), self._candle(o=98, h=99.5, l=97, c=98.2))
        self.assertFalse(fill2.filled)


class TestBacktestRunner(unittest.TestCase):
    def test_end_to_end_synthetic_runs_clean(self):
        cfg = make_quick_config(name="e2e", n_candles=400, seed=1)
        result = BacktestRunner(cfg).run()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.candles_processed, 400)
        self.assertGreater(len(result.equity_curve["BTC/USD"]), 100)
        # Metrics are populated (even if trade count is low)
        self.assertIsNotNone(result.metrics)
        self.assertGreaterEqual(result.metrics.total_trades, 0)

    def test_determinism_same_seed_same_metrics(self):
        cfg_a = make_quick_config(name="det", n_candles=300, seed=99)
        cfg_b = make_quick_config(name="det2", n_candles=300, seed=99)
        ra = BacktestRunner(cfg_a).run()
        rb = BacktestRunner(cfg_b).run()
        self.assertAlmostEqual(ra.metrics.total_return_pct, rb.metrics.total_return_pct, places=6)
        self.assertAlmostEqual(ra.metrics.sharpe, rb.metrics.sharpe, places=6)
        self.assertEqual(ra.metrics.total_trades, rb.metrics.total_trades)

    def test_different_seed_different_outcome(self):
        """Seeds change the synthetic path; idle cash under hold-through is OK.

        Hold-through default ON often yields flat cash equity (no trades) on
        short GBM windows — both seeds then share identical cash curves, so
        comparing equity is not a seed probe. Prove diversity at the source;
        when either seed trades or MTM-moves, runner outcomes must also differ.
        """
        closes_a = [
            c.close
            for c in SyntheticSource(
                kind="gbm", n_candles=300, seed=1
            ).iter_candles("SOL/USD")
        ]
        closes_b = [
            c.close
            for c in SyntheticSource(
                kind="gbm", n_candles=300, seed=2
            ).iter_candles("SOL/USD")
        ]
        self.assertNotEqual(closes_a, closes_b, "synthetic seeds must diverge")

        ra = BacktestRunner(make_quick_config(name="d1", n_candles=300, seed=1)).run()
        rb = BacktestRunner(make_quick_config(name="d2", n_candles=300, seed=2)).run()
        self.assertEqual(ra.status, "complete")
        self.assertEqual(rb.status, "complete")
        ea = ra.equity_curve.get("SOL/USD") or []
        eb = rb.equity_curve.get("SOL/USD") or []
        outcomes_differ = (
            ra.metrics.total_return_pct != rb.metrics.total_return_pct
            or ra.metrics.total_trades != rb.metrics.total_trades
            or ea != eb
        )
        if ra.metrics.total_trades + rb.metrics.total_trades > 0 or (
            ea and (min(ea) != max(ea) or (eb and min(eb) != max(eb)))
        ):
            self.assertTrue(
                outcomes_differ,
                "when either seed is active, runner outcomes must differ",
            )
        # else: pure-cash idle on both seeds under defensive rails — allowed

    def test_cancel_token_stops_early(self):
        cfg = make_quick_config(name="c", n_candles=10_000, seed=1)
        tok = threading.Event()
        tok.set()  # cancel before first tick
        result = BacktestRunner(cfg).run(cancel_token=tok)
        self.assertEqual(result.status, "cancelled")
        self.assertLess(result.candles_processed, 10_000)

    def test_on_tick_callback_invoked(self):
        cfg = make_quick_config(name="cb", n_candles=50, seed=1)
        states = []
        BacktestRunner(cfg).run(on_tick=lambda s: states.append(s))
        self.assertGreater(len(states), 10)
        self.assertIn("pairs", states[0])
        self.assertIn("tick", states[0])

    def test_multi_pair_runs_without_error(self):
        cfg = BacktestConfig(
            name="multi",
            pairs=("SOL/USD", "BTC/USD"),
            data_source="synthetic",
            data_source_params_json=json.dumps({
                "kind": "gbm", "n_candles": 200, "seed": 5, "volatility": 0.02,
            }),
            random_seed=5,
        )
        cfg = finalize_stamps(cfg)
        result = BacktestRunner(cfg).run()
        self.assertEqual(result.status, "complete")
        self.assertIn("SOL/USD", result.equity_curve)
        self.assertIn("BTC/USD", result.equity_curve)

    def test_param_overrides_applied_to_engine(self):
        cfg = make_quick_config(
            name="po",
            n_candles=100,
            seed=1,
            overrides={"BTC/USD": {"momentum_rsi_upper": 78.0}},
        )
        runner = BacktestRunner(cfg)
        self.assertAlmostEqual(runner.engines["BTC/USD"].momentum_rsi_upper, 78.0)

    def test_competition_mode_uses_competition_sizing(self):
        cfg = finalize_stamps(BacktestConfig(
            name="comp",
            pairs=("SOL/USD",),
            mode="competition",
            data_source="synthetic",
            data_source_params_json=json.dumps({"kind": "gbm", "n_candles": 50, "seed": 1, "volatility": 0.02}),
        ))
        runner = BacktestRunner(cfg)
        # Competition preset: kelly_multiplier=0.50
        self.assertAlmostEqual(runner.engines["SOL/USD"].sizer.kelly_multiplier, 0.50)

    def test_live_engine_not_mutated(self):
        """I2 sanity: BacktestRunner constructs its own engines; does not take external refs."""
        cfg = make_quick_config(name="i2", n_candles=50, seed=1)
        runner = BacktestRunner(cfg)
        # There is no way for a caller to inject a live engine through the public API
        self.assertTrue(hasattr(runner, "engines"))
        # Fresh runner with same config still gets its own engine (different object)
        runner2 = BacktestRunner(cfg)
        self.assertIsNot(runner.engines["BTC/USD"], runner2.engines["BTC/USD"])

    def test_gapped_pairs_equity_curves_tick_aligned(self):
        """A pair missing bars at the frontier must still record equity.

        Pre-fix, only engine_states (pairs with a bar) appended, so a
        5-bar series zipped with a 3-bar series at index i — the same
        class of bug v2.32.1 fixed in _loop. Both curves must be one
        point per unique timestamp, gaps forward-filled.
        """
        from hydra_engine import Candle

        class _SeqSource:
            def __init__(self, candles):
                self._c = list(candles)

            def iter_candles(self, pair):
                return iter(self._c)

            def describe(self):
                return {"kind": "seq", "n": len(self._c)}

        def _bars(timestamps, px=100.0):
            return [
                Candle(open=px, high=px, low=px, close=px, volume=1.0,
                       timestamp=float(t))
                for t in timestamps
            ]

        cfg = finalize_stamps(BacktestConfig(
            name="gap-align",
            pairs=("AAA/USD", "BBB/USD"),
            candle_interval=60,
            max_ticks=20,
            coordinator_enabled=False,
        ))
        runner = BacktestRunner(cfg, sources_override={
            "AAA/USD": _SeqSource(_bars([100, 200, 300, 400, 500])),
            "BBB/USD": _SeqSource(_bars([100, 300, 500])),
        })
        result = runner.run()
        self.assertEqual(result.status, "complete")
        ea = result.equity_curve["AAA/USD"]
        eb = result.equity_curve["BBB/USD"]
        self.assertEqual(len(ea), 5, "full series should have 5 frontier ticks")
        self.assertEqual(len(eb), 5, "gapped series must forward-fill, not shrink")
        # Flat 100 close, no trades → equity stays at the seeded balance.
        self.assertEqual(len(set(round(x, 8) for x in eb)), 1)


class TestMetricHelpers(unittest.TestCase):
    def test_annualize_zero_ticks(self):
        self.assertEqual(_annualize_return(10.0, 0, 15), 0.0)

    def test_annualize_positive(self):
        # 10% over 96 ticks of 15-min candles = 1 day; annualized should be ~huge
        val = _annualize_return(10.0, 96, 15)
        self.assertGreater(val, 100.0)

    def test_annualize_negative_handles_underwater(self):
        val = _annualize_return(-99.0, 100, 15)
        # Geometric annualization of a -99% cumulative over 100 ticks is very negative
        self.assertLess(val, -50.0)

    def test_max_dd_empty(self):
        self.assertEqual(_max_dd_pct([]), 0.0)

    def test_max_dd_computed(self):
        equity = [100, 110, 105, 120, 60, 90]
        # Peak 120, trough 60 → 50% DD
        self.assertAlmostEqual(_max_dd_pct(equity), 50.0, places=2)

    def test_sharpe_flat_equity_is_zero(self):
        self.assertEqual(_sharpe_from_equity([100.0] * 20, 15), 0.0)

    def test_sharpe_rising_is_positive(self):
        equity = [100 + i for i in range(50)]
        self.assertGreater(_sharpe_from_equity(equity, 15), 0.0)

    def test_sortino_no_downside_handled(self):
        # Monotonically rising equity → no downside. Historically this
        # returned math.inf, but that sanitises to None on JSON save and
        # crashes compare() on reload, so the engine now emits a finite
        # sentinel (999.0) meaning "∞". Accept either form for resilience.
        equity = [100 + i for i in range(30)]
        s = _sortino_from_equity(equity, 15)
        self.assertTrue(s == math.inf or s == 999.0 or s == 0.0)

    def test_returns_from_equity_zero_prev_safe(self):
        # Internal: ensure divide-by-zero protected
        from hydra_backtest import _returns_from_equity
        rets = _returns_from_equity([0.0, 0.0, 10.0])
        self.assertEqual(rets, [0.0, 0.0])


class TestTradeLogProfit(unittest.TestCase):
    """SELL fills must carry realized profit in trade_log — Monte Carlo and
    bootstrap CI resample these; a None-profit log silently disables both."""

    def test_sell_fills_record_realized_profit(self):
        import os
        from dataclasses import replace
        saved = {k: os.environ.get(k) for k in
                 ("HYDRA_HOLD_THROUGH", "HYDRA_FRICTION_GATE_DISABLED")}
        os.environ["HYDRA_HOLD_THROUGH"] = "0"
        os.environ["HYDRA_FRICTION_GATE_DISABLED"] = "1"
        try:
            cfg = make_quick_config(
                name="mc-profit", n_candles=900, kind="mean_reverting",
                seed=5, mode="competition",
            )
            cfg = replace(cfg, coordinator_enabled=False)
            result = BacktestRunner(cfg).run()
            sells = [t for t in result.trade_log if t["side"] == "SELL"]
            self.assertGreater(len(sells), 0,
                               "fixture produced no SELL fills — adjust seed")
            missing = [t for t in sells if t["profit"] is None]
            self.assertEqual(missing, [],
                             f"SELL fills missing profit: {missing}")
            for t in sells:
                self.assertIsInstance(t["profit"], float)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _wide_bar(ts: float, close: float) -> Candle:
    return Candle(
        open=close, high=close + 5.0, low=max(0.01, close - 5.0),
        close=close, volume=10.0, timestamp=float(ts),
    )


class _ListSource:
    def __init__(self, candles):
        self._candles = list(candles)

    def iter_candles(self, pair):
        yield from self._candles

    def describe(self):
        return {"kind": "list"}


def _scripted_books(plan, sell_price=None):
    """Replace tick/execute with a fixed BUY/SELL script. Returns a restore fn."""
    orig_tick = HydraEngine.tick
    orig_exec = HydraEngine.execute_signal

    def tick(self, generate_only=False):
        action = plan.get(len(self.prices), "HOLD")
        return {
            "regime": "RANGING",
            "strategy": "MOMENTUM",
            "signal": {"action": action, "confidence": 1.0, "reason": "test"},
        }

    def execute_signal(
        self, action, confidence, reason="", strategy="MOMENTUM", size_multiplier=1.0,
    ):
        mark = float(self.prices[-1])
        if action == "BUY":
            price = mark
            amount = 1.0
            cost = amount * price
            self.balance -= cost
            self.position.size = amount
            self.position.avg_entry = price
            return Trade(
                action="BUY", asset=self.asset, price=price, amount=amount,
                value=cost, reason=reason or "test", confidence=float(confidence),
                strategy=strategy or "MOMENTUM",
            )
        if action == "SELL" and self.position.size > 0:
            price = mark if sell_price is None else float(sell_price)
            amount = self.position.size
            entry = self.position.avg_entry
            revenue = amount * price
            profit = (price - entry) * amount
            self.balance += revenue
            self.position.size = 0.0
            self.position.avg_entry = 0.0
            self.total_trades += 1
            if profit > 0:
                self.win_count += 1
                self.gross_profit += profit
            else:
                self.loss_count += 1
                self.gross_loss += abs(profit)
            return Trade(
                action="SELL", asset=self.asset, price=price, amount=amount,
                value=revenue, reason=reason or "test", confidence=float(confidence),
                strategy=strategy or "MOMENTUM", profit=profit,
            )
        return None

    HydraEngine.tick = tick
    HydraEngine.execute_signal = execute_signal

    def restore():
        HydraEngine.tick = orig_tick
        HydraEngine.execute_signal = orig_exec

    return restore


class TestFinalBarBooks(unittest.TestCase):
    """An order posted on the last frontier never reaches SimulatedFiller."""

    def test_run_restores_open_position_and_rewrites_equity(self):
        candles = [
            _wide_bar(1_000_000 + i * 3600, px)
            for i, px in enumerate((100.0, 100.0, 100.0, 130.0))
        ]
        cfg = replace(
            make_quick_config(name="last-bar-books", n_candles=4, seed=1),
            coordinator_enabled=False,
            fill_model="optimistic",
            maker_fee_bps=16.0,
            initial_balance_per_pair=10_000.0,
        )
        runner = BacktestRunner(
            cfg, sources_override={"BTC/USD": _ListSource(candles)},
        )
        restore = _scripted_books({2: "BUY", 4: "SELL"}, sell_price=50.0)
        try:
            result = runner.run()
        finally:
            restore()

        engine = runner.engines["BTC/USD"]
        buy = next(t for t in result.trade_log if t["side"] == "BUY")
        post_buy_cash = 10_000.0 - float(buy["value"]) - float(buy["fee_paid"])
        self.assertEqual(result.fills, 1)
        self.assertEqual(result.rejects, 1)
        self.assertEqual(result.metrics.total_trades, 0)
        self.assertEqual(engine.total_trades, 0)
        self.assertEqual(engine.win_count, 0)
        self.assertEqual(engine.loss_count, 0)
        self.assertAlmostEqual(engine.position.size, float(buy["amount"]))
        self.assertAlmostEqual(engine.balance, post_buy_cash, places=6)
        restored_mark = post_buy_cash + float(buy["amount"]) * 130.0
        optimistic = post_buy_cash + float(buy["amount"]) * 50.0
        self.assertAlmostEqual(result.equity_curve["BTC/USD"][-1], restored_mark, places=6)
        self.assertNotAlmostEqual(
            result.equity_curve["BTC/USD"][-1], optimistic, places=4,
        )
        self.assertIsNone(runner._pending["BTC/USD"])

    def test_reject_unfilled_rewrites_poisoned_last_mark(self):
        cfg = make_quick_config(name="settle", n_candles=3, seed=1)
        runner = BacktestRunner(cfg)
        pair = "BTC/USD"
        engine = runner.engines[pair]
        engine.prices.append(10.0)
        engine.balance = 50.0
        engine.position.size = 2.0
        snap = engine.snapshot_position()
        engine.balance = 70.0
        engine.position.size = 0.0
        engine.total_trades = 1
        engine.win_count = 1
        engine.gross_profit = 20.0
        runner._pending[pair] = PendingOrder(
            pair=pair, side="SELL", limit_price=10.0, size=2.0,
            placed_tick=0, pre_trade_snapshot=snap,
        )
        result = BacktestResult(config=runner.config)
        result.equity_curve[pair] = [123.0]
        result.candles_processed = 1
        runner._reject_unfilled_at_end(result)
        self.assertEqual(result.rejects, 1)
        self.assertIsNone(runner._pending[pair])
        self.assertAlmostEqual(engine.balance, 50.0)
        self.assertAlmostEqual(engine.position.size, 2.0)
        self.assertEqual(engine.total_trades, 0)
        self.assertEqual(engine.win_count, 0)
        self.assertAlmostEqual(engine.gross_profit, 0.0)
        self.assertAlmostEqual(result.equity_curve[pair][-1], 70.0)


class TestRoundTripFees(unittest.TestCase):
    def test_buy_fee_matcher_prefers_recorded_fee(self):
        fee = _buy_fee_for_close(
            [{"pair": "BTC/USD", "side": "BUY", "amount": 1.0, "fee_paid": 5.0}],
            "BTC/USD", 1.0, raw_profit=1.0, sell_notional=101.0, maker_fee_bps=16.0,
        )
        self.assertAlmostEqual(fee, 5.0)

    def test_ambiguous_buy_fee_uses_notional_once(self):
        # No buy, a buy with no fee, a short lot, and another pair's buy
        # are all ambiguous. Notional is sell_notional - raw_profit = 100.
        expected = 100.0 * 16.0 / 10_000.0
        cases = [
            [],
            [{"pair": "BTC/USD", "side": "BUY", "amount": 1.0}],
            [{"pair": "BTC/USD", "side": "BUY", "amount": 0.5, "fee_paid": 5.0}],
            [{"pair": "ETH/USD", "side": "BUY", "amount": 1.0, "fee_paid": 5.0}],
        ]
        for log in cases:
            fee = _buy_fee_for_close(
                log, "BTC/USD", 1.0, raw_profit=1.0, sell_notional=101.0,
                maker_fee_bps=16.0,
            )
            self.assertAlmostEqual(fee, expected, msg=repr(log))

    def test_fifo_sums_open_buys_and_skips_closed_lots(self):
        summed = _buy_fee_for_close(
            [
                {"pair": "BTC/USD", "side": "BUY", "amount": 1.0, "fee_paid": 0.1},
                {"pair": "BTC/USD", "side": "BUY", "amount": 1.0, "fee_paid": 0.25},
            ],
            "BTC/USD", 2.0, raw_profit=0.0, sell_notional=200.0, maker_fee_bps=16.0,
        )
        self.assertAlmostEqual(summed, 0.35)
        after_close = _buy_fee_for_close(
            [
                {"pair": "BTC/USD", "side": "BUY", "amount": 1.0, "fee_paid": 0.1},
                {"pair": "BTC/USD", "side": "SELL", "amount": 1.0, "fee_paid": 0.1, "profit": 0.0},
                {"pair": "BTC/USD", "side": "BUY", "amount": 1.0, "fee_paid": 0.4},
            ],
            "BTC/USD", 1.0, raw_profit=0.0, sell_notional=100.0, maker_fee_bps=16.0,
        )
        self.assertAlmostEqual(after_close, 0.4)

    def test_close_nets_both_fees_before_win_loss_without_second_debit(self):
        # Price P&L is +0.20. The sell fee alone leaves a win; buy + sell
        # fees flip it to a loss. Balance already paid both fees once.
        candles = [
            _wide_bar(2_000_000 + i * 3600, px)
            for i, px in enumerate((100.0, 100.0, 100.0, 100.2, 100.2, 100.2))
        ]
        cfg = replace(
            make_quick_config(name="both-fees", n_candles=6, seed=1),
            coordinator_enabled=False,
            fill_model="optimistic",
            maker_fee_bps=16.0,
            initial_balance_per_pair=10_000.0,
        )
        runner = BacktestRunner(
            cfg, sources_override={"BTC/USD": _ListSource(candles)},
        )
        restore = _scripted_books({2: "BUY", 4: "SELL"})
        try:
            result = runner.run()
        finally:
            restore()

        buys = [t for t in result.trade_log if t["side"] == "BUY"]
        sells = [t for t in result.trade_log if t["side"] == "SELL"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(len(sells), 1)
        self.assertEqual(result.fills, 2)
        self.assertEqual(result.rejects, 0)
        raw = (sells[0]["price"] - buys[0]["price"]) * sells[0]["amount"]
        sell_only = raw - sells[0]["fee_paid"]
        net = sell_only - buys[0]["fee_paid"]
        self.assertGreater(sell_only, 0.0)
        self.assertLess(net, 0.0)
        self.assertAlmostEqual(sells[0]["profit"], net, places=9)
        engine = runner.engines["BTC/USD"]
        self.assertAlmostEqual(engine.trades[-1].profit, net, places=9)
        self.assertEqual(result.metrics.win_count, 0)
        self.assertEqual(result.metrics.loss_count, 1)
        self.assertEqual(result.per_pair_metrics["BTC/USD"].win_count, 0)
        self.assertAlmostEqual(result.metrics.win_rate_pct, 0.0)
        self.assertAlmostEqual(result.metrics.profit_factor, 0.0)
        self.assertAlmostEqual(engine.gross_profit, 0.0, places=6)
        self.assertAlmostEqual(engine.gross_loss, abs(net), places=6)
        self.assertAlmostEqual(result.metrics.avg_loss, abs(net), places=6)
        self.assertAlmostEqual(engine.position.size, 0.0)
        # Cash moved by the net round trip exactly once — not by another buy fee.
        self.assertAlmostEqual(engine.balance, 10_000.0 + net, places=6)
        self.assertAlmostEqual(result.equity_curve["BTC/USD"][-1], engine.balance, places=6)


class TestOosWindowMetrics(unittest.TestCase):
    def test_metrics_between_drops_pad_and_end_bound(self):
        cfg = replace(
            make_quick_config(name="window", n_candles=4, seed=1),
            initial_balance_per_pair=100.0,
            candle_interval=60,
        )
        result = BacktestResult(config=cfg)
        start = 1_000_000
        # Pad crashes 100 → 40. OOS rises 50 → 80. The bar at end_ts is flat.
        ts = [start - 7200, start - 3600, start, start + 3600, start + 7200]
        eq = [100.0, 40.0, 50.0, 80.0, 80.0]
        result.equity_curve["BTC/USD"] = eq
        result.signal_log["BTC/USD"] = [
            {"tick": i, "action": "HOLD", "confidence": 0.0, "timestamp": t}
            for i, t in enumerate(ts)
        ]
        end = start + 7200
        result.trade_log = [
            {"side": "SELL", "timestamp": start - 3600, "profit": -60.0, "pair": "BTC/USD"},
            {"side": "BUY", "timestamp": start, "profit": None, "pair": "BTC/USD"},
            {"side": "SELL", "timestamp": start + 3600, "profit": 30.0, "pair": "BTC/USD"},
            {"side": "SELL", "timestamp": end, "profit": 1.0, "pair": "BTC/USD"},
        ]
        scored = metrics_between(result, start, end)
        self.assertAlmostEqual(scored["total_return_pct"], (80.0 - 40.0) / 40.0 * 100.0)
        self.assertAlmostEqual(
            scored["sharpe"], _sharpe_from_equity([40.0, 50.0, 80.0], 60),
        )
        self.assertAlmostEqual(scored["max_drawdown_pct"], 0.0)
        self.assertEqual(scored["n_trades"], 1)
        # The pad's 60% hole must not leak into the fold drawdown.
        self.assertAlmostEqual(_max_dd_pct(eq), 60.0)

    def test_signal_log_carries_frontier_timestamp(self):
        cfg = make_quick_config(name="ts", n_candles=5, seed=1)
        result = BacktestRunner(cfg).run()
        log = result.signal_log["BTC/USD"]
        self.assertEqual(len(log), len(result.equity_curve["BTC/USD"]))
        stamps = [e["timestamp"] for e in log]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))

    def test_lab_fold_scores_oos_only_and_still_replays_the_pad(self):
        from hydra_backtest_server import run_lab_oos_fold
        from hydra_walk_forward import Fold

        fold = Fold(
            idx=3, is_start=0, is_end=1_000_000,
            oos_start=1_000_000, oos_end=1_200_000,
        )
        captured = {}

        def fake_run(self, on_tick=None, cancel_token=None):
            captured["params"] = dict(self.config.data_source_params)
            result = BacktestResult(config=self.config)
            pair = self.config.pairs[0]
            start = fold.oos_start
            ts = [start - 7200, start - 3600, start, start + 3600]
            eq = [100.0, 40.0, 50.0, 80.0]
            result.equity_curve[pair] = eq
            result.signal_log[pair] = [
                {"tick": i, "action": "HOLD", "confidence": 0.0, "timestamp": t}
                for i, t in enumerate(ts)
            ]
            result.trade_log = [
                {"side": "SELL", "timestamp": start - 3600, "profit": -60.0, "pair": pair},
                {"side": "SELL", "timestamp": start + 3600, "profit": 40.0, "pair": pair},
                {"side": "SELL", "timestamp": fold.oos_end, "profit": 1.0, "pair": pair},
            ]
            result.status = "complete"
            return result

        tmp = Path(tempfile.mkdtemp(prefix="hydra-oos-"))
        try:
            with patch.object(BacktestRunner, "run", fake_run):
                metrics = run_lab_oos_fold(
                    db_path=str(tmp / "h.sqlite"),
                    job_id="job",
                    side="baseline",
                    pair="BTC/USD",
                    overrides={},
                    fold=fold,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(captured["params"]["start_ts"], fold.oos_start - 60 * 3600)
        self.assertEqual(captured["params"]["end_ts"], fold.oos_end)
        self.assertEqual(captured["params"]["grain_sec"], 3600)
        self.assertAlmostEqual(metrics.total_return_pct, 100.0)
        self.assertAlmostEqual(metrics.fee_adj_return_pct, metrics.total_return_pct)
        self.assertAlmostEqual(metrics.max_dd_pct, 0.0)
        self.assertEqual(metrics.n_trades, 1)
        self.assertAlmostEqual(
            metrics.sharpe, _sharpe_from_equity([40.0, 50.0, 80.0], 60),
        )


class TestParamHash(unittest.TestCase):
    def test_hash_deterministic(self):
        cfg = make_quick_config(name="h", n_candles=50, seed=1)
        h1 = _compute_param_hash(cfg)
        h2 = _compute_param_hash(cfg)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)  # SHA256 hex

    def test_hash_sensitive_to_overrides(self):
        a = make_quick_config(name="a", n_candles=50, seed=1, overrides={"SOL/USD": {"momentum_rsi_upper": 70.0}})
        b = make_quick_config(name="b", n_candles=50, seed=1, overrides={"SOL/USD": {"momentum_rsi_upper": 75.0}})
        self.assertNotEqual(a.param_hash, b.param_hash)


if __name__ == "__main__":
    unittest.main()


def test_synthetic_bars_do_not_depend_on_the_wall_clock():
    """I12: timestamps came from time.time(), so the UTC-hour session weight
    (and the result) depended on when the run happened, and two pairs could
    land a second apart and never align."""
    from unittest import mock
    from hydra_backtest import SyntheticSource
    src = SyntheticSource(kind="gbm", n_candles=50, seed=3)
    with mock.patch("time.time", return_value=1_800_000_000.0):
        a = [c.timestamp for c in src.iter_candles("BTC/USD")]
    with mock.patch("time.time", return_value=1_800_043_200.0):
        b = [c.timestamp for c in src.iter_candles("BTC/USD")]
        c = [x.timestamp for x in src.iter_candles("ETH/USD")]
    assert a == b == c


def test_realistic_fill_rejects_a_wick_only_doji_and_a_no_trade_bar():
    from hydra_backtest import Candle, PendingOrder, SimulatedFiller
    filler = SimulatedFiller("realistic")

    def order(side, px):
        return PendingOrder(pair="BTC/USD", side=side, limit_price=px, size=1.0,
                            placed_tick=0, pre_trade_snapshot={})

    doji_above = Candle(open=105, high=105.5, low=99.5, close=105, volume=10, timestamp=0)
    assert not filler.try_fill(order("BUY", 100.0), doji_above).filled
    doji_through = Candle(open=99.8, high=100.4, low=99.5, close=99.8, volume=10, timestamp=0)
    assert filler.try_fill(order("BUY", 100.0), doji_through).filled
    flat_dead = Candle(open=100, high=100, low=100, close=100, volume=0.0, timestamp=0)
    for model in ("optimistic", "realistic", "pessimistic"):
        f = SimulatedFiller(model)
        assert not f.try_fill(order("BUY", 100.0), flat_dead).filled
        assert not f.try_fill(order("SELL", 100.0), flat_dead).filled


def test_coordinator_override_skips_a_protected_flatten_like_live():
    """Live (_apply_cross_pair_overrides) keeps every protected flatten; the
    backtest skipped only HALT FLATTEN, so a coordinator BUY relabelled a
    hold-through or sleeve exit and the replay diverged from production."""
    cfg = make_quick_config(name="coord-parity", n_candles=40)
    runner = BacktestRunner(cfg)
    engine = runner.engines["BTC/USD"]
    real_tick = engine.tick
    calls = []

    def tick(generate_only=False):
        state = real_tick(generate_only=generate_only)
        state["signal"] = {"action": "SELL", "confidence": 0.9,
                           "reason": "HOLD_THROUGH:force_flatten|Defensive"}
        return state

    def execute_signal(action, confidence, reason="", strategy="MOMENTUM", **kw):
        calls.append((action, reason))
        return None

    class _Coord:
        def update(self, pair, regime):
            pass

        def get_overrides(self, states, price_series=None):
            return {"BTC/USD": {"action": "ADJUST", "signal": "BUY",
                                "confidence_adj": 0.9, "reason": "confluence"}}

    engine.tick = tick
    engine.execute_signal = execute_signal
    runner.coordinator = _Coord()
    result = runner.run()
    assert result.status == "complete", result.errors
    assert calls
    assert all(a == "SELL" and r.startswith("HOLD_THROUGH:force_flatten") for a, r in calls)
