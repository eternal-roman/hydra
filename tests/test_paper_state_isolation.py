"""Paper sessions must never read or write the live state files.

Regression: start_hydra_companion.bat runs ``--paper`` from the production
directory. Paper wrote hydra_session_snapshot.json, hydra_order_journal.json
and hydra_params_<pair>.json beside the live ones, so the next live
``--resume`` restored the paper books over real positions and counted paper
fills in live realized P&L.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hydra_agent
from hydra_agent import (
    HydraAgent, PAPER_STATE_DIR, journal_row_matches_mode, state_dir_for_mode,
)
from hydra_engine import HydraEngine, CrossPairCoordinator

AGENT_DIR = os.path.dirname(os.path.abspath(hydra_agent.__file__))


def test_live_state_dir_is_agent_dir():
    assert state_dir_for_mode(False) == AGENT_DIR


def test_paper_state_dir_is_separate_and_created():
    path = state_dir_for_mode(True)
    assert path == os.path.join(AGENT_DIR, PAPER_STATE_DIR)
    assert os.path.isdir(path)
    assert path != state_dir_for_mode(False)


def test_journal_row_mode_filter():
    live_row = {"intent": {"paper": False}}
    paper_row = {"intent": {"paper": True}}
    legacy_row = {"intent": {"amount": 1.0}}  # predates the flag: live
    assert journal_row_matches_mode(live_row, paper=False)
    assert not journal_row_matches_mode(paper_row, paper=False)
    assert journal_row_matches_mode(legacy_row, paper=False)
    assert journal_row_matches_mode(paper_row, paper=True)
    assert not journal_row_matches_mode(live_row, paper=True)
    assert not journal_row_matches_mode(legacy_row, paper=True)
    assert not journal_row_matches_mode("garbage", paper=False)


def _shell(tmp_path, paper: bool) -> HydraAgent:
    agent = object.__new__(HydraAgent)
    agent.paper = paper
    agent.pairs = ["BTC/USD"]
    agent.triangle = None
    agent._snapshot_dir = str(tmp_path)
    agent.engines = {"BTC/USD": HydraEngine(initial_balance=500.0, asset="BTC/USD")}
    agent.coordinator = CrossPairCoordinator(agent.pairs)
    agent.order_journal = []
    agent._competition_start_balance = None
    agent._portfolio_peak_usd = 0.0
    agent._portfolio_max_drawdown_pct = 0.0
    agent._userref_counter = 0
    return agent


def _snapshot(paper: bool, size: float) -> dict:
    eng = HydraEngine(initial_balance=500.0, asset="BTC/USD")
    eng.position.size = size
    eng.position.avg_entry = 60000.0
    return {
        "version": 1,
        "timestamp": "2026-10-01T00:00:00+00:00",
        "mode": "competition",
        "paper": paper,
        "pairs": ["BTC/USD"],
        "competition_start_balance": 500.0,
        "engines": {"BTC/USD": eng.snapshot_runtime()},
        "coordinator_regime_history": {},
        "order_journal": [],
        "userref_counter": 0,
    }


def test_live_resume_refuses_a_paper_snapshot(tmp_path):
    agent = _shell(tmp_path, paper=False)
    with open(os.path.join(str(tmp_path), "hydra_session_snapshot.json"), "w") as f:
        json.dump(_snapshot(paper=True, size=0.002), f)
    out = io.StringIO()
    with redirect_stdout(out):
        agent._load_snapshot()
    assert "REFUSED" in out.getvalue()
    assert agent.engines["BTC/USD"].position.size == 0.0


def test_live_resume_still_restores_a_live_snapshot(tmp_path):
    agent = _shell(tmp_path, paper=False)
    with open(os.path.join(str(tmp_path), "hydra_session_snapshot.json"), "w") as f:
        json.dump(_snapshot(paper=False, size=0.01), f)
    agent._load_snapshot()
    assert agent.engines["BTC/USD"].position.size == 0.01


def test_live_journal_merge_drops_paper_rows(tmp_path):
    agent = _shell(tmp_path, paper=False)
    agent.ORDER_JOURNAL_CAP = 2000
    rows = [
        {"placed_at": "2026-10-01T00:00:01", "pair": "BTC/USD", "side": "BUY",
         "intent": {"amount": 0.01, "paper": False},
         "order_ref": {"order_id": "OLIVE"}, "lifecycle": {"state": "FILLED"}},
        {"placed_at": "2026-10-01T00:00:02", "pair": "BTC/USD", "side": "BUY",
         "intent": {"amount": 0.002, "paper": True},
         "order_ref": {"order_id": None}, "lifecycle": {"state": "FILLED"}},
    ]
    with open(os.path.join(str(tmp_path), "hydra_order_journal.json"), "w") as f:
        json.dump(rows, f)
    out = io.StringIO()
    with redirect_stdout(out):
        agent._merge_order_journal()
    assert [r["order_ref"]["order_id"] for r in agent.order_journal] == ["OLIVE"]
    assert "dropped 1 paper-mode rows" in out.getvalue()


def test_paper_trackers_write_under_paper_dir():
    from hydra_tuner import ParameterTracker
    tracker = ParameterTracker(pair="BTC/USD", save_dir=state_dir_for_mode(True))
    assert os.path.dirname(tracker.save_path) == os.path.join(AGENT_DIR, PAPER_STATE_DIR)
