#!/usr/bin/env python3
"""HYDRA Risk Manager engine-internal features.

Pure functions that derive portfolio-health signals from data already
available to HydraAgent: engine candle buffers, the order journal,
in-memory balance history, and cross-pair engine handles. Consumed by
`hydra_agent._build_quant_indicators` and surfaced to the Risk Manager
prompt as concrete, articulable flags (replacing RM's prior habit of
producing only "general caution").

════════════════════════════════════════════════════════════════════════
HARD INVARIANT — READ-ONLY / NO SIDE EFFECTS
════════════════════════════════════════════════════════════════════════
Every function here is pure: input dataclasses / lists in, Optional[float]
(or Optional[dict]) out. No mutation. No subprocess. No network. No file
I/O. If a future contributor is tempted to add one, that is a bug and
violates the module's single reason for existence (CLAUDE.md: "Files
that change together should live together").

If `HYDRA_RM_FEATURES_DISABLED=1` in env, callers skip this module
entirely — they should not invoke these functions and then discard
results. The disable check lives in the caller.
════════════════════════════════════════════════════════════════════════

Fields produced (all Optional[float], units as documented per function):
  realized_vol_pct(candles, window_min)   : annualized stddev of log-returns, percent
  drawdown_velocity_pct_per_hr(history)   : peak-to-trough burn rate over trailing window
  fill_rate_24h(journal, now)             : filled / (filled + cancelled + failed), [0,1]
  avg_slippage_bps_24h(journal, now)      : signed (+favorable / -adverse), bps
  cross_pair_corr(returns_a, returns_b)   : Pearson correlation, [-1,1]
  minutes_since_last_trade(journal, now)  : minutes since most recent terminal fill

Returns None whenever input is insufficient for a statistically
meaningful result. Callers pass None straight into the quant_indicators
dict where R10 treats it as missing-field data (distinct from bad
data), and the RM prompt interprets missing fields as "not enough
history to flag on this axis."
"""

import math
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

# Minimum samples for a meaningful Pearson correlation. Below this, the
# confidence interval on r is so wide that the signal misleads more than
# informs. 30 is a conventional floor; our 15m-candle 24h window gives 96.
_CORR_MIN_SAMPLES = 30

# Minimum minutes of balance history before drawdown velocity is computed.
# Less than this and you see startup-noise "drawdowns" that aren't real.
_DDV_MIN_WINDOW_MIN = 10.0

# Seconds → minutes helper (readability only; inline division loses intent).
_SEC_PER_MIN = 60.0

# Minutes in a year (365.25 * 24 * 60) for vol annualization.
_MIN_PER_YEAR = 525960.0


def realized_vol_pct(
    candles: Sequence[Dict],
    window_minutes: int,
) -> Optional[float]:
    """Annualized realized volatility over a trailing window, in percent.

    Args:
        candles: chronological sequence of candle dicts, each with 'close'
            and 'ts' (UNIX seconds). Only the tail inside the window is used.
            Caller passes the engine's own candle buffer; no I/O here.
        window_minutes: how far back to look. The candle duration is
            inferred from the first two candles' 'ts' delta; if fewer
            than 2 candles, returns None.

    Returns:
        Annualized stddev of log-returns × 100, rounded to 2 decimals.
        None if < 3 candles fit the window (stddev of 2 points is
        degenerate; 3 gives 2 returns which is the minimum for sample
        stddev to be non-zero-by-construction).
    """
    if not candles or len(candles) < 3:
        return None
    try:
        candle_minutes = max(1.0, (candles[1]["ts"] - candles[0]["ts"]) / _SEC_PER_MIN)
    except (KeyError, TypeError, IndexError):
        return None

    needed = int(window_minutes / candle_minutes) + 1  # +1 for N-1 returns
    tail = list(candles[-needed:])
    if len(tail) < 3:
        return None

    try:
        log_returns: List[float] = []
        for prev, curr in zip(tail, tail[1:]):
            p0 = float(prev["close"])
            p1 = float(curr["close"])
            if p0 <= 0 or p1 <= 0:
                return None
            log_returns.append(math.log(p1 / p0))
    except (KeyError, TypeError, ValueError):
        return None

    n = len(log_returns)
    if n < 2:
        return None
    mean = sum(log_returns) / n
    var = sum((r - mean) ** 2 for r in log_returns) / (n - 1)  # sample variance
    sigma = math.sqrt(var)
    annualization = math.sqrt(_MIN_PER_YEAR / candle_minutes)
    return round(sigma * annualization * 100.0, 2)


def drawdown_velocity_pct_per_hr(
    history: Iterable[Tuple[float, float]],
    now: float,
    window_minutes: float = 60.0,
) -> Optional[float]:
    """Peak-to-current burn rate over a trailing window, in percent/hour.

    Args:
        history: iterable of (unix_seconds, balance) pairs, chronological
            order not required. Caller typically passes a bounded deque.
        now: current UNIX seconds (caller supplies for testability).
        window_minutes: how far back to look for the peak. Default 60.

    Returns:
        Sign convention: negative = balance falling (real drawdown),
        0.0 = flat or rising, positive = impossible by design (current
        is always <= peak_in_window, since peak is max of window).
        Returns None when the window contains less than
        `_DDV_MIN_WINDOW_MIN` of data or when all samples lie outside
        the window.
    """
    cutoff = now - window_minutes * _SEC_PER_MIN
    in_window = [(ts, bal) for ts, bal in history if ts >= cutoff]
    if not in_window:
        return None
    in_window.sort(key=lambda p: p[0])
    span_min = (in_window[-1][0] - in_window[0][0]) / _SEC_PER_MIN
    if span_min < _DDV_MIN_WINDOW_MIN:
        return None

    peak_ts, peak_bal = max(in_window, key=lambda p: p[1])
    current_ts, current_bal = in_window[-1]
    if peak_bal <= 0:
        return None
    if current_bal >= peak_bal:
        return 0.0
    pct_drop = (current_bal - peak_bal) / peak_bal * 100.0  # negative
    minutes_since_peak = max(1.0, (current_ts - peak_ts) / _SEC_PER_MIN)
    return round(pct_drop * 60.0 / minutes_since_peak, 2)


