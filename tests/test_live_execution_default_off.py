"""Money-safety invariant: HYDRA_COMPANION_LIVE_EXECUTION defaults OFF.

Guards the CLAUDE.md contract "`HYDRA_COMPANION_LIVE_EXECUTION` default OFF —
proposals are paper until opted in" and audit-2026-05-28's finding that no
test covered it. live_execution_enabled() must require the env var to be
EXACTLY "1"; every other value (unset, "0", "false", "", "true", "yes") must
read as OFF, and it must also be gated behind proposals being enabled.
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.config import live_execution_enabled  # noqa: E402

LIVE = "HYDRA_COMPANION_LIVE_EXECUTION"
PROP = "HYDRA_COMPANION_PROPOSALS_ENABLED"
DISABLED = "HYDRA_COMPANION_DISABLED"


def _set(key, val):
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val


def _with_env(live=None, proposals=None, fn=None):
    """Isolate companion gates from suite env pollution.

    live_execution_enabled() also requires proposals_enabled() which
    requires the companion not to be disabled — clear DISABLED for the
    duration so parallel/prior tests cannot false-fail the money gate.
    """
    prev_live = os.environ.get(LIVE)
    prev_prop = os.environ.get(PROP)
    prev_dis = os.environ.get(DISABLED)
    try:
        _set(LIVE, live)
        _set(PROP, proposals)
        _set(DISABLED, None)  # companion subsystem on for this probe
        return fn()
    finally:
        _set(LIVE, prev_live)
        _set(PROP, prev_prop)
        _set(DISABLED, prev_dis)


def test_default_unset_is_off():
    assert _with_env(live=None, fn=live_execution_enabled) is False


def test_falsy_strings_are_off():
    for val in ("0", "false", "False", "", "true", "TRUE", "yes", "on", "2"):
        assert _with_env(live=val, fn=live_execution_enabled) is False, val


def test_exact_one_enables():
    assert _with_env(live="1", fn=live_execution_enabled) is True


def test_disabled_when_proposals_off_even_if_live_one():
    # Live execution rides on proposals; if proposals are off, live is off.
    assert _with_env(live="1", proposals="0", fn=live_execution_enabled) is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("all live-execution default-off tests passed")
