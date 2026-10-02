"""Null calibration of the Monte Carlo trade bootstrap (stdlib only).

Under zero-edge i.i.d. trade profits, a 95% CI's lower bound should exceed
zero about 2.5% of the time. Measures that rate for the sampler in
hydra_backtest_metrics and for the variants it replaced, and writes
research/data/abi/mc_bootstrap_calibration.json.

Usage: python tools/mc_bootstrap_calibration.py [--trials 400]
"""
import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_backtest_metrics import _block_bootstrap_sample  # noqa: E402


def _old_noncircular(profits, block_len, rng):
    """The sampler before the 2026-10 fix (non-circular, uncapped block)."""
    n = len(profits)
    if block_len <= 0 or block_len >= n:
        return [profits[rng.randint(0, n - 1)] for _ in range(n)]
    out = []
    while len(out) < n:
        start = rng.randint(0, n - block_len)
        out.extend(profits[start:start + block_len])
    return out[:n]


def _circular(profits, block_len, rng):
    n = len(profits)
    out = []
    while len(out) < n:
        s = rng.randrange(n)
        out.extend(profits[(s + j) % n] for j in range(block_len))
    return out[:n]


def _rate(n, sampler, trials, iters=400):
    hits = 0
    for t in range(trials):
        r = random.Random(1000 + t)
        prof = [r.gauss(0.0, 1.0) for _ in range(n)]
        rng = random.Random(t)
        sums = sorted(sum(sampler(prof, rng)) for _ in range(iters))
        hits += sums[int(0.025 * iters)] > 0
    return round(hits / trials, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=400)
    args = ap.parse_args()
    out = {"target_rate": 0.025, "statistic": "sum of resampled trade profits",
           "null": "iid N(0,1) trade profits", "trials": args.trials, "by_n": {}}
    for n in (25, 40, 60, 100):
        out["by_n"][str(n)] = {
            "old_noncircular_L20": _rate(n, lambda p, g: _old_noncircular(p, 20, g), args.trials),
            "circular_L20": _rate(n, lambda p, g: _circular(p, 20, g), args.trials),
            "current_sampler_block_len20": _rate(n, lambda p, g: _block_bootstrap_sample(p, 20, g), args.trials),
            "iid": _rate(n, lambda p, g: [g.choice(p) for _ in p], args.trials),
        }
        print(n, out["by_n"][str(n)])
    path = ROOT / "research" / "data" / "abi" / "mc_bootstrap_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
