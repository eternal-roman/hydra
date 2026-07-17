"""Weight fitting + walk-forward evaluation.

Because the engine tracks per-feature decayed sums S_i with
L = sum_i w_i * S_i exactly (see posterior.py), fitting a NO-INTERCEPT
L2-regularized logistic regression on snapshot S vectors IS fitting the
live posterior in its true functional form. Weights learned here drop
straight into config features.weights with zero approximation gap.

Protocol (deterministic, pure stdlib):
  1. per-event design vector: S at the bounce+3 candle close (the
     promotion checkpoint), label 1 = reversal, 0 = fake;
  2. initialize each w_i from a single-feature logistic fit;
  3. joint fit: full-batch gradient descent, L2 penalty, fixed iteration
     count and learning rate (no randomness anywhere);
  4. walk-forward: events sorted by time, expanding-window folds — train
     on all events strictly BEFORE the fold's test window, evaluate AUC
     on the fold's events at each checkpoint. Train/test ranges are
     returned so the report can prove non-overlap.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Sequence

from ..eval.labeler import BounceEvent
from ..eval.metrics import roc_auc
from .posterior import sigmoid


@dataclass(frozen=True)
class EventVector:
    ts: float
    label: int                     # 1 = reversal, 0 = fake
    s_at: dict[str, dict[str, float]]   # checkpoint -> {feature: S}


def event_vectors(events: Sequence[BounceEvent],
                  rows: Sequence[dict],
                  feature_names: Sequence[str],
                  checkpoints: Sequence[str] = ("bounce+1", "bounce+2",
                                                "bounce+3")) -> list[EventVector]:
    """Extract per-event S vectors at each checkpoint from posterior rows.

    rows[i] must align with the candle index used by the labeler.
    """
    import json
    out = []
    for e in events:
        s_at: dict[str, dict[str, float]] = {}
        for cp in checkpoints:
            k = int(cp.split("+")[1])
            idx = e.bounce_idx + k
            if idx >= len(rows):
                continue
            feats = json.loads(rows[idx]["features_json"])
            vec = {}
            for name in feature_names:
                entry = feats.get(name) or {}
                s = entry.get("S")
                vec[name] = float(s) if s is not None else 0.0
            s_at[cp] = vec
        if "bounce+3" in s_at:
            out.append(EventVector(ts=e.low_ts, label=1 if e.label == "reversal" else 0,
                                   s_at=s_at))
    return out


# -- logistic fitting (no intercept, deterministic) ---------------------------

def _fit_logistic(X: list[list[float]], y: list[int], init: list[float],
                  l2: float, lr: float, iters: int) -> list[float]:
    n, k = len(X), len(init)
    if n == 0:
        return list(init)
    w = list(init)
    for _ in range(iters):
        grad = [0.0] * k
        for xi, yi in zip(X, y):
            p = sigmoid(sum(w[j] * xi[j] for j in range(k)))
            err = p - yi
            for j in range(k):
                grad[j] += err * xi[j]
        for j in range(k):
            grad[j] = grad[j] / n + l2 * w[j] / n
            w[j] -= lr * grad[j]
    return w


def fit_weights(vectors: Sequence[EventVector], feature_names: Sequence[str],
                checkpoint: str = "bounce+3",
                l2: float = 1.0, lr: float = 0.05,
                iters: int = 2000) -> dict[str, float]:
    train = [v for v in vectors if checkpoint in v.s_at]
    if not train:
        raise ValueError("no events with the calibration checkpoint")
    y = [v.label for v in train]
    if len(set(y)) < 2:
        raise ValueError("need both classes to calibrate")
    # 1) single-feature fits for initialization
    init = []
    for name in feature_names:
        col = [[v.s_at[checkpoint][name]] for v in train]
        w1 = _fit_logistic(col, y, [0.0], l2, lr, iters // 2)
        init.append(w1[0])
    # 2) joint fit
    X = [[v.s_at[checkpoint][name] for name in feature_names] for v in train]
    w = _fit_logistic(X, y, init, l2, lr, iters)
    return dict(zip(feature_names, w))


# -- walk-forward ---------------------------------------------------------------

def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%d")


def walk_forward(vectors: Sequence[EventVector], feature_names: Sequence[str],
                 folds: int = 4, min_train: int = 20,
                 l2: float = 1.0, lr: float = 0.05,
                 iters: int = 2000) -> list[dict]:
    """Expanding-window walk-forward. Returns one row per usable fold with
    train/test date ranges (train strictly precedes test) and per-checkpoint
    test AUC of the fitted posterior."""
    vecs = sorted(vectors, key=lambda v: v.ts)
    n = len(vecs)
    if n < min_train + 5:
        return []
    chunk = n // (folds + 1)
    results = []
    for f in range(1, folds + 1):
        split = chunk * f
        test_end = chunk * (f + 1) if f < folds else n
        train, test = vecs[:split], vecs[split:test_end]
        if len(train) < min_train or not test:
            continue
        if len({v.label for v in train}) < 2 or len({v.label for v in test}) < 2:
            continue
        w = fit_weights(train, feature_names, l2=l2, lr=lr, iters=iters)
        row = {
            "fold": f,
            "train_range": f"{_iso(train[0].ts)}..{_iso(train[-1].ts)}",
            "test_range": f"{_iso(test[0].ts)}..{_iso(test[-1].ts)}",
            "train_n": len(train), "test_n": len(test),
            "no_overlap": train[-1].ts < test[0].ts,
        }
        for cp in ("bounce+1", "bounce+2", "bounce+3"):
            pos, neg = [], []
            for v in test:
                if cp not in v.s_at:
                    continue
                score = sigmoid(sum(w[name] * v.s_at[cp][name]
                                    for name in feature_names))
                (pos if v.label == 1 else neg).append(score)
            auc = roc_auc(pos, neg)
            row[f"auc_{cp.replace('bounce+', 'bounce')}"] = (
                round(auc, 4) if auc is not None else None)
        row["weights"] = {k: round(v, 4) for k, v in w.items()}
        results.append(row)
    return results
