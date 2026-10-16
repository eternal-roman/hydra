"""Nudge scheduler tests \u2014 Phase 6.

These tests exercise the rate-limit + silence-detection logic without
spinning up a real companion LLM call. The scheduler's LLM path is
mocked via monkeypatching the coordinator's companion.respond method.
"""
import sys
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.nudge_scheduler import (
    NudgeScheduler, DEFAULT_MIN_INTERVAL_S, USER_ACTIVITY_SUPPRESSION_S,
)


def test_nudges_are_opt_in(monkeypatch):
    """Nudges fire on 1h regime flips (no measured edge) and cost an LLM
    call each, so they stay off unless HYDRA_COMPANION_NUDGES=1."""
    from hydra_companions.config import nudges_enabled
    monkeypatch.delenv("HYDRA_COMPANION_DISABLED", raising=False)
    monkeypatch.delenv("HYDRA_COMPANION_NUDGES", raising=False)
    assert nudges_enabled() is False
    monkeypatch.setenv("HYDRA_COMPANION_NUDGES", "0")
    assert nudges_enabled() is False
    monkeypatch.setenv("HYDRA_COMPANION_NUDGES", "1")
    assert nudges_enabled() is True
    monkeypatch.setenv("HYDRA_COMPANION_DISABLED", "1")
    assert nudges_enabled() is False


class StubBroadcaster:
    def __init__(self, state):
        self.latest_state = state
        self.msgs = []

    def broadcast_message(self, t, p):
        self.msgs.append((t, p))


class StubAgent:
    def __init__(self, bc):
        self.broadcaster = bc


class StubResult:
    def __init__(self):
        self.message = "regime shifted. heads up."
        self.error = None
        self.model_used = "xai:grok"
        self.tokens_in = 10
        self.tokens_out = 20
        self.cost_usd = 0.0001


class StubCompanion:
    def __init__(self):
        class S: display_name = "Apex"
        self.soul = S()
        self.respond_calls = 0

    def respond(self, text):
        self.respond_calls += 1
        return StubResult()


class StubCoord:
    def __init__(self):
        self._comps = {"apex": StubCompanion()}

    def get(self, cid, uid="local"):
        return self._comps.get(cid)


def _scheduler(state):
    bc = StubBroadcaster(state)
    agent = StubAgent(bc)
    coord = StubCoord()
    return NudgeScheduler(coordinator=coord, agent=agent), bc, coord


def test_no_fire_without_trigger():
    sched, bc, coord = _scheduler({"pairs": {"SOL/USDC": {"regime": "TREND_UP"}}})
    sched._state.last_nudge_ts = 0
    sched._state.last_user_msg_ts = 0
    sched._tick()
    assert not bc.msgs


def test_fires_on_regime_flip():
    sched, bc, coord = _scheduler({"pairs": {"SOL/USDC": {"regime": "TREND_UP"}}})
    # Prime previous regime so next tick sees a flip.
    sched._tick()  # records TREND_UP, no flip
    sched.agent.broadcaster.latest_state = {"pairs": {"SOL/USDC": {"regime": "VOLATILE"}}}
    sched._state.last_user_msg_ts = 0  # silent user
    sched._state.last_nudge_ts = 0     # no prior nudge
    sched._tick()
    types = [t for t, _ in bc.msgs]
    assert "companion.message.complete" in types


def test_suppressed_after_user_activity():
    sched, bc, coord = _scheduler({"pairs": {"SOL/USDC": {"regime": "TREND_UP"}}})
    sched._tick()  # TREND_UP baseline
    sched.record_user_activity()
    sched.agent.broadcaster.latest_state = {"pairs": {"SOL/USDC": {"regime": "VOLATILE"}}}
    sched._tick()
    # Should NOT fire because user was just active
    assert not bc.msgs


def test_rate_limit_respected():
    sched, bc, coord = _scheduler({"pairs": {"SOL/USDC": {"regime": "TREND_UP"}}})
    sched._tick()  # TREND_UP baseline
    sched._state.last_nudge_ts = time.time()  # just nudged
    sched.agent.broadcaster.latest_state = {"pairs": {"SOL/USDC": {"regime": "VOLATILE"}}}
    sched._tick()
    assert not bc.msgs


def test_mute_silences():
    sched, bc, coord = _scheduler({"pairs": {"SOL/USDC": {"regime": "TREND_UP"}}})
    sched._tick()  # baseline
    sched.mute(3600)
    sched.agent.broadcaster.latest_state = {"pairs": {"SOL/USDC": {"regime": "VOLATILE"}}}
    sched._state.last_user_msg_ts = 0
    sched._state.last_nudge_ts = 0
    sched._tick()
    assert not bc.msgs


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all nudge tests passed")
