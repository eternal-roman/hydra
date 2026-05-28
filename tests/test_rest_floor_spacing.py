"""Kraken 2s REST-floor spacing in the order path (audit-2026-05-28 #3).

_place_order makes two distinct REST hits — a validate call and the live
placement — which previously fired back-to-back (<2s apart) and could trip
Kraken's throttle/ban. This is a structural guard (in the spirit of the
derivatives-stream read-only source test): it asserts the order path sleeps
the REST floor before BOTH calls and, specifically, between validate and the
live placement. A behavioral test is impractical here because _place_order
needs a fully wired agent and mock mode no-ops time.sleep.
"""
import inspect
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hydra_agent  # noqa: E402


def test_rest_floor_constant_is_two_seconds():
    assert hydra_agent.KRAKEN_REST_FLOOR_S == 2.0


def test_place_order_spaces_validate_and_live_placement():
    src = inspect.getsource(hydra_agent.HydraAgent._place_order)
    sleeps = [m.start() for m in
              re.finditer(r"time\.sleep\(KRAKEN_REST_FLOOR_S\)", src)]
    # One floor sleep before validate, one before the live placement.
    assert len(sleeps) >= 2, (
        "expected >=2 KRAKEN_REST_FLOOR_S sleeps in _place_order "
        f"(before validate AND before placement), found {len(sleeps)}")

    validate_pos = src.find("validate=True")
    place_pos = src.find("userref=userref")  # live placement passes the userref
    assert validate_pos != -1, "could not locate the validate call"
    assert place_pos != -1, "could not locate the live placement call"
    assert validate_pos < place_pos, "validate must precede live placement"

    # A REST-floor sleep must sit between the validate call and the placement.
    assert any(validate_pos < s < place_pos for s in sleeps), (
        "no KRAKEN_REST_FLOOR_S sleep between validate and live placement — "
        "the two REST calls could fire inside the 2s floor")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("all REST-floor spacing tests passed")
