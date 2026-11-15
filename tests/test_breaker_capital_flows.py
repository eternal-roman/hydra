"""The 15% breakers measure trading losses on Hydra's own books.

A deposit, a withdrawal, a re-split of the quote pool at boot, and coins
the operator holds outside the engine books are capital moving, not a
loss. Each of them used to read as drawdown:

- the portfolio halt was measured on the whole Kraken account, so a
  withdrawal or a fall in coins no engine held blocked every BUY;
- a flat engine re-seeded with a smaller slice kept its old peak and
  halted on the next tick, and a larger slice erased a real drawdown;
- the operator reset was not durable at the portfolio level (resume
  re-armed on the recorded maximum), and at the engine level it re-halted
  on the next tick because a flattened book is cash under its old peak.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from hydra_agent import HydraAgent, CrossPairCoordinator
from hydra_engine import HydraEngine

CORES = ["BTC/USD", "ETH/USD", "ZEC/USD"]
PRICES = {"BTC/USD": 80_000.0, "ETH/USD": 2_000.0, "ZEC/USD": 500.0}
T0 = 1_700_000_000


class _NullBalanceStream:
    healthy = False

    def latest_balances(self):
        return {}


def _bars(engine: HydraEngine, price: float, n: int = 60, start: int = 0) -> None:
    for i in range(start, start + n):
        engine.ingest_candle({"open": price, "high": price, "low": price,
                              "close": price, "volume": 1.0,
                              "timestamp": T0 + i * 3600})


def _quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _agent(tmp_path, usd: float, pairs=CORES, paper: bool = False) -> HydraAgent:
    """A live agent shell with flat engines that have price history."""
    agent = object.__new__(HydraAgent)
    agent.paper = paper
    agent.demo = False
    agent.mode = "competition"
    agent.pairs = list(pairs)
    agent.triangle = None
    agent.candle_interval = 60
    agent._snapshot_dir = str(tmp_path)
    agent.initial_balance = 100.0
    agent._constructor_balance_split = 100.0 / len(pairs)
    agent.balance_stream = _NullBalanceStream()
    agent._cached_balance = {"ZUSD": usd}
    agent._cached_free_balance = None
    agent.engines = {}
    for pair in pairs:
        eng = HydraEngine(initial_balance=100.0 / len(pairs), asset=pair)
        _bars(eng, PRICES[pair])
        agent.engines[pair] = eng
    agent.coordinator = CrossPairCoordinator(list(pairs))
    agent.order_journal = []
    agent._competition_start_balance = None
    agent._portfolio_peak_usd = 0.0
    agent._portfolio_max_drawdown_pct = 0.0
    agent._portfolio_current_drawdown_pct = 0.0
    agent._portfolio_buy_halted = False
    agent.PORTFOLIO_CIRCUIT_BREAKER_PCT = 15.0
    agent._userref_counter = 0
    return agent


def _boot(tmp_path, usd: float, reset: bool = False, pairs=CORES,
          monkeypatch=None) -> HydraAgent:
    """Resume the saved session the way run() does: load, then seed cash."""
    if monkeypatch is not None:
        if reset:
            monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
        else:
            monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER", raising=False)
    agent = _agent(tmp_path, usd, pairs=pairs)
    _quiet(agent._load_snapshot)
    _quiet(agent._set_engine_balances, usd / len(pairs))
    return agent


def _first_session(tmp_path, usd: float, pairs=CORES) -> HydraAgent:
    agent = _agent(tmp_path, usd, pairs=pairs)
    _quiet(agent._set_engine_balances, usd / len(pairs))
    _quiet(agent._arm_portfolio_before_orders)
    return agent


def _hold(engine: HydraEngine, units: float, price: float) -> None:
    """Put `units` on the book at `price`, paid from the engine's cash."""
    engine.balance -= units * price
    engine.position.size = units
    engine.position.avg_entry = price


def _mark(engine: HydraEngine, price: float) -> None:
    last = engine.candles[-1].timestamp
    engine.ingest_candle({"open": price, "high": price, "low": price,
                          "close": price, "volume": 1.0, "timestamp": last + 3600})


# ── the portfolio halt ──────────────────────────────────────────────────