# Terminal lifecycle states considered for the rate denominator.
# Exclude PLACED and any non-terminal states — those are still in flight.
_TERMINAL_STATES = frozenset({
    "FILLED", "PARTIALLY_FILLED", "CANCELLED_UNFILLED", "PLACEMENT_FAILED",
})
_FILLED_STATES = frozenset({"FILLED", "PARTIALLY_FILLED"})


def _iso_to_ts(iso: str) -> Optional[float]:
    """Robust parser for ISO-8601 stored in the journal. Returns None on
    malformed input rather than raising (caller drops the entry)."""
    if not isinstance(iso, str):
        return None
    try:
        import datetime as _dt
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def _entries_in_window(
    journal: Iterable[Dict], now: float, hours: float,
) -> List[Dict]:
    """Filter journal to entries whose placed_at falls within the trailing
    window. Entries with unparseable timestamps are dropped (robust against
    a historical format drift without crashing the feature pipeline)."""
    cutoff = now - hours * 3600
    out = []
    for e in journal:
        ts = _iso_to_ts(e.get("placed_at", ""))
        if ts is not None and ts >= cutoff:
            out.append(e)
    return out


def fill_rate_24h(journal: Iterable[Dict], now: float) -> Optional[float]:
    """Fraction of terminal orders in last 24h that filled (or partially).

    Returns None when the window has zero terminal orders — no signal
    yet, don't lie. Typical healthy value is 0.6–0.9 on post-only limits.
    Values < 0.3 indicate execution quality issues (price moved away,
    spreads widened, or engine is posting aggressively).
    """
    window = _entries_in_window(journal, now, hours=24.0)
    terminal = [e for e in window if e.get("lifecycle", {}).get("state") in _TERMINAL_STATES]
    if not terminal:
        return None
    filled = [e for e in terminal if e["lifecycle"]["state"] in _FILLED_STATES]
    return round(len(filled) / len(terminal), 3)


def avg_slippage_bps_24h(journal: Iterable[Dict], now: float) -> Optional[float]:
    """Mean slippage of fills in last 24h, in bps, signed.

    Convention:
      positive = favorable to engine (BUY filled < limit, SELL filled > limit)
      negative = adverse (BUY filled > limit, SELL filled < limit)
      zero     = filled exactly at limit

    On post-only orders favorable is the typical case since they only rest
    at or inside the top of book. Persistent negative values indicate the
    engine is chasing, which feeds RM's "bleed watch" concern. Returns
    None when no fills are in the window.
    """
    window = _entries_in_window(journal, now, hours=24.0)
    fills = [
        e for e in window
        if e.get("lifecycle", {}).get("state") in _FILLED_STATES
        and e["lifecycle"].get("avg_fill_price") is not None
        and e.get("intent", {}).get("limit_price") is not None
    ]
    if not fills:
        return None
    bps_list: List[float] = []
    for e in fills:
        side = e.get("side")
        limit = float(e["intent"]["limit_price"])
        filled = float(e["lifecycle"]["avg_fill_price"])
        if limit <= 0:
            continue
        raw = (filled - limit) / limit * 10000.0
        # Negate for BUY so positive always means favorable:
        # BUY favorable when filled < limit (raw negative) -> flip sign
        # SELL favorable when filled > limit (raw positive) -> already right
        signed = -raw if side == "BUY" else raw
        bps_list.append(signed)
    if not bps_list:
        return None
    return round(sum(bps_list) / len(bps_list), 2)


def cross_pair_corr(
    returns_a: Sequence[float],
    returns_b: Sequence[float],
    min_samples: int = _CORR_MIN_SAMPLES,
) -> Optional[float]:
    """Pearson correlation between two return series.

    Truncates both series to the shorter length (caller typically passes
    equal-length candle-aligned returns; this is defensive). Returns None
    on fewer than `min_samples` points or when either series has zero
    variance (correlation undefined). Result clamped to [-1.0, 1.0] to
    absorb floating-point overshoot at the extremes.
    """
    n = min(len(returns_a), len(returns_b))
    if n < min_samples:
        return None
    a = list(returns_a[:n])
    b = list(returns_b[:n])
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a == 0 or var_b == 0:
        return None
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    r = cov / math.sqrt(var_a * var_b)
    # Clamp to absorb FP overshoot past ±1 on perfectly correlated inputs.
    return round(max(-1.0, min(1.0, r)), 4)


def minutes_since_last_trade(
    journal: Iterable[Dict], now: float,
) -> Optional[float]:
    """Time since the most recent FILLED/PARTIALLY_FILLED entry's final_at.

    Returns None when no such entry exists (never traded / fresh install).
    Useful RM cue: a long idle spell after a loss may indicate the engine
    is waiting for setup, but paired with other flags (fill_rate crash,
    DD velocity negative) suggests a stuck state worth flagging.
    """
    fills = [
        e for e in journal
        if e.get("lifecycle", {}).get("state") in _FILLED_STATES
    ]
    if not fills:
        return None
    timestamps = []
    for e in fills:
        ts = _iso_to_ts(e["lifecycle"].get("final_at") or e.get("placed_at", ""))
        if ts is not None:
            timestamps.append(ts)
    if not timestamps:
        return None
    most_recent = max(timestamps)
    return round((now - most_recent) / _SEC_PER_MIN, 1)
