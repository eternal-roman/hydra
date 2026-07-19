"""Determinism gate (Phase 2): same tape + config -> bit-identical output,
including across store round-trips (what `heartbeat replay` relies on)."""

import json

from heartbeat.engine.pipeline import run_tape
from heartbeat.store import Store
from heartbeat.synth import SynthSpec, generate_tape
from helpers import base_config


def _digest(rows):
    import hashlib
    return hashlib.sha256(json.dumps(rows, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def test_two_replays_bit_identical():
    cfg = base_config()
    trades, _ = generate_tape(SynthSpec(seed=42, days=5))
    r1 = run_tape(cfg, "BTC/USD", "1h", trades)
    r2 = run_tape(cfg, "BTC/USD", "1h", trades)
    assert _digest(r1) == _digest(r2)
    assert r1 == r2


def test_replay_through_store_identical(tmp_path):
    cfg = base_config()
    trades, _ = generate_tape(SynthSpec(seed=42, days=3))
    direct = run_tape(cfg, "BTC/USD", "1h", trades)
    store = Store(tmp_path)
    # write in awkward, overlapping chunks to prove reader normalization
    store.append_tape("BTC/USD", "1h", trades[:4000])
    store.append_tape("BTC/USD", "1h", trades[3500:])  # overlap on purpose
    loaded = store.read_tape("BTC/USD", "1h")
    assert loaded == sorted(trades, key=lambda t: t.sort_key())
    via_store = run_tape(cfg, "BTC/USD", "1h", loaded)
    assert _digest(direct) == _digest(via_store)


def test_synth_generator_deterministic():
    a, ia = generate_tape(SynthSpec(seed=9, days=2))
    b, ib = generate_tape(SynthSpec(seed=9, days=2))
    assert a == b and ia == ib
    c, _ = generate_tape(SynthSpec(seed=10, days=2))
    assert a != c
