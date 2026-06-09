"""Wilder-smoothing reference tests — v2.26.2 (audit M3).

CLAUDE.md pins RSI/ATR to Wilder exponential smoothing (HIGH severity if
replaced with SMA), but the pre-existing tests only asserted ranges and
direction — a silent swap to SMA smoothing would have passed. These tests
compare the engine against an independently written textbook Wilder
implementation AND prove their own sensitivity: the same series pushed
through an SMA-smoothed variant must disagree, so a regression cannot
hide behind two-implementations-same-bug.
"""
import random
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_engine import Candle, Indicators


# ---------------------------------------------------------------------------
# Independent textbook reference (deliberately different code shape from
# hydra_engine: explicit gain/loss lists + incremental-EMA form).
# ---------------------------------------------------------------------------

def _wilder_smooth(values, period):
    """Seed = SMA of first `period` values, then x += (v - x)/period."""
    x = sum(values[:period]) / period
    for v in values[period:]:
        x += (v - x) / period
    return x


def _ref_rsi(prices, period=14):
    gains, losses = [], []
    for a, b in zip(prices, prices[1:]):
        d = b - a
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _true_ranges(candles):
    out = []
    for prev, cur in zip(candles, candles[1:]):
        out.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return out


def _ref_atr(candles, period=14):
    return _wilder_smooth(_true_ranges(candles), period)


def _sma_rsi(prices, period=14):
    """The forbidden variant: plain SMA of the last `period` gains/losses."""
    gains, losses = [], []
    for a, b in zip(prices, prices[1:]):
        d = b - a
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _random_walk(seed, n, start=100.0, step=1.5):
    rng = random.Random(seed)
    prices = [start]
    for _ in range(n - 1):
        prices.append(max(1.0, prices[-1] + rng.uniform(-step, step)))
    return prices


def _candles_from(prices, seed):
    rng = random.Random(seed)
    out = []
    for i, c in enumerate(prices):
        spread = rng.uniform(0.1, 1.2)
        out.append(Candle(open=c, high=c + spread, low=c - spread,
                          close=c, volume=10.0, timestamp=float(i)))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_rsi_matches_independent_wilder_reference():
    for seed in (7, 42, 1337):
        for n in (15, 30, 80, 200):
            prices = _random_walk(seed, n)
            got = Indicators.rsi(prices, period=14)
            want = _ref_rsi(prices, period=14)
            assert abs(got - want) < 1e-9, (
                f"seed={seed} n={n}: engine {got} != Wilder reference {want}")


def test_rsi_test_is_sensitive_to_sma_swap():
    # A big early spike that Wilder smoothing remembers but a 14-window SMA
    # forgets entirely. If the engine ever matches the SMA variant here, the
    # reference test above would also be failing — this guards the guard.
    prices = [100.0] * 5 + [130.0] + [129.0 - 0.1 * i for i in range(40)]
    wilder = _ref_rsi(prices, period=14)
    sma = _sma_rsi(prices, period=14)
    engine = Indicators.rsi(prices, period=14)
    assert abs(wilder - sma) > 1.0, "fixture no longer separates Wilder from SMA"
    assert abs(engine - wilder) < 1e-9
    assert abs(engine - sma) > 1.0


def test_rsi_analytic_anchors():
    # All-gain series → RSI 100. Alternating equal up/down oscillates around
    # 50 (avg_gain/avg_loss update out of phase under Wilder smoothing) but
    # must stay tightly banded; both engine and reference agree exactly via
    # the test above, this just pins gross behavior.
    assert Indicators.rsi([float(i) for i in range(1, 31)], period=14) == 100.0
    alternating = [100.0 + (0.5 if i % 2 else 0.0) for i in range(40)]
    assert 45.0 < Indicators.rsi(alternating, period=14) < 55.0


def test_atr_matches_independent_wilder_reference():
    for seed in (7, 42, 1337):
        for n in (15, 40, 120):
            candles = _candles_from(_random_walk(seed, n), seed + 1)
            got = Indicators.atr(candles, period=14)
            want = _ref_atr(candles, period=14)
            assert abs(got - want) < 1e-9, (
                f"seed={seed} n={n}: engine {got} != Wilder reference {want}")


def test_atr_pct_series_last_value_matches_atr():
    candles = _candles_from(_random_walk(99, 60), 100)
    series = Indicators.atr_pct_series(candles, period=14)
    atr_now = Indicators.atr(candles, period=14)
    expected_pct = (atr_now / candles[-1].close) * 100.0
    assert abs(series[-1] - expected_pct) < 1e-9


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("all wilder reference tests passed")
