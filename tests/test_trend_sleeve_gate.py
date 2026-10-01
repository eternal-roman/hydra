"""Machinery tests for tools/trend_sleeve_gate.py (synthetic data only;
the gate's verdict is decided on real data by the operator)."""
import json
import math
import os
import random
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import trend_sleeve_gate as gate  # noqa: E402
from hydra_engine import HydraEngine  # noqa: E402

DAY = 86400
START_DAY = 16_000  # 2013-10-23


def _trend_cycles(n=2600, seed=1):
    """Persistent drift regimes (a series that trend timing should exploit)."""
    rng = random.Random(seed)
    px, out, drift = 100.0, [], 0.003
    for i in range(n):
        if i % 180 == 0:
            drift = 0.004 if (i // 180) % 2 == 0 else -0.004
        px *= math.exp(drift + rng.gauss(0.0, 0.03))
        out.append(px)
    return out


def test_sleeve_path_matches_a_seeded_engine():
    closes = _trend_cycles(400)
    days = [START_DAY + i for i in range(len(closes))]
    path = gate.sleeve_path(days, closes)
    for k in (215, 300, 399):
        eng = HydraEngine(initial_balance=1000.0, asset="BTC/USD", trend_sleeve=True)
        eng.seed_daily_closes([{"timestamp": d * DAY, "close": c}
                               for d, c in zip(days[:k], closes[:k])])
        eng.ingest_candle({"open": closes[k - 1], "high": closes[k - 1],
                           "low": closes[k - 1], "close": closes[k - 1],
                           "volume": 0.0, "timestamp": days[k] * DAY})
        assert path[k] == (eng.sleeve_trend_score(), eng._sleeve_vol_multiplier())


def test_decision_never_sees_the_day_it_trades():
    closes = _trend_cycles(400)
    days = [START_DAY + i for i in range(len(closes))]
    path = gate.sleeve_path(days, closes)
    shocked = list(closes)
    shocked[350] *= 0.3  # only day 350's own close moves
    path2 = gate.sleeve_path(days, shocked)
    assert path2[350] == path[350]
    assert path2[351] != path[351]


def test_flat_price_round_trip_cost_is_exact():
    closes = [100.0] * 6
    path = [(None, 1.0), (1.0, 1.0), (1.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)]
    run = gate.simulate(closes, path, 1, "sleeve", cost=0.01, cap=0.5)
    equity = 1.0
    for r in run["rets"]:
        equity *= 1.0 + r
    # buy 0.5 notional at 1% cost, sell the same units at 1% cost
    units = 0.5 / 100.0
    expected = (1.0 - units * 100.0 * 1.01) + units * 100.0 * 0.99
    assert equity == pytest.approx(expected, rel=1e-12)


def test_inverse_arm_is_the_complement():
    closes = _trend_cycles(900)
    days = [START_DAY + i for i in range(len(closes))]
    path = gate.sleeve_path(days, closes)
    start = next(i for i, (s, _) in enumerate(path) if s is not None)
    d = gate.simulate(closes, path, start, "sleeve", 0.0)
    e = gate.simulate(closes, path, start, "inverse", 0.0)
    assert d["time_in_market"] + e["time_in_market"] == pytest.approx(1.0, abs=0.02)


def test_buy_and_hold_cap_tracks_the_price_at_cap_exposure():
    closes = [100.0 * (1.001 ** i) for i in range(40)]
    path = [(1.0, 1.0)] * 40
    run = gate.simulate(closes, path, 1, "bh_cap", cost=0.0, cap=0.4)
    assert run["avg_exposure"] == pytest.approx(0.4, abs=0.01)


def test_equal_series_bootstrap_is_zero():
    rng = random.Random(2)
    a = [rng.gauss(0.0005, 0.02) for _ in range(500)]
    boot = gate.paired_block_bootstrap(a, list(a), n_boot=200)
    assert boot["p05"] == 0.0 and boot["p95"] == 0.0
    assert boot["block"] == round(500 ** (1 / 3))


def test_bootstrap_separates_a_real_difference():
    rng = random.Random(4)
    noise = [rng.gauss(0.0, 0.02) for _ in range(1500)]
    a = [x + 0.002 for x in noise]
    b = [x - 0.002 for x in noise]
    boot = gate.paired_block_bootstrap(a, b, n_boot=300)
    assert boot["p05"] > 0


def test_criteria_table():
    def s(sharpe, dd=10.0):
        return {"sharpe": sharpe, "max_dd_pct": dd}
    base = {"sleeve": s(1.2, 20.0), "bh_voltarget": s(1.0, 40.0), "inverse": s(0.1)}
    th = {"sleeve": [s(1.0), s(0.5), s(2.0)], "bh_voltarget": [s(0.8), s(0.9), s(1.0)]}
    stress = {"sleeve": s(1.1), "bh_voltarget": s(1.0)}
    assert all(gate.criteria(base, th, stress).values())
    base["sleeve"] = s(1.2, 31.0)  # 31 > 0.75 * 40
    assert gate.criteria(base, th, stress)["C3_maxdd_le_0.75x_voltarget_bh"] is False
    th["sleeve"] = [s(1.0), s(0.5), s(0.9)]
    assert gate.criteria(base, th, stress)["C2_wins_2_of_3_thirds"] is False


def test_short_history_is_insufficient_not_pass():
    closes = _trend_cycles(600)
    days = [START_DAY + i for i in range(len(closes))]
    out = gate.evaluate_asset(days, closes, boot_n=50)
    assert out["verdict"] == "INSUFFICIENT_DATA"
    assert out["window"]["warmup_days"] >= 210


def test_overall_verdict_rules():
    def asset(v, p05=0.1, g=True, fid=True, done=True):
        return {"verdict": v,
                "bootstrap_sharpe_diff_sleeve_minus_voltarget_bh": {"p05": p05},
                "engine_check": {"complete": done, "G_beats_F": g, "fidelity": fid}}
    two = {"A": asset("PASS"), "B": asset("PASS"), "C": asset("FAIL")}
    assert gate.overall_verdict(two, engine_ran=False) == ("PASS_DAILY_ONLY", True)
    assert gate.overall_verdict(two, engine_ran=True) == ("PASS", True)
    one = {"A": asset("PASS"), "B": asset("INSUFFICIENT_DATA"), "C": asset("FAIL")}
    assert gate.overall_verdict(one, engine_ran=True)[0] == "FAIL"
    thin = {"A": asset("PASS"), "B": asset("INSUFFICIENT_DATA"),
            "C": asset("INSUFFICIENT_DATA")}
    assert gate.overall_verdict(thin, engine_ran=True)[0] == "INSUFFICIENT_DATA"
    fid = {"A": asset("PASS", fid=False), "B": asset("PASS")}
    assert gate.overall_verdict(fid, engine_ran=True)[0] == "FIDELITY_FAIL"
    lose = {"A": asset("PASS", g=False), "B": asset("PASS")}
    assert gate.overall_verdict(lose, engine_ran=True)[0] == "FAIL"
    weak = {"A": asset("PASS", p05=-0.2), "B": asset("PASS")}
    assert gate.overall_verdict(weak, engine_ran=True) == ("PASS", False)


def test_engine_check_fidelity_band():
    arms = {"engine_off": {"status": "complete", "total_return_pct": 5.0},
            "engine_sleeve": {"status": "complete", "total_return_pct": 60.0}}
    ok = gate.engine_check(70.0, arms)
    assert ok == {"complete": True, "G_beats_F": True, "fidelity": True}
    far = gate.engine_check(300.0, arms)
    assert far["fidelity"] is False
    near_zero = gate.engine_check(1.0, {"engine_off": arms["engine_off"],
                                        "engine_sleeve": {"status": "complete",
                                                          "total_return_pct": 4.0}})
    assert near_zero["fidelity"] is True  # inside the 0.05 log-growth floor


def _write_hourly(db, pair, closes_daily, start_day=START_DAY):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS ohlc (pair TEXT, grain_sec INTEGER,
                   ts INTEGER, open REAL, high REAL, low REAL, close REAL,
                   volume REAL, source TEXT, ingested_at INTEGER,
                   PRIMARY KEY (pair, grain_sec, ts))""")
    for d, c in enumerate(closes_daily):
        for h in (0, 12, 23):
            ts = (start_day + d) * DAY + h * 3600
            con.execute("INSERT OR REPLACE INTO ohlc VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (pair, 3600, ts, c, c, c, c, 1.0, "kraken_archive", 0))
    con.commit()
    con.close()


def test_sqlite_resample_takes_the_last_close_of_each_day(tmp_path):
    db = str(tmp_path / "h.sqlite")
    _write_hourly(db, "BTC/USD", [10.0, 11.0, 12.0])
    con = sqlite3.connect(db)
    con.execute("UPDATE ohlc SET close=99 WHERE ts=?", (START_DAY * DAY + 12 * 3600,))
    con.commit()
    con.close()
    assert gate.load_daily_sqlite(db, "BTC/USD") == [
        (START_DAY, 10.0), (START_DAY + 1, 11.0), (START_DAY + 2, 12.0)]


def test_csv_loader_accepts_dates_and_epochs(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("Date,Open,Close\n2020-01-01,1,7100.5\n2020-01-02,1,6950\n", encoding="utf-8")
    rows = gate.load_daily_csv(str(p))
    assert rows == [(18262, 7100.5), (18263, 6950.0)]
    q = tmp_path / "e.csv"
    q.write_text("timestamp,close\n1577836800000,1.5\n", encoding="utf-8")
    assert gate.load_daily_csv(str(q)) == [(18262, 1.5)]


def test_main_writes_a_complete_report(tmp_path):
    db = str(tmp_path / "h.sqlite")
    for pair, seed in (("BTC/USD", 1), ("ETH/USD", 2)):
        _write_hourly(db, pair, _trend_cycles(2400, seed=seed))
    out = tmp_path / "gate.json"
    report = gate.main(["--db", db, "--pairs", "BTC/USD,ETH/USD,ZEC/USD",
                        "--out", str(out), "--boot", "50"])
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["verdict"] == report["verdict"]
    assert saved["verdict"] in ("PASS_DAILY_ONLY", "FAIL", "INSUFFICIENT_DATA")
    assert saved["assets"]["ZEC/USD"]["verdict"] == "INSUFFICIENT_DATA"
    btc = saved["assets"]["BTC/USD"]
    assert btc["window"]["years_evaluated"] >= 5.0
    assert set(btc["arms"]) == set(gate.ARMS)
    assert len(btc["criteria"]) == 5


def test_engine_arms_run_on_the_hourly_tape(tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_TREND_SLEEVE", raising=False)
    db = str(tmp_path / "h.sqlite")
    closes = _trend_cycles(260)
    from hydra_history_store import CandleRow, HistoryStore
    store = HistoryStore(db)
    rows = []
    rng = random.Random(9)
    for d, c in enumerate(closes):
        for h in range(24):
            px = c * math.exp(rng.gauss(0.0, 0.004))
            rows.append(CandleRow("BTC/USD", 3600, (START_DAY + d) * DAY + h * 3600,
                                  px, px * 1.003, px * 0.997, px, 5.0, "kraken_archive"))
    store.upsert_candles(rows)
    start_ts = (START_DAY + 240) * DAY
    end_ts = (START_DAY + 259) * DAY + 23 * 3600
    arms = gate.run_engine_arms(db, "BTC/USD", start_ts, end_ts)
    assert arms["engine_off"]["status"] == "complete"
    assert arms["engine_sleeve"]["status"] == "complete"
    assert arms["engine_sleeve"]["candles"] == arms["engine_off"]["candles"] > 400
    assert "HYDRA_TREND_SLEEVE" not in os.environ


def test_engine_breaker_flattens_and_never_reenters():
    closes = [100.0, 100.0, 100.0, 50.0, 50.0, 60.0, 80.0, 100.0]
    path = [(None, 1.0)] + [(1.0, 1.0)] * 7
    run = gate.simulate(closes, path, 1, "sleeve", cost=0.0, cap=0.4, breaker_pct=15.0)
    assert run["breaker_day_index"] == 3  # 40% exposure x -50% = -20% equity
    assert run["entries"] == 1
    assert all(r == 0.0 for r in run["rets"][3:])  # flat for good after the trip
    free = gate.simulate(closes, path, 1, "sleeve", cost=0.0, cap=0.4)
    assert free["breaker_day_index"] is None
    assert sum(free["rets"][3:]) > 0


def test_report_carries_the_breaker_diagnostic():
    closes = _trend_cycles(600)
    days = [START_DAY + i for i in range(len(closes))]
    out = gate.evaluate_asset(days, closes, boot_n=20)
    diag = out["engine_breaker_diagnostic"]
    assert set(diag) == {"sleeve", "bh_voltarget"}
    assert "breaker_tripped_on" in diag["sleeve"]
    assert 0.0 <= out["sleeve_max_exposure"] <= 1.0


def test_calibration_reports_each_family():
    cal = gate.calibrate(seeds=2, n_days=500)
    assert set(cal["families"]) == {"null_random_walk_bull_drift",
                                     "null_random_walk_zero_drift",
                                     "alt_180d_trend_regimes"}
    for row in cal["families"].values():
        assert row["runs"] == 2


def test_a_failed_engine_arm_is_recorded_not_raised(tmp_path, monkeypatch):
    import hydra_backtest

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no tape")

    monkeypatch.setattr(hydra_backtest, "BacktestRunner", _Boom)
    arms = gate.run_engine_arms(str(tmp_path / "x.sqlite"), "BTC/USD", 0, 3600)
    assert arms["engine_off"]["status"] == "failed"
    assert gate.engine_check(10.0, arms)["complete"] is False


def test_a_foreign_sqlite_is_refused_clearly(tmp_path):
    db = tmp_path / "other.sqlite"
    sqlite3.connect(str(db)).execute("CREATE TABLE t (x)").connection.commit()
    with pytest.raises(SystemExit, match="not a Hydra history store"):
        gate.load_daily_sqlite(str(db), "BTC/USD")
    assert gate.has_hourly(str(db), "BTC/USD") is False
