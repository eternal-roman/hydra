# Pre-registration — quantum-kernel kill-test (ABI frame F-Q)

Registered 2026-10-01, before the runner (`tools/abi_quantum_kernel_killtest.py`)
was executed. Criteria below are frozen; the runner encodes them verbatim.

## Frame

Foreign domain: quantum information. Mapping: embed each standardized
candle-feature vector x in an n-qubit Hilbert space with the ZZ feature map
(Havlicek et al. 2019, Qiskit `ZZFeatureMap`, reps=2, full entanglement);
similarity = state fidelity K(x,x') = |<phi(x)|phi(x')>|^2; classify with a
precomputed-kernel SVM. Testable implication: if next-bar direction (or S3
bounce quality) depends on feature interactions that this embedding captures
and classical kernels miss, the quantum kernel's out-of-sample AUC beats the
best classical baseline on identical features and identical folds.

Emulation note: a classically emulated circuit is classical computation. Any
"advantage" measured here can only be an inductive-bias advantage of this
particular kernel, never a computational one.

## Data (real, committed, no network)

- D1 next-day direction: BTC/ETH/ZEC daily bars,
  `s3bounce/tests/fixtures/parity_*_USD.json` (400 bars each, 2025-06 .. 2026-07).
- D2 next-hour direction: same fixtures' `hourly_sample` (714 x 1h bars each)
  plus hourly prices from `research/grok_paper_opinions/60m_ledger.jsonl`
  (Sept 2026), each contiguous segment featurized separately.
- D3 S3 second stage: `research/data/s3/s3_trade_ledger_x1.json` (69 gated
  trades, 2019-2026); label = trade return > 0; features = the 6 frozen S3
  features (clv, range_atr, vol_z, shock_recency, breadth, retest).

## Features (D1/D2, closes only, all causal at bar t)

r1 = log(c_t/c_{t-1}), r5 = log(c_t/c_{t-5}), r20 = log(c_t/c_{t-20}),
rsi14 (Wilder)/100, z20 = (c_t - SMA20)/stdev20, lvol20 = log(stdev of 20 r1).
Label y_t = 1 if c_{t+1} > c_t.

## Protocol

Expanding-window walk-forward per asset/segment; train strictly before test.
D1/D2: first train block 120 samples, test blocks of 20. D3: yearly folds,
train = years < Y (min 20 trades). Scaling fit on train only (z-score for
classical models; min-max to [0, pi] for the feature map, test clipped).
Fixed hyper-parameters, no tuning on any test data: SVM C=1.0; RBF
gamma='scale'; poly degree 2; logistic C=1.0.

Arms: logistic, RBF-SVM, poly2-SVM, QK-SVM (numpy statevector), and
coin (AUC 0.5). Qiskit `FidelityStatevectorKernel` cross-checks the numpy
kernel on a sample (max abs difference must be < 1e-9, else the run aborts).

## Kill criteria (QK survives only if ALL hold, per dataset)

1. pooled OOS AUC(QK) - max(AUC classical) >= 0.03, AND
2. paired bootstrap (2000 resamples of OOS predictions) 95% CI of that
   difference excludes 0, AND
3. AUC(QK) 95% bootstrap CI excludes 0.5.

Any failure = KILLED for that dataset. Economic readout (not a criterion):
long-only on predictions above the train-median score, 26 bps/side fees,
vs always-long, D1/D2 only.

## Amendment A1 (2026-10-01, BEFORE the first run — no test data seen)

Diagnostic on random uniform inputs (not test data): with angles spanning
[0, pi] the 6-qubit ZZ kernel's mean off-diagonal value is 0.019 — the
documented exponential-concentration failure (kernel ~ identity, SVM
memorizes). Testing only that arm would be a strawman, so the frame is
steelmanned:

- `quantum_svm_bw` (PRIMARY quantum arm): angles = s * pi * minmax(x); the
  bandwidth s is chosen per fold from {0.05, 0.1, 0.2, 0.5, 1.0} by an inner
  holdout on the TRAIN fold only (last 25% of train, chronological), maximizing
  inner AUC, then refit on the full train fold (Shaydulin & Wild 2022).
- `rbf_svm_cv` (matched classical control): gamma chosen per fold from
  {0.01, 0.03, 0.1, 0.3, 1.0} by the identical inner holdout.
- `quantum_svm` (fixed s = 1) is still reported, but the kill criteria are
  applied to `quantum_svm_bw` versus the best of {logistic, rbf_svm,
  poly2_svm, rbf_svm_cv}.