def test_portfolio_halt_reset_survives_the_next_resume(tmp_path, monkeypatch):
    """One operator reset clears the halt for good, not for one boot.

    Resume used to re-arm whenever the recorded maximum drawdown was 15%
    or more. That record never falls, so the next ordinary restart halted
    again even with the account at a new high.
    """
    first = _first_session(tmp_path, 3_000.0)
    btc = first.engines["BTC/USD"]
    _hold(btc, 0.0125, 80_000.0)                       # the whole BTC slice
    for eng in first.engines.values():
        if eng is not btc:
            _hold(eng, eng.balance / PRICES[eng.asset], PRICES[eng.asset])
    for pair, eng in first.engines.items():
        _mark(eng, PRICES[pair] * 0.80)                # every book down 20%
    _quiet(first._arm_portfolio_before_orders)
    assert first._portfolio_buy_halted is True
    for pair, eng in first.engines.items():            # sold at the low
        eng.balance += eng.position.size * eng.prices[-1]
        eng.position.size = 0.0
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 2_400.0, reset=True, monkeypatch=monkeypatch)
    assert second._portfolio_buy_halted is False
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is False, "the reset re-armed on its first tick"
    _quiet(second._save_snapshot)

    third = _boot(tmp_path, 2_400.0, reset=False, monkeypatch=monkeypatch)
    assert third._portfolio_buy_halted is False
    _quiet(third._arm_portfolio_before_orders)
    assert third._portfolio_buy_halted is False
    assert third._portfolio_max_drawdown_pct >= 15.0, "the record is kept"


def test_portfolio_halt_stays_across_resume_until_the_operator_resets(tmp_path, monkeypatch):
    """Control: a restart alone never clears a breach."""
    first = _first_session(tmp_path, 3_000.0)
    for pair, eng in first.engines.items():
        _hold(eng, eng.balance / PRICES[pair], PRICES[pair])
        _mark(eng, PRICES[pair] * 0.80)
    _quiet(first._arm_portfolio_before_orders)
    assert first._portfolio_buy_halted is True
    for pair, eng in first.engines.items():
        _mark(eng, PRICES[pair] * 1.10)                # fully recovered
    _quiet(first._arm_portfolio_before_orders)
    assert first._portfolio_buy_halted is True
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 3_000.0, reset=False, monkeypatch=monkeypatch)
    assert second._portfolio_buy_halted is True


def test_portfolio_reset_rearms_on_a_new_fifteen_percent_loss(tmp_path, monkeypatch):
    """The reset starts a new drawdown epoch; it does not disarm the breaker."""
    first = _first_session(tmp_path, 3_000.0)
    for pair, eng in first.engines.items():
        _hold(eng, eng.balance / PRICES[pair], PRICES[pair])
        _mark(eng, PRICES[pair] * 0.80)
    _quiet(first._arm_portfolio_before_orders)
    for eng in first.engines.values():
        eng.balance += eng.position.size * eng.prices[-1]
        eng.position.size = 0.0
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 2_400.0, reset=True, monkeypatch=monkeypatch)
    for pair, eng in second.engines.items():
        _hold(eng, eng.balance / eng.prices[-1], eng.prices[-1])
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is False
    for eng in second.engines.values():
        _mark(eng, eng.prices[-1] * 0.90)              # 10% down: under the line
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is False
    for eng in second.engines.values():
        _mark(eng, eng.prices[-1] * 0.93)              # 16.3% from the reset point
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is True


def test_withdrawal_between_sessions_is_not_a_portfolio_drawdown(tmp_path, monkeypatch):
    first = _first_session(tmp_path, 3_000.0)
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 2_400.0, monkeypatch=monkeypatch)   # $600 withdrawn
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is False
    assert second._portfolio_current_drawdown_pct == pytest.approx(0.0, abs=1e-6)
    assert second._portfolio_peak_usd == pytest.approx(2_400.0)


def test_capital_flows_keep_a_real_portfolio_drawdown(tmp_path, monkeypatch):
    """A deposit must not erase a drawdown and a withdrawal must not deepen it."""
    first = _first_session(tmp_path, 3_000.0)
    for pair, eng in first.engines.items():
        _hold(eng, eng.balance / PRICES[pair], PRICES[pair])
        _mark(eng, PRICES[pair] * 0.90)                # books down 10%
    _quiet(first._arm_portfolio_before_orders)
    assert first._portfolio_current_drawdown_pct == pytest.approx(10.0, abs=1e-6)
    for eng in first.engines.values():
        eng.balance += eng.position.size * eng.prices[-1]
        eng.position.size = 0.0
    _quiet(first._save_snapshot)                        # $2,700 on the books

    deposit = _boot(tmp_path, 5_400.0, monkeypatch=monkeypatch)
    _quiet(deposit._arm_portfolio_before_orders)
    assert deposit._portfolio_current_drawdown_pct == pytest.approx(10.0, abs=1e-6)
    assert deposit._portfolio_buy_halted is False

    withdrawal = _boot(tmp_path, 1_350.0, monkeypatch=monkeypatch)
    _quiet(withdrawal._arm_portfolio_before_orders)
    assert withdrawal._portfolio_current_drawdown_pct == pytest.approx(10.0, abs=1e-6)
    assert withdrawal._portfolio_buy_halted is False


