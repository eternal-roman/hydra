"""ABI frame F-Q kill-test: does a quantum-kernel classifier beat classical
baselines at predicting crypto candles?

Pre-registration (criteria frozen before the first run):
    research/data/abi/quantum_kernel_REGISTRATION.md

What "quantum computing API emulation" means here: each feature vector is
encoded into an n-qubit state with the ZZ feature map (Havlicek et al. 2019,
Qiskit ``ZZFeatureMap``, reps=2, full entanglement) and compared by state
fidelity K(x, x') = |<phi(x)|phi(x')>|^2. The circuit is simulated exactly
(statevector), so this is classical computation; the only thing a quantum
kernel can add is a different inductive bias. The test measures whether that
bias helps on REAL committed data, against classical kernels on identical
features and identical walk-forward folds.

Research-only. Requires numpy + scikit-learn (NOT engine dependencies; never
imported by the engine, agent, or test suite). Qiskit + qiskit-machine-learning
are optional and only used to cross-check the numpy kernel.

Usage:
    python tools/abi_quantum_kernel_killtest.py \
        [--out research/data/abi/quantum_kernel_killtest.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.svm import SVC

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "s3bounce" / "tests" / "fixtures"
GROK_LEDGER = ROOT / "research" / "grok_paper_opinions" / "60m_ledger.jsonl"
S3_LEDGER = ROOT / "research" / "data" / "s3" / "s3_trade_ledger_x1.json"
PAIRS = ("BTC_USD", "ETH_USD", "ZEC_USD")

REPS = 2
FIRST_TRAIN = 120
TEST_BLOCK = 20
FEE_PER_SIDE = 0.0026
BOOT = 2000
SEED = 20261001
MIN_AUC_EDGE = 0.03


# ── features ────────────────────────────────────────────────────────────

def _wilder_rsi(closes: np.ndarray, t: int, period: int = 14) -> float:
    if t < period:
        return 50.0
    diffs = np.diff(closes[: t + 1])
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def featurize(closes: np.ndarray):
    """Causal close-only features at bar t; label = next close up."""
    xs, ys, rets, ts_idx = [], [], [], []
    logc = np.log(closes)
    r1 = np.diff(logc, prepend=np.nan)
    for t in range(21, len(closes) - 1):
        win = closes[t - 19: t + 1]
        sd = win.std()
        rv = np.nanstd(r1[t - 19: t + 1])
        if sd <= 0 or rv <= 0:
            continue
        xs.append([
            logc[t] - logc[t - 1],
            logc[t] - logc[t - 5],
            logc[t] - logc[t - 20],
            _wilder_rsi(closes, t) / 100.0,
            (closes[t] - win.mean()) / sd,
            math.log(rv),
        ])
        nxt = closes[t + 1] / closes[t] - 1.0
        ys.append(1 if nxt > 0 else 0)
        rets.append(nxt)
        ts_idx.append(t)
    return np.array(xs), np.array(ys), np.array(rets), ts_idx


# ── quantum kernel (numpy statevector, verified against Qiskit) ─────────

def _zz_phase_table(n: int):
    """Bit matrix for all 2^n basis states (qubit i = bit i, little-endian
    like Qiskit) and pair-parity matrix for the full-entanglement ZZ map."""
    idx = np.arange(2 ** n)
    bits = ((idx[:, None] >> np.arange(n)[None, :]) & 1).astype(float)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    parity = np.stack([np.logical_xor(bits[:, i], bits[:, j]) for i, j in pairs],
                      axis=1).astype(float) if pairs else np.zeros((2 ** n, 0))
    return bits, parity, pairs


def zz_states(X: np.ndarray, reps: int = REPS) -> np.ndarray:
    """Exact statevectors of ZZFeatureMap(x) for each row of X (angles in
    [0, pi]). U = prod_reps [diag(exp(i*2*phase(b))) H^n], phase(b) =
    sum_i x_i b_i + sum_{i<j} (pi-x_i)(pi-x_j) (b_i XOR b_j)."""
    n = X.shape[1]
    dim = 2 ** n
    bits, parity, pairs = _zz_phase_table(n)
    # H^n as a dense matrix (dim <= 64 here)
    h1 = np.array([[1, 1], [1, -1]]) / math.sqrt(2)
    hn = np.array([[1.0]])
    for _ in range(n):
        hn = np.kron(hn, h1)
    out = np.empty((X.shape[0], dim), dtype=complex)
    for k, x in enumerate(X):
        pair_coef = np.array([(math.pi - x[i]) * (math.pi - x[j]) for i, j in pairs])
        phase = bits @ x + (parity @ pair_coef if pairs else 0.0)
        diag = np.exp(1j * 2.0 * phase)
        psi = np.zeros(dim, dtype=complex)
        psi[0] = 1.0
        for _ in range(reps):
            psi = diag * (hn @ psi)
        out[k] = psi
    return out


def fidelity_kernel(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    return np.abs(A.conj() @ B.T) ** 2


def qiskit_crosscheck(X: np.ndarray) -> dict:
    """Max |K_numpy - K_qiskit| on a sample; aborts the run if > 1e-9."""
    try:
        from qiskit.circuit.library import zz_feature_map
        from qiskit_machine_learning.kernels import FidelityStatevectorKernel
    except Exception as e:  # optional dependency
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}
    fmap = zz_feature_map(feature_dimension=X.shape[1], reps=REPS, entanglement="full")
    kq = FidelityStatevectorKernel(feature_map=fmap).evaluate(x_vec=X)
    s = zz_states(X)
    kn = fidelity_kernel(s, s)
    diff = float(np.max(np.abs(kq - kn)))
    if diff > 1e-9:
        raise SystemExit(f"numpy kernel disagrees with Qiskit (max diff {diff})")
    return {"available": True, "n": int(X.shape[0]), "max_abs_diff": diff}


# ── walk-forward ────────────────────────────────────────────────────────

ARMS = ("logistic", "rbf_svm", "poly2_svm", "rbf_svm_cv", "quantum_svm",
        "quantum_svm_bw")
PRIMARY_QK = "quantum_svm_bw"          # Amendment A1: the steelman arm
CLASSICAL = ("logistic", "rbf_svm", "poly2_svm", "rbf_svm_cv")
QK_BANDWIDTHS = (0.05, 0.1, 0.2, 0.5, 1.0)
RBF_GAMMAS = (0.01, 0.03, 0.1, 0.3, 1.0)
INNER_FRAC = 0.25


def _qk_angles(Xtr, X, s):
    lo, hi = Xtr.min(0), Xtr.max(0)
    span = np.where(hi > lo, hi - lo, 1.0)
    return np.clip((X - lo) / span, 0.0, 1.0) * math.pi * s


def _qk_fit(Xtr, ytr, s):
    Str = zz_states(_qk_angles(Xtr, Xtr, s))
    m = SVC(C=1.0, kernel="precomputed").fit(fidelity_kernel(Str, Str), ytr)
    return m, Str


def _qk_scores(Xtr, ytr, Xte, s):
    m, Str = _qk_fit(Xtr, ytr, s)
    Ste = zz_states(_qk_angles(Xtr, Xte, s))
    return (m.decision_function(fidelity_kernel(Ste, Str)),
            m.decision_function(fidelity_kernel(Str, Str)))


def _inner_pick(Xtr, ytr, candidates, score_fn):
    """Chronological inner holdout on the TRAIN fold only; returns the
    candidate with the best inner AUC (first wins ties)."""
    cut = int(len(Xtr) * (1.0 - INNER_FRAC))
    a_X, a_y, b_X, b_y = Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:]
    if len(set(a_y)) < 2 or len(set(b_y)) < 2:
        return candidates[len(candidates) // 2]
    best, best_auc = candidates[0], -1.0
    for c in candidates:
        auc = roc_auc_score(b_y, score_fn(a_X, a_y, b_X, c))
        if auc > best_auc:
            best, best_auc = c, auc
    return best


def _fit_predict(arm: str, Xtr, ytr, Xte):
    mu, sd = Xtr.mean(0), Xtr.std(0)
    sd[sd == 0] = 1.0
    Ztr, Zte = (Xtr - mu) / sd, (Xte - mu) / sd
    if arm == "logistic":
        m = LogisticRegression(C=1.0, max_iter=2000).fit(Ztr, ytr)
        return m.decision_function(Zte), m.decision_function(Ztr)
    if arm == "rbf_svm":
        m = SVC(C=1.0, kernel="rbf", gamma="scale").fit(Ztr, ytr)
        return m.decision_function(Zte), m.decision_function(Ztr)
    if arm == "poly2_svm":
        m = SVC(C=1.0, kernel="poly", degree=2).fit(Ztr, ytr)
        return m.decision_function(Zte), m.decision_function(Ztr)
    if arm == "rbf_svm_cv":
        def _rbf(aX, ay, bX, g):
            m_ = SVC(C=1.0, kernel="rbf", gamma=g).fit(aX, ay)
            return m_.decision_function(bX)
        g = _inner_pick(Ztr, ytr, RBF_GAMMAS, _rbf)
        m = SVC(C=1.0, kernel="rbf", gamma=g).fit(Ztr, ytr)
        return m.decision_function(Zte), m.decision_function(Ztr)
    if arm == "quantum_svm":
        return _qk_scores(Xtr, ytr, Xte, 1.0)
    if arm == "quantum_svm_bw":
        s_bw = _inner_pick(Xtr, ytr, QK_BANDWIDTHS,
                           lambda aX, ay, bX, c: _qk_scores(aX, ay, bX, c)[0])
        return _qk_scores(Xtr, ytr, Xte, s_bw)
    raise ValueError(arm)


def walk_forward(X, y, folds):
    """folds: list of (train_idx, test_idx). Returns per-arm OOS score
    arrays aligned with the concatenated test indices, plus per-arm
    train-median thresholds for the economic readout."""
    scores = {a: [] for a in ARMS}
    thresholds = {a: [] for a in ARMS}
    order = []
    for tr, te in folds:
        if len(set(y[tr])) < 2:
            continue
        for arm in ARMS:
            s_te, s_tr = _fit_predict(arm, X[tr], y[tr], X[te])
            scores[arm].append(s_te)
            thresholds[arm].append(np.full(len(te), np.median(s_tr)))
        order.extend(te)
    return ({a: np.concatenate(v) if v else np.array([]) for a, v in scores.items()},
            {a: np.concatenate(v) if v else np.array([]) for a, v in thresholds.items()},
            np.array(order, dtype=int))


def expanding_folds(n: int):
    folds, start = [], FIRST_TRAIN
    while start < n:
        end = min(n, start + TEST_BLOCK)
        folds.append((np.arange(0, start), np.arange(start, end)))
        start = end
    return folds


def _auc(y, s):
    return float(roc_auc_score(y, s)) if len(set(y)) == 2 else float("nan")


def judge(y, scores, rng):
    """Apply the registered kill criteria to pooled OOS scores."""
    aucs = {a: _auc(y, s) for a, s in scores.items()}
    best = max(CLASSICAL, key=lambda a: aucs[a])
    n = len(y)
    diffs, q_boot = [], []
    for _ in range(BOOT):
        i = rng.integers(0, n, n)
        if len(set(y[i])) < 2:
            continue
        q = _auc(y[i], scores[PRIMARY_QK][i])
        c = _auc(y[i], scores[best][i])
        diffs.append(q - c)
        q_boot.append(q)
    d_lo, d_hi = np.percentile(diffs, [2.5, 97.5])
    q_lo, q_hi = np.percentile(q_boot, [2.5, 97.5])
    edge = aucs[PRIMARY_QK] - aucs[best]
    c1 = edge >= MIN_AUC_EDGE
    c2 = d_lo > 0
    c3 = q_lo > 0.5 or q_hi < 0.5
    return {
        "n_oos": int(n),
        "auc": {a: round(v, 4) for a, v in aucs.items()},
        "primary_quantum_arm": PRIMARY_QK,
        "best_classical": best,
        "qk_minus_best_classical": round(edge, 4),
        "diff_ci95": [round(float(d_lo), 4), round(float(d_hi), 4)],
        "qk_auc_ci95": [round(float(q_lo), 4), round(float(q_hi), 4)],
        "criteria": {"c1_edge_ge_0.03": bool(c1), "c2_diff_ci_excludes_0": bool(c2),
                     "c3_qk_ci_excludes_0.5": bool(c3)},
        "verdict": "SURVIVES" if (c1 and c2 and c3) else "KILLED",
    }


def economic_readout(y_rets, scores, thresholds):
    """Long-only when score > train-median score; fees on each entry/exit."""
    out = {}
    for arm in ARMS:
        long = scores[arm] > thresholds[arm]
        equity, prev = 1.0, False
        for want, r in zip(long, y_rets):
            if want != prev:
                equity *= (1.0 - FEE_PER_SIDE)
            if want:
                equity *= (1.0 + r)
            prev = want
        if prev:
            equity *= (1.0 - FEE_PER_SIDE)
        out[arm] = {"ret_pct": round((equity - 1.0) * 100.0, 2),
                    "time_in_market": round(float(long.mean()), 3)}
    bh = float(np.prod(1.0 + y_rets)) * (1.0 - FEE_PER_SIDE) ** 2
    out["always_long"] = {"ret_pct": round((bh - 1.0) * 100.0, 2), "time_in_market": 1.0}
    return out


# ── datasets ────────────────────────────────────────────────────────────

def _segments_daily():
    for p in PAIRS:
        d = json.loads((FIXTURES / f"parity_{p}.json").read_text())
        yield p, np.array([b["close"] for b in d["bars"]], dtype=float)


def _segments_hourly():
    for p in PAIRS:
        d = json.loads((FIXTURES / f"parity_{p}.json").read_text())
        yield f"{p}:jun-jul", np.array([b["close"] for b in d["hourly_sample"]], dtype=float)
    rows = [json.loads(line) for line in GROK_LEDGER.read_text().splitlines() if line.strip()]
    for p in PAIRS:
        slash = p.replace("_", "/")
        series = sorted((r["ts"], r["price"]) for r in rows if r["pair"] == slash)
        yield f"{p}:sep", np.array([c for _, c in series], dtype=float)


def run_candle_dataset(segments, rng):
    all_y, all_r, all_scores, all_thr = [], [], {a: [] for a in ARMS}, {a: [] for a in ARMS}
    per_segment = {}
    feature_sample = None
    for name, closes in segments:
        X, y, rets, _ = featurize(closes)
        if feature_sample is None and len(X) >= 24:
            lo, hi = X.min(0), X.max(0)
            feature_sample = np.clip((X[:24] - lo) / np.where(hi > lo, hi - lo, 1.0), 0, 1) * math.pi
        scores, thr, order = walk_forward(X, y, expanding_folds(len(X)))
        if len(order) == 0:
            continue
        per_segment[name] = {"n_samples": int(len(X)), "n_oos": int(len(order)),
                             "auc": {a: round(_auc(y[order], scores[a]), 4) for a in ARMS},
                             "up_rate_oos": round(float(y[order].mean()), 3)}
        all_y.append(y[order])
        all_r.append(rets[order])
        for a in ARMS:
            all_scores[a].append(scores[a])
            all_thr[a].append(thr[a])
    y = np.concatenate(all_y)
    r = np.concatenate(all_r)
    scores = {a: np.concatenate(v) for a, v in all_scores.items()}
    thr = {a: np.concatenate(v) for a, v in all_thr.items()}
    return {"per_segment": per_segment, "pooled": judge(y, scores, rng),
            "economic_long_only": economic_readout(r, scores, thr)}, feature_sample


def run_s3_dataset(rng):
    trades = json.loads(S3_LEDGER.read_text())
    feats = ("clv", "range_atr", "vol_z", "shock_recency", "breadth", "retest")
    trades.sort(key=lambda t: t["entry_date"])
    X = np.array([[float(t[f]) for f in feats] for t in trades])
    y = np.array([1 if float(t["ret"]) > 0 else 0 for t in trades])
    years = np.array([int(t["year"]) for t in trades])
    folds = []
    for yr in sorted(set(years)):
        tr = np.where(years < yr)[0]
        te = np.where(years == yr)[0]
        if len(tr) >= 20 and len(te):
            folds.append((tr, te))
    scores, _, order = walk_forward(X, y, folds)
    res = judge(y[order], scores, rng)
    res["base_win_rate_oos"] = round(float(y[order].mean()), 3)
    res["folds"] = [int(sorted(set(years[te]))[0]) for _, te in folds]
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "research" / "data" / "abi"
                                         / "quantum_kernel_killtest.json"))
    args = ap.parse_args()
    rng = np.random.default_rng(SEED)

    d1, sample = run_candle_dataset(_segments_daily(), rng)
    crosscheck = qiskit_crosscheck(sample)
    d2, _ = run_candle_dataset(_segments_hourly(), rng)
    d3 = run_s3_dataset(rng)
    result = {
        "registration": "research/data/abi/quantum_kernel_REGISTRATION.md",
        "feature_map": {"name": "ZZFeatureMap", "reps": REPS, "entanglement": "full",
                        "qubits": 6, "simulation": "exact statevector (numpy)"},
        "qiskit_crosscheck": crosscheck,
        "fixed_hyperparameters": {"svm_C": 1.0, "rbf_gamma": "scale", "poly_degree": 2,
                                  "logistic_C": 1.0, "first_train": FIRST_TRAIN,
                                  "test_block": TEST_BLOCK, "bootstrap": BOOT, "seed": SEED},
        "D1_next_day_direction": d1,
        "D2_next_hour_direction": d2,
        "D3_s3_second_stage": d3,
    }
    verdicts = {k: result[k]["pooled"]["verdict"] if "pooled" in result[k] else result[k]["verdict"]
                for k in ("D1_next_day_direction", "D2_next_hour_direction", "D3_s3_second_stage")}
    result["verdict"] = {"per_dataset": verdicts,
                         "frame": "SURVIVES" if any(v == "SURVIVES" for v in verdicts.values())
                         else "KILLED"}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["verdict"], indent=2))
    for k in ("D1_next_day_direction", "D2_next_hour_direction"):
        print(k, json.dumps(result[k]["pooled"]["auc"]), result[k]["pooled"]["verdict"])
    print("D3_s3_second_stage", json.dumps(d3["auc"]), d3["verdict"])
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
