from heartbeat.eval.metrics import (brier, calibration_curve, roc_auc,
                                    separation)


def test_auc_hand():
    assert roc_auc([0.9, 0.8], [0.1, 0.2]) == 1.0          # perfect
    assert roc_auc([0.1, 0.2], [0.8, 0.9]) == 0.0          # inverted
    assert roc_auc([0.5], [0.5]) == 0.5                    # tie
    # mixed: pos {0.7, 0.4}, neg {0.5, 0.3}:
    # pairs: (0.7>0.5)+(0.7>0.3)+(0.4<0.5=0)+(0.4>0.3) = 3/4
    assert roc_auc([0.7, 0.4], [0.5, 0.3]) == 0.75
    assert roc_auc([], [0.5]) is None


def test_brier_hand():
    # ((0.8-1)^2 + (0.3-0)^2) / 2 = (0.04 + 0.09)/2 = 0.065
    assert abs(brier([0.8, 0.3], [1, 0]) - 0.065) < 1e-12
    assert brier([], []) is None


def test_separation_hand():
    assert separation([0.8, 0.9, 0.7], [0.2, 0.3, 0.1]) == 0.8 - 0.2


def test_calibration_bins():
    curve = calibration_curve([0.05, 0.95, 0.92], [0, 1, 1], bins=10)
    assert curve[0]["n"] == 1 and curve[0]["obs_freq"] == 0.0
    assert curve[9]["n"] == 2 and curve[9]["obs_freq"] == 1.0
    assert sum(b["n"] for b in curve) == 3
