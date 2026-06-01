#!/usr/bin/env python3
"""
HYDRA Tuner — Self-Tuning Parameters via Exponential Smoothing

Tracks which parameter values led to winning vs losing trades and
shifts thresholds toward profitable values (and away from losing values).
Conservative shift per update cycle prevents overfitting.

Usage:
    from hydra_tuner import ParameterTracker
    tracker = ParameterTracker(pair="SOL/USD")
    tracker.record_trade(params, "BUY", "win", profit=12.50)
    updated = tracker.update()  # returns new params if >= 20 observations
"""

import json
import math
import os
import time
from collections import deque
from typing import Dict, List, Optional, Any


# ═══════════════════════════════════════════════════════════════
# DEFAULT PARAMETERS & BOUNDS
# ═══════════════════════════════════════════════════════════════

DEFAULT_PARAMS = {
    "volatile_atr_mult": 1.8,
    "volatile_bb_mult": 1.8,
    "trend_ema_ratio": 1.005,
    "momentum_rsi_lower": 30.0,
    "momentum_rsi_upper": 70.0,
    "mean_reversion_rsi_buy": 35.0,
    "mean_reversion_rsi_sell": 65.0,
    "min_confidence_threshold": 0.65,
}

# Hard bounds — parameters are clamped to these ranges to prevent
# degenerate configurations. No RSI threshold below 10 or above 90, etc.
PARAM_BOUNDS = {
    "volatile_atr_mult": (1.2, 3.0),
    "volatile_bb_mult": (1.2, 3.0),
    "trend_ema_ratio": (1.001, 1.02),
    "momentum_rsi_lower": (10.0, 45.0),
    "momentum_rsi_upper": (55.0, 90.0),
    "mean_reversion_rsi_buy": (10.0, 45.0),
    "mean_reversion_rsi_sell": (55.0, 90.0),
    "min_confidence_threshold": (0.55, 0.80),
}

# How much to shift toward the winning mean per update cycle
SHIFT_RATE = 0.10

# Minimum number of observations before updating
MIN_OBSERVATIONS = 20


# ═══════════════════════════════════════════════════════════════
# PARAMETER TRACKER
# ═══════════════════════════════════════════════════════════════

