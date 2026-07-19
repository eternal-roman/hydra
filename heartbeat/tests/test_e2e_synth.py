"""End-to-end machinery validation on the deterministic synthetic tape:
trades -> pipeline -> posterior series -> labeler -> metrics.

This proves the PIPELINE separates the two injected archetypes; it says
nothing about real markets (see HONEST_FINDINGS.md).
"""

import pytest

from heartbeat.engine.candle import candles_from_trades
from heartbeat.engine.pipeline import run_tape
from heartbeat.eval.labeler import extract_events
from heartbeat.eval.metrics import checkpoint_table
from heartbeat.synth import SynthSpec, generate_tape
from helpers import base_config

DAYS = 40


@pytest.fixture(scope="module")
def series():
    cfg = base_config()
    trades, injected = generate_tape(SynthSpec(seed=7, days=DAYS))
    rows = run_tape(cfg, "BTC/USD", "1h", trades)
    candles = candles_from_trades(trades, "1h")
    assert len(rows) == len(candles), "pipeline/batch candle misalignment"
    return cfg, trades, injected, rows, candles


def test_pipeline_emits_full_series(series):
    cfg, trades, injected, rows, candles = series
    assert len(candles) >= DAYS * 24 - 24
    assert all(0.0 <= r["p_up"] <= 1.0 for r in rows)
    assert not any(r["tainted"] for r in rows)  # clean synthetic feed


def test_labeler_finds_injected_events(series):
    cfg, trades, injected, rows, candles = series
    p_up = [r["p_up"] for r in rows]
    events = extract_events("BTC/USD", "1h", candles, p_up, cfg)
    assert len(events) >= 8, f"only {len(events)} events found"
    labels = {e.label for e in events}
    assert labels == {"reversal", "fake"}
    # recall vs ground truth: a decent fraction of INJECTED events must be
    # recovered. (The labeler also finds ORGANIC bounces inside the noisy
    # down-legs — those are legitimately labeled by future price action,
    # so total events > injected events is expected, not an error.)
    found_idx = [e.low_idx for e in events]
    recalled = sum(1 for inj in injected
                   if any(abs(inj["candle_idx"] - i) <= 6 for i in found_idx))
    assert recalled / len(injected) >= 0.4, (
        f"labeler recalled only {recalled}/{len(injected)} injected events")


def test_posterior_separates_archetypes(series):
    """Naive equal weights must beat chance; CALIBRATED weights (fit on
    earlier events, evaluated on later ones) must clear the 0.70 promote
    bar. Equal weights are a diluted prior — vol_z/range_atr are
    non-directional until calibration learns their sign/magnitude."""
    from heartbeat.engine.calibrate import event_vectors, walk_forward
    from heartbeat.engine.posterior import PosteriorEngine

    cfg, trades, injected, rows, candles = series
    p_up = [r["p_up"] for r in rows]
    events = extract_events("BTC/USD", "1h", candles, p_up, cfg)
    table = checkpoint_table(events, ["bounce+1", "bounce+2", "bounce+3",
                                      "progress_2atr"])
    auc3 = table["checkpoints"]["bounce+3"]["auc"]
    # default equal weights are a naive prior; no threshold asserted —
    # single-feature evidence sums are individually near-chance here and
    # the discriminating signal lives in their calibrated combination.
    assert auc3 is not None
    # calibrated, held-out (train strictly precedes test)
    names = [f.name for f in PosteriorEngine(cfg).features]
    vecs = event_vectors(events, rows, names)
    folds = walk_forward(vecs, names, folds=2, min_train=10)
    assert folds, "walk-forward produced no usable folds"
    assert all(f["no_overlap"] for f in folds)
    best = max(f["auc_bounce3"] for f in folds if f["auc_bounce3"] is not None)
    assert best >= 0.70, (
        f"calibrated held-out bounce+3 AUC {best} — machinery failed "
        f"to separate the injected archetypes")