def test_coins_outside_engine_books_do_not_move_the_portfolio_breaker(tmp_path):
    """BTC the operator holds, on no engine book, falls 22%. Nothing is lost
    on Hydra's books, so its BUYs are not blocked."""
    agent = _first_session(tmp_path, 6_800.0)
    agent._cached_balance = {"ZUSD": 6_800.0, "XXBT": 0.40}
    _quiet(agent._arm_portfolio_before_orders)
    _mark(agent.engines["BTC/USD"], 80_000.0 * 0.78)
    _quiet(agent._arm_portfolio_before_orders)
    assert sum(e.position.size for e in agent.engines.values()) == 0.0
    assert agent._portfolio_buy_halted is False
    assert agent._portfolio_current_drawdown_pct == pytest.approx(0.0, abs=1e-6)


def test_loss_on_engine_books_arms_the_portfolio_breaker(tmp_path):
    """Control: the same fall on coins the engines hold does block BUYs."""
    agent = _first_session(tmp_path, 3_000.0)
    for pair, eng in agent.engines.items():
        _hold(eng, eng.balance / PRICES[pair], PRICES[pair])
    _quiet(agent._arm_portfolio_before_orders)
    for pair, eng in agent.engines.items():
        _mark(eng, PRICES[pair] * 0.84)
    _quiet(agent._arm_portfolio_before_orders)
    assert agent._portfolio_buy_halted is True
    assert agent._portfolio_current_drawdown_pct == pytest.approx(16.0, abs=1e-6)


def test_whole_account_peak_from_an_older_snapshot_is_not_compared_to_engine_books(
        tmp_path, monkeypatch):
    """Snapshots written before the basis changed hold a whole-account peak.
    Read against engine books it is an instant false breach."""
    first = _first_session(tmp_path, 6_800.0)
    _quiet(first._save_snapshot)
    path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    snap = json.load(open(path))
    snap["portfolio_drawdown"] = {"peak_usd": 48_000.0, "max_pct": 6.0}
    json.dump(snap, open(path, "w"))

    second = _boot(tmp_path, 6_800.0, monkeypatch=monkeypatch)
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is False
    assert second._portfolio_peak_usd == pytest.approx(6_800.0)


def test_breach_recorded_by_an_older_snapshot_stays_halted(tmp_path, monkeypatch):
    """Control: an upgrade never clears a halt by itself."""
    first = _first_session(tmp_path, 6_800.0)
    _quiet(first._save_snapshot)
    path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    snap = json.load(open(path))
    snap["portfolio_drawdown"] = {"peak_usd": 48_000.0, "max_pct": 18.0}
    json.dump(snap, open(path, "w"))

    second = _boot(tmp_path, 6_800.0, monkeypatch=monkeypatch)
    assert second._portfolio_buy_halted is True
    _quiet(second._arm_portfolio_before_orders)
    assert second._portfolio_buy_halted is True


def test_unpriced_position_does_not_arm_the_portfolio_breaker(tmp_path):
    """A held position with no price yet would count as worth nothing."""
    agent = _first_session(tmp_path, 3_000.0)
    btc = agent.engines["BTC/USD"]
    _hold(btc, 0.0125, 80_000.0)
    _quiet(agent._arm_portfolio_before_orders)
    btc.prices = []
    btc.candles = []
    _quiet(agent._arm_portfolio_before_orders)
    assert agent._portfolio_buy_halted is False


# ── the per-engine breaker ──────────────────────────────────────────────

def test_withdrawal_between_sessions_does_not_halt_a_flat_engine(tmp_path, monkeypatch):
    first = _first_session(tmp_path, 3_000.0)
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 2_400.0, monkeypatch=monkeypatch)   # 20% withdrawn
    for eng in second.engines.values():
        assert eng.balance == pytest.approx(800.0)
        _quiet(eng.tick)
        assert eng.halted is False, eng.halt_reason
        assert eng.current_drawdown_pct() == pytest.approx(0.0, abs=1e-6)


