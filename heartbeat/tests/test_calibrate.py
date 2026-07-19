"""Calibration: recovery of a known signal + walk-forward hygiene."""

from heartbeat.engine.calibrate import (EventVector, fit_weights,
                                        walk_forward)
from heartbeat.engine.posterior import sigmoid


def _vec(ts, label, s_good, s_noise):
    s = {"good": s_good, "noise": s_noise}
    return EventVector(ts=ts, label=label,
                       s_at={"bounce+1": s, "bounce+2": s, "bounce+3": s})


def _dataset(n=80):
    """`good` separates classes; `noise` is symmetric and uninformative."""
    vecs = []
    for i in range(n):
        label = i % 2
        s_good = (1.0 if label else -1.0) + 0.3 * ((i * 7 % 11) - 5) / 5
        s_noise = ((i * 13 % 17) - 8) / 8
        vecs.append(_vec(ts=1_700_000_000 + i * 86_400, label=label,
                         s_good=s_good, s_noise=s_noise))
    return vecs


def test_fit_recovers_signal_direction():
    vecs = _dataset()
    w = fit_weights(vecs, ["good", "noise"])
    assert w["good"] > 0.5
    assert abs(w["noise"]) < abs(w["good"]) / 3
    # fitted model actually separates
    scores1 = [sigmoid(w["good"] * v.s_at["bounce+3"]["good"]
                       + w["noise"] * v.s_at["bounce+3"]["noise"])
               for v in vecs if v.label == 1]
    scores0 = [sigmoid(w["good"] * v.s_at["bounce+3"]["good"]
                       + w["noise"] * v.s_at["bounce+3"]["noise"])
               for v in vecs if v.label == 0]
    assert min(scores1) > max(scores0) - 0.2


def test_fit_requires_both_classes():
    import pytest
    vecs = [_vec(i, 1, 1.0, 0.0) for i in range(30)]
    with pytest.raises(ValueError, match="both classes"):
        fit_weights(vecs, ["good", "noise"])


def test_walk_forward_no_overlap_and_auc():
    vecs = _dataset(100)
    folds = walk_forward(vecs, ["good", "noise"], folds=4, min_train=10)
    assert len(folds) >= 3
    for f in folds:
        assert f["no_overlap"], "train/test ranges overlap!"
        assert f["auc_bounce3"] is not None and f["auc_bounce3"] > 0.9
    # fold test windows advance in time
    starts = [f["test_range"].split("..")[0] for f in folds]
    assert starts == sorted(starts)


def test_walk_forward_deterministic():
    vecs = _dataset(100)
    a = walk_forward(vecs, ["good", "noise"])
    b = walk_forward(vecs, ["good", "noise"])
    assert a == b
