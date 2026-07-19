"""Evaluation metrics: ROC-AUC, Brier, separation, lead-time, calibration.

Pure stdlib, deterministic. Positive class = "reversal" (P(up) should be
HIGH for reversals, LOW for fakes).
"""

from __future__ import annotations

from statistics import median
from typing import Optional, Sequence


def roc_auc(scores_pos: Sequence[float], scores_neg: Sequence[float]) -> Optional[float]:
    """Mann-Whitney AUC: P(score_pos > score_neg) + 0.5*P(equal)."""
    if not scores_pos or not scores_neg:
        return None
    wins = 0.0
    for p in scores_pos:
        for n in scores_neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(scores_pos) * len(scores_neg))


def brier(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    if not scores:
        return None
    if len(scores) != len(labels):
        raise ValueError("scores/labels length mismatch")
    return sum((s - y) ** 2 for s, y in zip(scores, labels)) / len(scores)


def separation(scores_pos: Sequence[float], scores_neg: Sequence[float]) -> Optional[float]:
    if not scores_pos or not scores_neg:
        return None
    return median(scores_pos) - median(scores_neg)


def calibration_curve(scores: Sequence[float], labels: Sequence[int],
                      bins: int = 10) -> list[dict]:
    """Per-bin (mean predicted, observed frequency, count)."""
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores)
               if (lo <= s < hi) or (b == bins - 1 and s == hi)]
        if not idx:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0,
                        "mean_pred": None, "obs_freq": None})
            continue
        mean_pred = sum(scores[i] for i in idx) / len(idx)
        obs = sum(labels[i] for i in idx) / len(idx)
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                    "mean_pred": round(mean_pred, 4), "obs_freq": round(obs, 4)})
    return out


def checkpoint_table(events, checkpoints: Sequence[str],
                     auc_promote: float = 0.70) -> dict:
    """Full metric block per checkpoint + lead-time."""
    table: dict = {"checkpoints": {}, "lead_time": None,
                   "n_events": len(events),
                   "n_reversal": sum(1 for e in events if e.label == "reversal"),
                   "n_fake": sum(1 for e in events if e.label == "fake")}
    for cp in checkpoints:
        pos = [e.p_at[cp] for e in events
               if e.label == "reversal" and e.p_at.get(cp) is not None]
        neg = [e.p_at[cp] for e in events
               if e.label == "fake" and e.p_at.get(cp) is not None]
        scores = pos + neg
        labels = [1] * len(pos) + [0] * len(neg)
        table["checkpoints"][cp] = {
            "n": len(scores),
            "auc": _r(roc_auc(pos, neg)),
            "brier": _r(brier(scores, labels)),
            "separation": _r(separation(pos, neg)),
            "calibration": calibration_curve(scores, labels),
        }
    for cp in ("bounce+1", "bounce+2", "bounce+3"):
        m = table["checkpoints"].get(cp)
        if m and m["auc"] is not None and m["auc"] >= auc_promote:
            table["lead_time"] = cp
            break
    return table


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return round(x, nd) if x is not None else None
