"""Deterministic synthetic Kraken-like tape generator.

Purpose: offline verification of the ENTIRE pipeline (feed-independent):
candles, features, posterior, labeler, calibration, replay determinism.
It encodes the core hypothesis into the tape so the eval harness has
ground truth to find:

  * REVERSAL bounces: after the local low, aggressive taker BUYING
    persists for several candles (positive OFI, buy streaks, volume,
    upper-third closes) and price advances beyond 3.3x ATR.
  * FAKE bounces: price pops ~1-2x ATR on PASSIVE fills — sell-aggressor
    share stays high, volume contracts, OFI decays by candle 2 — then
    rolls over and prints a new low.

IMPORTANT HONESTY NOTE: metrics computed on this tape validate the
MACHINERY (the pipeline can separate the two archetypes when they exist),
not the market hypothesis. Real-market numbers require the real tape
(see HONEST_FINDINGS.md).

Everything derives from random.Random(seed) — no wall clock anywhere.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .feed.tape import Side, Trade


@dataclass(frozen=True)
class SynthSpec:
    seed: int = 7
    start_ts: float = 1_700_000_000.0   # any fixed epoch; not "now"
    days: int = 90
    tf_s: int = 3600
    start_price: float = 60_000.0
    base_vol_bps: float = 25.0          # per-candle sigma in bps
    trades_per_candle: int = 40
    event_every_candles: int = 24       # try to seed one event per ~day
    p_reversal: float = 0.45


def generate_tape(spec: SynthSpec) -> tuple[list[Trade], list[dict]]:
    """Returns (trades, injected_events). injected_events records ground
    truth [{candle_idx, kind}] for test assertions (the labeler must find
    a decent fraction of these and label them consistently)."""
    rng = random.Random(spec.seed)
    n_candles = spec.days * 86400 // spec.tf_s
    price = spec.start_price
    trades: list[Trade] = []
    injected: list[dict] = []

    # regime script: alternating down-legs and recoveries so the labeler's
    # "established down-leg" precondition actually occurs.
    drift_bps = -6.0
    i = 0
    while i < n_candles:
        leg = rng.randint(18, 30)          # down-leg length
        # --- down-leg
        for k in range(leg):
            if i >= n_candles:
                break
            price = _emit_candle(trades, rng, spec, i, price,
                                 drift_bps=drift_bps, ofi_bias=-0.25,
                                 vol_mult=1.0)
            i += 1
        if i >= n_candles - 12:
            break
        # --- bounce event at the leg low
        kind = "reversal" if rng.random() < spec.p_reversal else "fake"
        injected.append({"candle_idx": i, "kind": kind})
        if kind == "reversal":
            # undercut low then persistent aggressive buying, 6-9 candles
            price = _emit_candle(trades, rng, spec, i, price, drift_bps=-20,
                                 ofi_bias=0.25, vol_mult=1.8, wick_reclaim=True)
            i += 1
            run = rng.randint(6, 9)
            for k in range(run):
                if i >= n_candles:
                    break
                price = _emit_candle(trades, rng, spec, i, price,
                                     drift_bps=+55.0, ofi_bias=+0.70,
                                     vol_mult=1.7, close_upper=True)
                i += 1
        else:
            # passive-fill pop: price up ~2 candles on NEGATIVE flow and
            # fading volume, then rollover to a new low
            for k in range(2):
                if i >= n_candles:
                    break
                price = _emit_candle(trades, rng, spec, i, price,
                                     drift_bps=+35.0, ofi_bias=-0.45,
                                     vol_mult=0.5, close_mid=True)
                i += 1
            for k in range(rng.randint(3, 5)):
                if i >= n_candles:
                    break
                price = _emit_candle(trades, rng, spec, i, price,
                                     drift_bps=-45.0, ofi_bias=-0.60,
                                     vol_mult=1.1)
                i += 1
        # --- recovery drift so legs don't run to zero
        rec = rng.randint(8, 16)
        for k in range(rec):
            if i >= n_candles:
                break
            price = _emit_candle(trades, rng, spec, i, price,
                                 drift_bps=+12.0, ofi_bias=+0.10, vol_mult=0.9)
            i += 1

    for j, t in enumerate(trades):  # assign monotone trade ids
        trades[j] = Trade(ts=t.ts, price=t.price, qty=t.qty, side=t.side,
                          ord_type=t.ord_type, trade_id=j + 1)
    return trades, injected


def _emit_candle(trades: list[Trade], rng: random.Random, spec: SynthSpec,
                 idx: int, price: float, drift_bps: float, ofi_bias: float,
                 vol_mult: float, wick_reclaim: bool = False,
                 close_upper: bool = False, close_mid: bool = False) -> float:
    """Emit one candle's worth of trades; returns the close price.

    ofi_bias in [-1, 1]: probability of buy-aggressor = 0.5 + bias/2.
    """
    open_ts = spec.start_ts + idx * spec.tf_s
    n = max(6, int(spec.trades_per_candle * vol_mult
                   * (0.7 + 0.6 * rng.random())))
    sigma = price * spec.base_vol_bps / 10_000.0
    drift = price * drift_bps / 10_000.0
    p = price
    path: list[float] = []
    for k in range(n):
        p += drift / n + rng.gauss(0, sigma / math.sqrt(n))
        path.append(max(p, 1e-6))
    if wick_reclaim:  # undercut mid-candle, close back near the top
        dip = min(path) - 2.2 * sigma
        third = n // 3
        for k in range(third, min(third + max(2, n // 6), n)):
            path[k] = max(dip + rng.gauss(0, sigma / 8), 1e-6)
        for k in range(n - max(2, n // 8), n):
            path[k] = price + abs(rng.gauss(0, sigma / 4))
    if close_upper:
        top = max(path)
        for k in range(n - max(2, n // 8), n):
            path[k] = top - abs(rng.gauss(0, sigma / 10))
    if close_mid:
        mid = (max(path) + min(path)) / 2
        for k in range(n - max(2, n // 8), n):
            path[k] = mid + rng.gauss(0, sigma / 10)
    p_buy = 0.5 + ofi_bias / 2.0
    for k in range(n):
        ts = open_ts + (k + 0.5) * spec.tf_s / n
        side = Side.BUY if rng.random() < p_buy else Side.SELL
        qty = abs(rng.gauss(0.5, 0.35)) + 0.01
        if side is Side.BUY and ofi_bias > 0.3:
            qty *= 1.6   # large-trader footprint on reversals
        trades.append(Trade(ts=ts, price=round(path[k], 1), qty=round(qty, 6),
                            side=side, ord_type="limit", trade_id=0))
    return path[-1]