class ParameterTracker:
    """Tracks trade outcomes against parameter snapshots and tunes thresholds.

    For each completed trade, stores the parameter values that were active
    when the entry signal was generated along with the outcome (win/loss/profit).
    After enough observations, shifts each parameter 10% toward the mean
    value observed in winning trades.

    Args:
        pair: Trading pair (e.g. "SOL/USD")
        save_dir: Directory to persist params JSON (default: cwd)
        defaults: Override default parameter values
    """

    def __init__(self, pair: str, save_dir: str = None,
                 defaults: Optional[Dict[str, float]] = None):
        self.pair = pair
        safe_pair = pair.replace("/", "_")
        self._save_dir = save_dir or os.path.dirname(os.path.abspath(__file__))
        self.save_path = os.path.join(self._save_dir, f"hydra_params_{safe_pair}.json")
        self._defaults = defaults or dict(DEFAULT_PARAMS)
        self.observations: List[Dict[str, Any]] = []
        self.update_count = 0
        self.current_params = self._load_or_default()
        # Phase 11 (v2.10.0) — rollback history for external updates (e.g.,
        # shadow-validator-promoted changes). Bounded depth=1 so a rollback
        # always reverts exactly one apply, never cascades.
        self._param_history: "deque[Dict[str, float]]" = deque(maxlen=1)

    def get_tunable_params(self) -> Dict[str, float]:
        """Return a copy of the current tunable parameters."""
        return dict(self.current_params)

    def record_trade(self, params_at_signal: Dict[str, float], signal: str,
                     outcome: str, profit: float):
        """Record a completed trade with the parameters that generated it.

        Args:
            params_at_signal: Snapshot of tunable params at BUY entry time
            signal: Signal action that initiated the trade ("BUY" or "SELL")
            outcome: "win" or "loss"
            profit: Realized profit/loss in quote currency
        """
        self.observations.append({
            "params": dict(params_at_signal),
            "signal": signal,
            "outcome": outcome,
            "profit": profit,
            "timestamp": time.time(),
        })

    def update(self) -> Dict[str, float]:
        """Run exponential-smoothing update if enough observations accumulated.

        For each parameter:
        1. Split observations into win/loss buckets
        2. Compute mean parameter value for wins (and losses if available)
        3. Shift current value toward the winning mean
        4. Shift away from the losing mean (at half rate) when loss data exists
        5. Clamp to hard bounds

        Returns:
            Updated parameter dict (unchanged if < MIN_OBSERVATIONS)
        """
        if len(self.observations) < MIN_OBSERVATIONS:
            return dict(self.current_params)

        # Split into win/loss
        wins = [o for o in self.observations if o["outcome"] == "win"]
        losses = [o for o in self.observations if o["outcome"] == "loss"]

        if not wins:
            # No winning trades — nothing to learn toward
            return dict(self.current_params)

        changes: Dict[str, tuple] = {}  # param -> (old, new)

        for param_name in DEFAULT_PARAMS:
            # Collect parameter values from winning trades. Observations missing
            # this parameter (e.g. recorded before the param existed) are skipped
            # entirely rather than defaulted — defaulting would fabricate fake
            # datapoints and bias the learned mean toward the default value.
            win_values = [o["params"][param_name]
                          for o in wins if param_name in o.get("params", {})]

            if not win_values:
                continue

            win_mean = sum(win_values) / len(win_values)
            old_val = self.current_params[param_name]

            # Shift toward winning mean
            new_val = old_val + SHIFT_RATE * (win_mean - old_val)

            # Also shift away from losing mean (at half rate) when loss data exists
            loss_values = [o["params"][param_name]
                           for o in losses if param_name in o.get("params", {})]
            if loss_values:
                loss_mean = sum(loss_values) / len(loss_values)
                # Push away from loss mean at half the attraction rate
                new_val += (SHIFT_RATE * 0.5) * (new_val - loss_mean)

            # Reject non-finite intermediates (defensive: a corrupted observation
            # or hand-edited params file could introduce NaN/Inf, which would
            # propagate through max/min and poison the tuner silently).
            if not math.isfinite(new_val):
                continue

            # Clamp to hard bounds
            lo, hi = PARAM_BOUNDS[param_name]
            new_val = max(lo, min(hi, new_val))

            if abs(new_val - old_val) > 1e-8:
                changes[param_name] = (old_val, new_val)
                self.current_params[param_name] = new_val

        self.update_count += 1

        # Clear observations after update to start fresh accumulation
        self.observations.clear()

        # Persist to disk
        self._save()

        return dict(self.current_params)

    def get_changes_log(self, old_params: Dict[str, float]) -> List[str]:
        """Compare old params to current and return human-readable change list."""
        lines = []
        for key in sorted(DEFAULT_PARAMS):
            old_val = old_params.get(key, self._defaults[key])
            new_val = self.current_params.get(key, self._defaults[key])
            if abs(new_val - old_val) > 1e-8:
                lines.append(f"    {key}: {old_val:.6f} → {new_val:.6f}")
        return lines

    def _load_or_default(self) -> Dict[str, float]:
        """Load saved params from disk, or return defaults.

        v2.15.0 hardening: on any parse failure (corrupt JSON,
        unreadable file), move the bad file aside as `<path>.rejected.<ts>`
        so the operator can inspect it, then fall back to defaults. A
        summary line is printed on every successful load so silent
        drift ('why is my tuner ignoring my saved file?') is visible.
        """
        if not os.path.exists(self.save_path):
            return dict(self._defaults)
        try:
            with open(self.save_path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            self._quarantine_bad_file(reason=f"{type(e).__name__}: {e}")
            return dict(self._defaults)

        if not isinstance(data, dict):
            self._quarantine_bad_file(reason="top-level not an object")
            return dict(self._defaults)

        params = dict(self._defaults)
        saved = data.get("params") or {}
        if not isinstance(saved, dict):
            self._quarantine_bad_file(reason="params field not an object")
            return dict(self._defaults)

        clamped = 0
        loaded = 0
        for key in DEFAULT_PARAMS:
            if key not in saved:
                continue
            try:
                val = float(saved[key])
            except (TypeError, ValueError):
                clamped += 1
                continue
            if not math.isfinite(val):
                clamped += 1
                continue
            lo, hi = PARAM_BOUNDS[key]
            bounded = max(lo, min(hi, val))
            if bounded != val:
                clamped += 1
            params[key] = bounded
            loaded += 1
        try:
            self.update_count = int(data.get("update_count", 0) or 0)
        except (TypeError, ValueError):
            self.update_count = 0
        print(
            f"  [TUNER] {self.pair}: loaded {loaded}/{len(DEFAULT_PARAMS)} "
            f"params ({clamped} clamped/rejected) from {self.save_path}"
        )
        return params

    def _quarantine_bad_file(self, reason: str) -> None:
        ts = int(time.time())
        bad = f"{self.save_path}.rejected.{ts}"
        try:
            os.replace(self.save_path, bad)
            print(
                f"  [TUNER] {self.pair}: rejected params file "
                f"({reason}); quarantined to {bad}; using defaults"
            )
        except OSError as e:
            print(
                f"  [TUNER] {self.pair}: rejected params file ({reason}); "
                f"quarantine failed ({type(e).__name__}: {e}); using defaults"
            )

    def _save(self):
        """Persist current params to disk."""
        data = {
            "pair": self.pair,
            "params": self.current_params,
            "update_count": self.update_count,
            "last_updated": time.time(),
        }
        try:
            with open(self.save_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # Mirrors HF-003 fix in hydra_agent.py: previously silently swallowed.
            # Surfacing the failure means the outer tick-body try/except in
            # hydra_agent.py will log the traceback to hydra_errors.log.
            print(f"  [WARN] tuner save failed for {self.pair}: {type(e).__name__}: {e}")

    # ─── External write path (Phase 11, v2.10.0) ──────────────────────
    # External write path for an approved PARAM_TWEAK candidate that cleared
    # validation and received explicit human approval. Deliberately
    # distinct from the tuner's own exponential-smoothing update loop:
    # external updates apply immediately, preserve rollback state, and
    # carry a `source` tag in the audit trail.

    def apply_external_param_update(
        self,
        params: Dict[str, float],
        source: str = "external",
    ) -> Dict[str, Any]:
        """Apply an externally-proposed param update (e.g., shadow-approved).

        Invariants:
          - Unknown keys silently ignored (forward-compat for new params).
          - Non-finite values rejected (mirror _load_or_default).
          - Values clamped to PARAM_BOUNDS — extreme proposals become
            boundary values, never degenerate configs.
          - Previous params saved to `_param_history` so
            rollback_to_previous() restores exactly this snapshot.
          - Does NOT bump `update_count` — that counter is reserved for the
            tuner's own observation-driven updates. External bumps are
            tracked per-apply via the returned dict.
        """
        self._param_history.append(dict(self.current_params))

        applied: Dict[str, float] = {}
        skipped: List[str] = []
        for key, raw in (params or {}).items():
            if key not in DEFAULT_PARAMS:
                skipped.append(f"unknown:{key}")
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                skipped.append(f"nan:{key}")
                continue
            if not math.isfinite(val):
                skipped.append(f"nonfinite:{key}")
                continue
            lo, hi = PARAM_BOUNDS[key]
            clamped = max(lo, min(hi, val))
            self.current_params[key] = clamped
            applied[key] = clamped

        if applied:
            self._save()
        else:
            # Nothing applied — don't bloat history with a dead snapshot
            if self._param_history:
                self._param_history.pop()

        return {
            "applied": applied,
            "skipped": skipped,
            "source": source,
            "timestamp": time.time(),
            "pair": self.pair,
        }

    def rollback_to_previous(self) -> bool:
        """Revert the single most-recent `apply_external_param_update`.

        Returns True on success, False when no snapshot is available
        (either no prior external apply, or already rolled back once).
        """
        if not self._param_history:
            return False
        prior = self._param_history.pop()
        self.current_params = dict(prior)
        self._save()
        return True

    def reset(self):
        """Reset parameters to defaults and delete saved file."""
        self.current_params = dict(self._defaults)
        self.observations.clear()
        self.update_count = 0
        try:
            if os.path.exists(self.save_path):
                os.remove(self.save_path)
        except Exception as e:
            print(f"  [WARN] tuner reset failed to remove {self.save_path}: {type(e).__name__}: {e}")