def test_reseed_keeps_an_engines_drawdown_fraction(tmp_path, monkeypatch):
    """A restart with more or less cash neither erases nor invents a loss."""
    first = _first_session(tmp_path, 3_000.0)
    for eng in first.engines.values():
        eng.balance = 900.0                             # each lost 10% trading
    _quiet(first._save_snapshot)

    for pool in (2_700.0, 5_400.0, 1_350.0):
        resumed = _boot(tmp_path, pool, monkeypatch=monkeypatch)
        for eng in resumed.engines.values():
            assert eng.current_drawdown_pct() == pytest.approx(10.0, abs=1e-6), pool
            _quiet(eng.tick)
            assert eng.halted is False


def test_resplit_does_not_halt_the_winner_or_forgive_the_loser(tmp_path, monkeypatch):
    """Flat engines share the quote pool equally at boot. The engine that
    made money gets less cash back than it had; that is not its loss. The
    engine that lost money gets more; that does not undo its loss."""
    first = _first_session(tmp_path, 3_000.0)
    winner, loser, flat = (first.engines[p] for p in CORES)
    winner.balance, winner.peak_equity = 1_500.0, 1_500.0
    loser.balance = 880.0                               # 12% under its 1,000 peak
    _quiet(first._save_snapshot)

    second = _boot(tmp_path, 3_380.0, monkeypatch=monkeypatch)
    winner, loser, flat = (second.engines[p] for p in CORES)
    assert winner.current_drawdown_pct() == pytest.approx(0.0, abs=1e-6)
    assert loser.current_drawdown_pct() == pytest.approx(12.0, abs=1e-6)
    assert flat.current_drawdown_pct() == pytest.approx(0.0, abs=1e-6)
    for eng in (winner, loser, flat):
        _quiet(eng.tick)
        assert eng.halted is False, (eng.asset, eng.halt_reason)


def test_operator_reset_revives_an_engine_after_a_halt_flatten(monkeypatch):
    """After a breaker flatten the book is cash under its old peak. Cash
    cannot recover and BUYs are blocked, so a reset that kept the peak
    re-halted on the next tick, every time."""
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(tripped, 50_000.0)
    tripped.balance, tripped.peak_equity = 8_410.0, 10_000.0
    tripped.max_drawdown = 15.9
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 15.9% >= 15.0% limit"
    snap = tripped.snapshot_runtime()

    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    revived = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(revived.restore_runtime, snap)
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER")
    assert revived.halted is False
    _mark(revived, 50_000.0)
    _quiet(revived.tick)
    assert revived.halted is False, revived.halt_reason
    assert revived.current_drawdown_pct() == pytest.approx(0.0, abs=1e-6)
    assert revived.max_drawdown == pytest.approx(15.9), "the record is kept"

    # The reset must survive an ordinary restart as well.
    again = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(again.restore_runtime, revived.snapshot_runtime())
    _mark(again, 50_000.0)
    _quiet(again.tick)
    assert again.halted is False, again.halt_reason


def test_reset_engine_rearms_on_a_new_fifteen_percent_loss(monkeypatch):
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(tripped, 50_000.0)
    tripped.balance, tripped.peak_equity = 8_410.0, 10_000.0
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 15.9% >= 15.0% limit"
    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    revived = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(revived.restore_runtime, tripped.snapshot_runtime())
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER")
    _mark(revived, 50_000.0)
    _quiet(revived.tick, True)                          # the epoch starts here

    _hold(revived, 0.1682, 50_000.0)                    # all cash into BTC
    _mark(revived, 50_000.0 * 0.90)
    _quiet(revived.tick, True)
    assert revived.halted is False
    _mark(revived, 50_000.0 * 0.84)
    _quiet(revived.tick, True)
    assert revived.halted is True


def test_new_epoch_starts_at_the_first_live_price_not_the_snapshot_price(monkeypatch):
    """The agent can be down for days. A held position is marked at the
    first tick's price, so the move during the downtime is not counted
    against the epoch the operator just started."""
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(tripped, 50_000.0)
    tripped.balance, tripped.peak_equity = 2_000.0, 10_000.0
    tripped.position.size, tripped.position.avg_entry = 0.128, 60_000.0
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 16.0% >= 15.0% limit"

    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    revived = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(revived.restore_runtime, tripped.snapshot_runtime())
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER")
    assert revived.peak_equity == pytest.approx(2_000.0 + 0.128 * 50_000.0)

    _mark(revived, 40_000.0)                            # 20% lower at boot
    _quiet(revived.tick, True)
    assert revived.halted is False, revived.halt_reason
    assert revived.peak_equity == pytest.approx(2_000.0 + 0.128 * 40_000.0)


