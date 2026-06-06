"""Unit tests for hydra_experiments (Phase 3): Experiment dataclass,
preset library, ExperimentStore CRUD + find_best + prune, sweep, compare.

Stdlib unittest; matches Phase 1/2 project style.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest import BacktestConfig, make_quick_config  # noqa: E402
from hydra_experiments import (  # noqa: E402
    PRESET_LIBRARY,
    ComparisonReport,
    Experiment,
    ExperimentStore,
    build_config_from_preset,
    compare,
    load_presets,
    new_experiment,
    resolve_preset,
    run_experiment,
    sweep_experiment,
    _atomic_write_json,
    _parse_iso_utc,
    _read_tuner_params,
    _safe_pair_filename,
)


class _TempStoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-exp-test-"))
        self.store = ExperimentStore(root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# Preset library
# ═══════════════════════════════════════════════════════════════

class TestPresetLibrary(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_load_bootstraps_disk_copy(self):
        presets_file = self.tmp / "presets.json"
        self.assertFalse(presets_file.exists())
        presets = load_presets(store_root=self.tmp)
        self.assertTrue(presets_file.exists())
        self.assertIn("default", presets)
        self.assertIn("divergent", presets)

    def test_disk_overrides_in_code(self):
        (self.tmp).mkdir(parents=True, exist_ok=True)
        custom = {
            "default": {"description": "user-edited", "overrides": {"foo": 1.0}},
            "custom_new": {"description": "brand new", "overrides": {}},
        }
        _atomic_write_json(self.tmp / "presets.json", custom)
        presets = load_presets(store_root=self.tmp)
        self.assertEqual(presets["default"]["description"], "user-edited")
        self.assertIn("custom_new", presets)
        # In-code presets not in disk file are still available
        self.assertIn("divergent", presets)

    def test_malformed_file_falls_back(self):
        (self.tmp / "presets.json").write_text("not valid json {]")
        presets = load_presets(store_root=self.tmp)
        self.assertIn("default", presets)  # in-code fallback
        self.assertIn("divergent", presets)

    def test_resolve_preset_unknown_raises(self):
        with self.assertRaises(KeyError):
            resolve_preset("totally-fake", ("SOL/USDC",), store_root=self.tmp)

    def test_resolve_preset_divergent_fills_per_pair(self):
        ov = resolve_preset("divergent", ("SOL/USDC", "BTC/USDC"), store_root=self.tmp)
        self.assertIn("SOL/USDC", ov)
        self.assertIn("BTC/USDC", ov)
        self.assertIn("min_confidence_threshold", ov["SOL/USDC"])

    def test_resolve_preset_ideal_reads_tuner(self):
        # Stage a fake tuner file in a tmp tuner_search_dir
        tuner_dir = self.tmp / "tuner"
        tuner_dir.mkdir(parents=True, exist_ok=True)
        safe = _safe_pair_filename("SOL/USDC")
        (tuner_dir / f"hydra_params_{safe}.json").write_text(json.dumps({
            "pair": "SOL/USDC",
            "params": {"momentum_rsi_upper": 77.5, "min_confidence_threshold": 0.68},
        }))
        ov = resolve_preset("ideal", ("SOL/USDC",), store_root=self.tmp,
                            tuner_search_dir=tuner_dir)
        self.assertEqual(ov["SOL/USDC"]["momentum_rsi_upper"], 77.5)

    def test_resolve_preset_ideal_missing_file_empty_ok(self):
        ov = resolve_preset("ideal", ("SOL/USDC",), store_root=self.tmp,
                            tuner_search_dir=self.tmp)  # no tuner files present
        self.assertEqual(ov, {})

    def test_in_code_library_is_complete(self):
        # Sanity: the in-code library has every name the spec expects
        expected = {"default", "ideal", "divergent", "aggressive", "defensive",
                    "regime_trending", "regime_ranging", "regime_volatile"}
        self.assertTrue(expected.issubset(PRESET_LIBRARY.keys()))


# ═══════════════════════════════════════════════════════════════
# Tuner file reading
# ═══════════════════════════════════════════════════════════════

class TestReadTunerParams(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_missing_file_empty(self):
        self.assertEqual(_read_tuner_params("SOL/USDC", search_dir=self.tmp), {})

    def test_well_formed_file(self):
        safe = _safe_pair_filename("SOL/USDC")
        (self.tmp / f"hydra_params_{safe}.json").write_text(json.dumps({
            "pair": "SOL/USDC", "params": {"x": 1.5, "y": 2.0}
        }))
        got = _read_tuner_params("SOL/USDC", search_dir=self.tmp)
        self.assertEqual(got, {"x": 1.5, "y": 2.0})

    def test_malformed_file_empty(self):
        safe = _safe_pair_filename("SOL/USDC")
        (self.tmp / f"hydra_params_{safe}.json").write_text("{ not json")
        self.assertEqual(_read_tuner_params("SOL/USDC", search_dir=self.tmp), {})

    def test_safe_pair_filename(self):
        self.assertEqual(_safe_pair_filename("SOL/USDC"), "SOL_USDC")
        self.assertEqual(_safe_pair_filename("BTC-EUR"), "BTC_EUR")


# ═══════════════════════════════════════════════════════════════
# build_config_from_preset
# ═══════════════════════════════════════════════════════════════

class TestBuildConfigFromPreset(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            build_config_from_preset("nope", pairs=("SOL/USDC",), store_root=self.tmp)

    def test_divergent_produces_overrides(self):
        cfg, ov = build_config_from_preset("divergent", pairs=("SOL/USDC",),
                                           n_candles=50, store_root=self.tmp)
        self.assertIsInstance(cfg, BacktestConfig)
        self.assertIn("min_confidence_threshold", ov["SOL/USDC"])
        # Overrides flow into the frozen config's param_overrides_json
        self.assertEqual(cfg.param_overrides["SOL/USDC"]["min_confidence_threshold"], 0.55)

    def test_aggressive_flips_mode_to_competition(self):
        cfg, _ = build_config_from_preset("aggressive", pairs=("SOL/USDC",),
                                          n_candles=50, store_root=self.tmp)
        self.assertEqual(cfg.mode, "competition")

    def test_extra_overrides_win(self):
        cfg, ov = build_config_from_preset(
            "divergent", pairs=("SOL/USDC",), n_candles=50, store_root=self.tmp,
            extra_overrides={"SOL/USDC": {"min_confidence_threshold": 0.70}},
        )
        self.assertEqual(ov["SOL/USDC"]["min_confidence_threshold"], 0.70)


# ═══════════════════════════════════════════════════════════════
# Experiment construction + round-trip
# ═══════════════════════════════════════════════════════════════

class TestExperiment(unittest.TestCase):
    def test_new_experiment_stamps_config(self):
        cfg = make_quick_config(name="t", n_candles=20)
        exp = new_experiment(name="t", config=cfg, triggered_by="cli", tags=["unit"])
        self.assertTrue(exp.id)
        self.assertTrue(exp.created_at)
        self.assertEqual(exp.triggered_by, "cli")
        self.assertEqual(exp.tags, ["unit"])
        self.assertTrue(exp.config.param_hash)

    def test_to_dict_from_dict_round_trip(self):
        cfg = make_quick_config(name="rt", n_candles=20)
        exp = new_experiment(name="rt", config=cfg)
        d = exp.to_dict()
        roundtrip = Experiment.from_dict(d)
        self.assertEqual(roundtrip.id, exp.id)
        self.assertEqual(roundtrip.name, exp.name)
        self.assertEqual(roundtrip.config.pairs, cfg.pairs)
        self.assertEqual(roundtrip.config.param_hash, cfg.param_hash)


# ═══════════════════════════════════════════════════════════════
# run_experiment
# ═══════════════════════════════════════════════════════════════

class TestRunExperiment(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_runs_and_persists(self):
        cfg = make_quick_config(name="run", n_candles=150, seed=3)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name="run", config=cfg)
        run_experiment(exp, store=self.store)
        self.assertEqual(exp.status, "complete")
        self.assertIsNotNone(exp.result)
        self.assertTrue(self.store.exists(exp.id))

    def test_with_monte_carlo_populates_mc_report(self):
        cfg = make_quick_config(name="mc", n_candles=300, seed=7)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name="mc", config=cfg)
        run_experiment(exp, store=self.store, with_monte_carlo=True, mc_iter=50)
        # mc_report only populated when trade_log has nonzero profits
        if any(t.get("profit") not in (None, 0.0) for t in (exp.result.trade_log or [])):
            self.assertIsNotNone(exp.mc_report)
        else:
            self.assertIsNone(exp.mc_report)

    def test_load_after_save_round_trips(self):
        cfg = make_quick_config(name="rs", n_candles=100, seed=2)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name="rs", config=cfg)
        run_experiment(exp, store=self.store)
        loaded = self.store.load(exp.id)
        self.assertEqual(loaded.id, exp.id)
        self.assertEqual(loaded.result.metrics.total_trades, exp.result.metrics.total_trades)
        self.assertEqual(loaded.status, "complete")

    def test_missing_config_raises(self):
        exp = Experiment(id="x", created_at="", name="bad")
        with self.assertRaises(ValueError):
            run_experiment(exp, store=self.store)

    def test_audit_log_records_start_and_finish(self):
        cfg = make_quick_config(name="au", n_candles=80, seed=1)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name="au", config=cfg)
        run_experiment(exp, store=self.store)
        events = self.store.read_audit()
        # at least one "start" and one "finish" for this id
        kinds = [e.get("event") for e in events if e.get("id") == exp.id]
        self.assertIn("start", kinds)
        self.assertIn("finish", kinds)


# ═══════════════════════════════════════════════════════════════
# ExperimentStore CRUD + query
# ═══════════════════════════════════════════════════════════════

class TestExperimentStore(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def _make_and_run(self, name: str, seed: int = 1, n_candles: int = 100, tags=None):
        cfg = make_quick_config(name=name, n_candles=n_candles, seed=seed)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name=name, config=cfg, tags=tags or [])
        run_experiment(exp, store=self.store)
        return exp

    def test_save_and_exists(self):
        exp = self._make_and_run("x1")
        self.assertTrue(self.store.exists(exp.id))

    def test_delete_returns_false_for_unknown(self):
        self.assertFalse(self.store.delete("no-such-id"))

    def test_delete_removes(self):
        exp = self._make_and_run("del")
        self.assertTrue(self.store.delete(exp.id))
        self.assertFalse(self.store.exists(exp.id))

    def test_list_sorted_created_desc(self):
        e1 = self._make_and_run("first")
        time.sleep(1.05)  # created_at has 1-second granularity
        e2 = self._make_and_run("second")
        listed = self.store.list()
        self.assertEqual(listed[0].id, e2.id)
        self.assertEqual(listed[1].id, e1.id)

    def test_list_limit(self):
        for i in range(3):
            self._make_and_run(f"x{i}")
        self.assertEqual(len(self.store.list(limit=2)), 2)

    def test_list_filter(self):
        a = self._make_and_run("a", tags=["keep"])
        self._make_and_run("b", tags=["skip"])
        listed = self.store.list(filter_fn=lambda e: "keep" in e.tags)
        self.assertEqual([e.id for e in listed], [a.id])

    def test_find_best_respects_min_trades(self):
        e = self._make_and_run("few", n_candles=60)
        # trades likely < 50 → find_best with default threshold returns None
        self.assertIsNone(self.store.find_best(metric="sharpe", min_trades=50))
        # with min_trades=0 it returns a finite result
        best = self.store.find_best(metric="sharpe", min_trades=0)
        self.assertIsNotNone(best)

    def test_prune_by_age(self):
        # Pre-seed a file with a fake old created_at
        old = Experiment(
            id="old-1", created_at="2001-01-01T00:00:00Z", name="ancient",
        )
        self.store.save(old)
        fresh = self._make_and_run("fresh")
        removed = self.store.prune(older_than_days=365)
        self.assertEqual(removed, 1)
        self.assertFalse(self.store.exists(old.id))
        self.assertTrue(self.store.exists(fresh.id))

    def test_prune_keep_tags(self):
        old_keep = Experiment(
            id="old-keep", created_at="2001-01-01T00:00:00Z",
            name="old", tags=["prod"],
        )
        old_drop = Experiment(
            id="old-drop", created_at="2001-01-01T00:00:00Z",
            name="old", tags=[],
        )
        self.store.save(old_keep)
        self.store.save(old_drop)
        removed = self.store.prune(older_than_days=365, keep_tags=["prod"])
        self.assertEqual(removed, 1)
        self.assertTrue(self.store.exists("old-keep"))
        self.assertFalse(self.store.exists("old-drop"))

    def test_corrupt_file_is_skipped_not_fatal(self):
        # Write garbage as an experiment file; iter_experiments must tolerate it
        (self.tmp / "experiments" / "corrupt.json").write_text("not json {[")
        e = self._make_and_run("good")
        listed = self.store.list()
        self.assertEqual([x.id for x in listed], [e.id])

    def test_audit_log_append_only(self):
        self.store.audit_log({"event": "hello"})
        self.store.audit_log({"event": "world"})
        events = self.store.read_audit()
        kinds = [e.get("event") for e in events]
        self.assertIn("hello", kinds)
        self.assertIn("world", kinds)

    def test_log_review_jsonl(self):
        self.store.log_review("exp-1", {"verdict": "NO_CHANGE"})
        self.store.log_review("exp-2", {"verdict": "PARAM_TWEAK"})
        lines = (self.tmp / "review_history.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(l) for l in lines]
        self.assertEqual(parsed[0]["exp_id"], "exp-1")


# ═══════════════════════════════════════════════════════════════
# sweep_experiment + compare
# ═══════════════════════════════════════════════════════════════

class TestSweep(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def test_sweep_empty_values_returns_empty(self):
        cfg = make_quick_config(name="sw-empty", n_candles=50)
        out = sweep_experiment(cfg, param="momentum_rsi_upper", values=[])
        self.assertEqual(out, [])

    def test_sweep_records_one_per_value(self):
        cfg = make_quick_config(name="sw", n_candles=100, seed=2)
        cfg = replace(cfg, coordinator_enabled=False)
        results = sweep_experiment(
            cfg, param="momentum_rsi_upper",
            values=[70.0, 75.0, 80.0], store=self.store,
        )
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.triggered_by, "cli")
            self.assertIn("sweep", r.tags)
            self.assertTrue(self.store.exists(r.id))

    def test_sweep_changes_param_hash(self):
        cfg = make_quick_config(name="sw-hash", n_candles=50, seed=1)
        results = sweep_experiment(cfg, param="momentum_rsi_upper",
                                   values=[70.0, 80.0])
        self.assertNotEqual(results[0].config.param_hash,
                            results[1].config.param_hash)


class TestCompare(unittest.TestCase, _TempStoreMixin):
    setUp = _TempStoreMixin.setUp
    tearDown = _TempStoreMixin.tearDown

    def _run(self, name, seed):
        cfg = make_quick_config(name=name, n_candles=120, seed=seed)
        cfg = replace(cfg, coordinator_enabled=False)
        exp = new_experiment(name=name, config=cfg)
        run_experiment(exp, store=self.store)
        return exp

    def test_compare_empty_returns_empty_report(self):
        report = compare([])
        self.assertIsInstance(report, ComparisonReport)
        self.assertEqual(report.experiments, [])
        self.assertEqual(report.rows, [])

    def test_compare_produces_winners(self):
        a = self._run("a", 1)
        b = self._run("b", 2)
        c = self._run("c", 3)
        report = compare([a, b, c])
        self.assertEqual(len(report.rows), 3)
        self.assertIn("sharpe", report.winner_per_metric)
        # Winner IDs must be members of the compared set
        for _metric, winner_id in report.winner_per_metric.items():
            self.assertIn(winner_id, [a.id, b.id, c.id])

    def test_compare_pairwise_p_values_present(self):
        a = self._run("a", 1)
        b = self._run("b", 2)
        report = compare([a, b])
        self.assertIn((a.id, b.id), report.pairwise_sharpe_p_values)
        p = report.pairwise_sharpe_p_values[(a.id, b.id)]
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


# ═══════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════

class TestUtilities(unittest.TestCase):
    def test_atomic_write_json_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            _atomic_write_json(p, {"a": 1})
            _atomic_write_json(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text())["a"], 2)

    def test_atomic_write_strips_non_finite(self):
        # math.inf would crash json.dump by default — we serialize as null
        import math as _math
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            _atomic_write_json(p, {"a": _math.inf, "b": 1.0})
            data = json.loads(p.read_text())
            self.assertIsNone(data["a"])
            self.assertEqual(data["b"], 1.0)

    def test_parse_iso_utc_roundtrip(self):
        ts = _parse_iso_utc("2025-01-15T10:30:00Z")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)

    def test_parse_iso_utc_bad(self):
        self.assertIsNone(_parse_iso_utc(""))
        self.assertIsNone(_parse_iso_utc("notadate"))


if __name__ == "__main__":
    unittest.main()