def test_rollback_to_a_snapshot_from_before_the_reset_keeps_the_new_peak(monkeypatch):
    """An order placed before the reset carries the old peak in its
    rollback snapshot. Restoring it brought that peak back and the next
    tick halted again, undoing the reset without a word."""
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(tripped, 50_000.0)
    tripped.balance, tripped.peak_equity = 2_000.0, 10_000.0
    tripped.position.size, tripped.position.avg_entry = 0.128, 60_000.0
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 16.0% >= 15.0% limit"
    before_reset = tripped.snapshot_position()          # e.g. the flatten order
    assert before_reset["peak_equity"] == 10_000.0

    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    revived = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(revived.restore_runtime, tripped.snapshot_runtime())
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER")
    _mark(revived, 50_000.0)
    _quiet(revived.tick, True)

    revived.restore_position(before_reset)              # that order is cancelled
    assert revived.peak_equity == pytest.approx(8_400.0)
    _mark(revived, 50_000.0)
    _quiet(revived.tick, True)
    assert revived.halted is False, revived.halt_reason


def test_rollback_within_one_epoch_still_restores_the_peak():
    """Control: an ordinary rollback puts the pre-trade peak back."""
    eng = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(eng, 50_000.0)
    snap = eng.snapshot_position()
    eng.peak_equity = 12_345.0
    eng.restore_position(snap)
    assert eng.peak_equity == 10_000.0


def test_halt_without_the_reset_stays_after_a_flatten(monkeypatch):
    """Control: no env var, no revival."""
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _bars(tripped, 50_000.0)
    tripped.balance, tripped.peak_equity = 8_410.0, 10_000.0
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 15.9% >= 15.0% limit"
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER", raising=False)
    resumed = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(resumed.restore_runtime, tripped.snapshot_runtime())
    assert resumed.halted is True
    assert resumed.peak_equity == 10_000.0


def test_reset_with_an_unpriced_position_rebases_on_the_first_mark(monkeypatch):
    """Candle history can be dropped on resume (interval change). The new
    epoch then starts at the first price, and a restart before that price
    arrives must not lose it."""
    tripped = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    tripped.balance, tripped.peak_equity = 2_000.0, 10_000.0
    tripped.position.size, tripped.position.avg_entry = 0.128, 60_000.0
    tripped.halted = True
    tripped.halt_reason = "CIRCUIT BREAKER: drawdown 16.0% >= 15.0% limit"
    snap = tripped.snapshot_runtime()
    assert snap["candles"] == []

    monkeypatch.setenv("HYDRA_RESET_CIRCUIT_BREAKER", "1")
    revived = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(revived.restore_runtime, snap)
    monkeypatch.delenv("HYDRA_RESET_CIRCUIT_BREAKER")
    assert revived.halted is False

    restarted = HydraEngine(initial_balance=10_000.0, asset="BTC/USD")
    _quiet(restarted.restore_runtime, revived.snapshot_runtime())
    _bars(restarted, 50_000.0)
    _quiet(restarted.tick, True)
    assert restarted.halted is False, restarted.halt_reason
    assert restarted.peak_equity == pytest.approx(2_000.0 + 0.128 * 50_000.0)


def test_unreadable_peak_does_not_drop_a_saved_portfolio_halt(tmp_path, monkeypatch):
    """A damaged number in the snapshot must not clear the halt beside it."""
    first = _first_session(tmp_path, 3_000.0)
    _quiet(first._save_snapshot)
    path = os.path.join(str(tmp_path), "hydra_session_snapshot.json")
    snap = json.load(open(path))
    snap["portfolio_drawdown"] = {"peak_usd": "n/a", "max_pct": None,
                                  "buy_halted": True, "basis": "engine_books"}
    json.dump(snap, open(path, "w"))

    second = _boot(tmp_path, 3_000.0, monkeypatch=monkeypatch)
    assert second._portfolio_buy_halted is True


def test_snapshot_round_trip_carries_the_halt_flag_and_basis(tmp_path):
    agent = _first_session(tmp_path, 3_000.0)
    agent._portfolio_buy_halted = True
    _quiet(agent._save_snapshot)
    saved = json.load(open(os.path.join(str(tmp_path), "hydra_session_snapshot.json")))
    assert saved["portfolio_drawdown"]["buy_halted"] is True
    assert saved["portfolio_drawdown"]["basis"] == "engine_books"
    assert saved["portfolio_drawdown"]["peak_usd"] == pytest.approx(3_000.0)
