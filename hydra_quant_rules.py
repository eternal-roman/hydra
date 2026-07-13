#!/usr/bin/env python3
"""
HYDRA Quant Rules — deterministic Python enforcement layer (v2.14).

════════════════════════════════════════════════════════════════════════
HARD INVARIANT — SPOT-ONLY EXECUTION
════════════════════════════════════════════════════════════════════════
These rules act on SPOT trade sizing ONLY. They consume derivatives
data (funding, OI regime, basis) as signal input and CVD divergence
from the engine's candle-based proxy. No rule in this module authorizes
or suggests a futures, options, or margin order. If you ever add one,
that's a bug that violates the invariant in CLAUDE.md.
════════════════════════════════════════════════════════════════════════

What this module does:
  The Market Quant (LLM) produces a scenario and a size_multiplier
  suggestion. The Risk Manager (LLM) layers its own size_multiplier.
  But LLM-discretionary enforcement drifts — some situations are too
  pathological to leave to prose reasoning. This module encodes the
  non-negotiable guardrails in Python, fired on the indicator values
  themselves, not the LLM's interpretation of them.

The 9 rules (v2.14+; options-related R6/R9 deferred):
  R1: funding_bps_8h > 80 AND engine=BUY  → force_hold (crowded long top)
  R2: funding_bps_8h < -80 AND engine=BUY  → force_hold (bounce-chase into
      capitulation). Spot SELL is a long close — never force_held by R2.
  R3: oi_price_regime = short_squeeze AND engine=BUY → size *= 0.5
  R4: oi_price_regime = liquidation_cascade AND engine=SELL → size *= 0.5
  R5: basis_apr_pct > 40 AND engine=BUY → size *= 0.7 (euphoric contango)
  R7: |cvd_divergence_sigma| > 2 opposing engine direction → size *= 0.5
  R8: engine direction FADES positioning_bias (contrarian edge) → size *= 1.15
  R10: staleness_s > 300 on 2+ indicator fields → force_hold (fly blind)
  R11: QFE — force_hold active + engine=SELL + position in profit + no
       squeeze catalyst → force_exit (let the SELL through). Evaluated
       by the agent via evaluate_qfe() after apply_rules() returns.

Rule fires ARE multiplicative. force_hold from any rule takes
precedence over all size multipliers. Final multiplier clamped to
[0.0, 1.5] by the caller (agent size-stacking layer).

Usage:
    from hydra_quant_rules import apply_rules
    result = apply_rules(
        engine_action="BUY",
        quant_output={"positioning_bias": "crowded_short", "force_hold": False},
        quant_indicators={
            "funding_bps_8h": 45.0,
            "oi_price_regime": "short_squeeze",
            ...
        },
    )
    # result.size_multiplier ∈ [0.0, 1.5]
    # result.force_hold : bool
    # result.triggered : list of RuleFiring dicts
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Thresholds — tuned conservatively. These are the "undeniably
# pathological" bands, not the "slightly elevated" ones. Raising a
# threshold weakens the guardrail; lowering it gives more false
# positives. Change with evidence (A/B a week of live data).
FUNDING_EXTREME_BPS = 80.0
BASIS_EUPHORIC_APR_PCT = 40.0
CVD_DIVERGENCE_SIGMA_THRESHOLD = 2.0
STALENESS_SECONDS_MAX = 300.0
STALE_FIELDS_FOR_FORCE_HOLD = 2

# Rule effects
FORCE_HOLD_MULT = 0.0  # applied when force_hold fires; stack still valid
CONTRARIAN_BOOST = 1.15
SQUEEZE_PENALTY = 0.5
EUPHORIC_BASIS_PENALTY = 0.7
CVD_DIVERGENCE_PENALTY = 0.5

MULTIPLIER_CLAMP_MAX = 1.5
MULTIPLIER_CLAMP_MIN = 0.0

# R11 — Quant Force Exit (QFE): profit-capture gate override.
# When force_hold blocks a SELL on a profitable position, QFE lets the
# exit through if no squeeze catalyst contradicts it.  This is NOT a
# stop-loss — it only fires when the position is in profit.
# Minimum unrealized mark P&L % to trigger QFE. Raised to 1.0% (PR-A / A5)
# so profit exits clear ~0.42% round-trip maker friction with margin.
# Pre-v2.28 floor was 0.5% (could fire fee-flat after costs).
QFE_MIN_PROFIT_PCT = 1.0
# Documented fee drag for logs / fee-true reporting (not double-counted
# into the floor — the 1.0% floor already embeds ~0.42% RT + cushion).
QFE_FEE_DRAG_PCT = 0.42


@dataclass
class RuleFiring:
    """One rule that triggered. Structured so journal + dashboard can
    surface exactly which mandates acted on a trade."""
    rule_id: str            # "R1", "R2", ..., "R10"
    name: str               # human-readable short name
    effect: str             # "force_hold" | "size_mult"
    size_mult: float = 1.0  # 1.0 = no effect; <1 = penalty; >1 = boost
    reason: str = ""        # cites specific indicator values


@dataclass
class QfeResult:
    """Output of evaluate_qfe — the Quant Force Exit assessment.

    force_exit=True means the caller should let the SELL through despite
    an active force_hold.  trigger_values captures the exact indicator
    snapshot for post-mortem logging."""
    force_exit: bool = False
    force_exit_reason: str = ""
    trigger_values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleResult:
    """Aggregate output of apply_rules.

    size_multiplier is the product of all fired rule multipliers,
    clamped to [MULTIPLIER_CLAMP_MIN, MULTIPLIER_CLAMP_MAX]. If
    force_hold is True, the caller should treat the trade as HOLD
    regardless of size_multiplier (size_mult is reported for audit
    but ignored when force_hold is set)."""
    size_multiplier: float = 1.0
    force_hold: bool = False
    force_hold_reason: str = ""
    triggered: List[RuleFiring] = field(default_factory=list)


def apply_rules(
    engine_action: str,
    quant_output: Optional[Dict[str, Any]] = None,
    quant_indicators: Optional[Dict[str, Any]] = None,
) -> RuleResult:
    """Evaluate all 8 deterministic guardrails.

    Args:
        engine_action: raw engine signal direction — "BUY" | "SELL" | "HOLD".
        quant_output: the Quant's parsed JSON (for positioning_bias and
            R8 contrarian-edge detection). Can be None / partial.
        quant_indicators: the derivatives + CVD indicator block. Can be
            None / partial. None-valued fields count as stale for R10.

    Returns:
        RuleResult with aggregated size_multiplier, force_hold, and
        the full list of fired rules for journaling.
    """
    qo = quant_output or {}
    qi = quant_indicators or {}
    result = RuleResult()

    # R10 first — if we're flying blind, nothing else matters.
    stale_count = _count_stale_fields(qi)
    if stale_count >= STALE_FIELDS_FOR_FORCE_HOLD:
        result.force_hold = True
        result.force_hold_reason = (
            f"R10: {stale_count} quant indicators stale > "
            f"{STALENESS_SECONDS_MAX:.0f}s; no trade without data"
        )
        result.triggered.append(RuleFiring(
            rule_id="R10",
            name="data_staleness_blackout",
            effect="force_hold",
            size_mult=FORCE_HOLD_MULT,
            reason=result.force_hold_reason,
        ))
        # Still evaluate other rules for audit visibility, but their
        # size_mult contribution is irrelevant given force_hold.

    funding = qi.get("funding_bps_8h")
    oi_regime = qi.get("oi_price_regime")
    basis = qi.get("basis_apr_pct")
    cvd = qi.get("cvd_divergence_sigma")
    positioning = (qo.get("positioning_bias") or "").lower()

    # R1: funding > +80 bps + BUY → force_hold (buying crowded long top)
    if funding is not None and funding > FUNDING_EXTREME_BPS and engine_action == "BUY":
        reason = f"R1: funding {funding:.1f} bps/8h > {FUNDING_EXTREME_BPS:.0f}; buying into crowded long"
        result.triggered.append(RuleFiring(
            rule_id="R1", name="funding_extreme_long",
            effect="force_hold", size_mult=FORCE_HOLD_MULT, reason=reason,
        ))
        if not result.force_hold:
            result.force_hold = True
            result.force_hold_reason = reason

    # R2 (PR-A / A4): funding < -80 bps + BUY → force_hold (bounce-chase
    # into capitulation). Spot SELL closes a long — it is NOT opening a
    # short — so extreme negative funding must never trap long inventory.
    if funding is not None and funding < -FUNDING_EXTREME_BPS and engine_action == "BUY":
        reason = (
            f"R2: funding {funding:.1f} bps/8h < -{FUNDING_EXTREME_BPS:.0f}; "
            f"buying into capitulation bounce"
        )
        result.triggered.append(RuleFiring(
            rule_id="R2", name="funding_extreme_capitulation_buy",
            effect="force_hold", size_mult=FORCE_HOLD_MULT, reason=reason,
        ))
        if not result.force_hold:
            result.force_hold = True
            result.force_hold_reason = reason

    # R3: short_squeeze regime + BUY → halve size (chasing unstable up)
    if oi_regime == "short_squeeze" and engine_action == "BUY":
        _apply_size_rule(
            result, "R3", "short_squeeze_chase",
            SQUEEZE_PENALTY,
            f"R3: OI regime short_squeeze; chasing unstable upside",
        )

    # R4: liquidation_cascade + SELL → halve size
    if oi_regime == "liquidation_cascade" and engine_action == "SELL":
        _apply_size_rule(
            result, "R4", "liquidation_cascade_chase",
            SQUEEZE_PENALTY,
            f"R4: OI regime liquidation_cascade; selling into washout",
        )

    # R5: euphoric contango + BUY → size × 0.7
    if basis is not None and basis > BASIS_EUPHORIC_APR_PCT and engine_action == "BUY":
        _apply_size_rule(
            result, "R5", "euphoric_basis",
            EUPHORIC_BASIS_PENALTY,
            f"R5: basis {basis:.1f}% APR > {BASIS_EUPHORIC_APR_PCT:.0f}; euphoric contango",
        )

    # R7: CVD divergence opposing engine direction → half size
    if cvd is not None and abs(cvd) > CVD_DIVERGENCE_SIGMA_THRESHOLD:
        opposing = (
            (cvd < 0 and engine_action == "BUY")
            or (cvd > 0 and engine_action == "SELL")
        )
        if opposing:
            _apply_size_rule(
                result, "R7", "cvd_divergence_opposing",
                CVD_DIVERGENCE_PENALTY,
                f"R7: CVD divergence {cvd:+.2f}σ opposes {engine_action} direction",
            )

    # R8: engine fades crowded positioning → small boost (contrarian edge)
    if positioning in ("crowded_long", "crowded_short"):
        fades = (
            (positioning == "crowded_long" and engine_action == "SELL")
            or (positioning == "crowded_short" and engine_action == "BUY")
        )
        if fades:
            _apply_size_rule(
                result, "R8", "contrarian_edge",
                CONTRARIAN_BOOST,
                f"R8: engine {engine_action} fades {positioning} positioning",
            )

    # Clamp final stack
    result.size_multiplier = max(
        MULTIPLIER_CLAMP_MIN,
        min(MULTIPLIER_CLAMP_MAX, result.size_multiplier),
    )
    return result


def evaluate_qfe(
    position_size: float,
    unrealized_pnl_pct: float,
    quant_indicators: Optional[Dict[str, Any]] = None,
    positioning_bias: str = "",
) -> QfeResult:
    """R11 — Quant Force Exit: should a profitable SELL bypass force_hold?

    Called by the agent AFTER apply_rules() when force_hold would block
    a SELL on an open position.  QFE is exit-only, profit-only, and
    squeeze-filtered.

    Preconditions (caller must verify):
      - The original engine signal was SELL.
      - force_hold is active (from rules or brain).

    Args:
        position_size: current position size (>0 means open long).
        unrealized_pnl_pct: (current_price - avg_entry) / avg_entry * 100.
        quant_indicators: derivatives + CVD indicator block (same as
            passed to apply_rules). None-safe.
        positioning_bias: Quant's positioning assessment
            ("crowded_short" | "crowded_long" | "balanced" | "unknown").

    Returns:
        QfeResult with force_exit=True if the SELL should proceed.
    """
    qi = quant_indicators or {}
    bias = (positioning_bias or "").lower()

    if position_size <= 0:
        return QfeResult()

    # Fee-aware floor (PR-A / A5): 1.0% raw mark floor embeds RT friction
    # cushion (~0.42% maker RT). Below this, "profit" may be fee-flat.
    if unrealized_pnl_pct < QFE_MIN_PROFIT_PCT:
        return QfeResult()
    fee_true_pnl = unrealized_pnl_pct - QFE_FEE_DRAG_PCT

    # Squeeze catalyst filter — deterministic only. LLM positioning_bias
    # alone must not hard-block QFE (audit: sticky "crowded_short" trapped
    # winners). Bias may still appear in the snapshot for post-mortems.
    oi_regime = qi.get("oi_price_regime", "")
    funding = qi.get("funding_bps_8h")
    cvd = qi.get("cvd_divergence_sigma")

    # 1. Squeeze already in progress (OI falling + price rising)
    if oi_regime == "short_squeeze":
        return QfeResult(
            trigger_values=_qfe_snapshot(qi, unrealized_pnl_pct, bias),
        )

    # 2. Extreme short funding + accumulation flow = squeeze setup
    if (funding is not None and funding < -FUNDING_EXTREME_BPS
            and cvd is not None and cvd > CVD_DIVERGENCE_SIGMA_THRESHOLD):
        return QfeResult(
            trigger_values=_qfe_snapshot(qi, unrealized_pnl_pct, bias),
        )

    triggers = _qfe_snapshot(qi, unrealized_pnl_pct, bias)
    reason = (
        f"R11/QFE: profit {unrealized_pnl_pct:+.2f}% "
        f"(fee-true ~{fee_true_pnl:+.2f}% after {QFE_FEE_DRAG_PCT:.2f}% drag) ≥ "
        f"{QFE_MIN_PROFIT_PCT:.1f}% floor, no deterministic squeeze; "
        f"releasing SELL through force_hold"
    )
    return QfeResult(
        force_exit=True,
        force_exit_reason=reason,
        trigger_values=triggers,
    )


def _qfe_snapshot(
    qi: Dict[str, Any], pnl_pct: float, bias: str,
) -> Dict[str, Any]:
    """Capture indicator state at QFE evaluation for post-mortem logging."""
    return {
        "unrealized_pnl_pct": round(pnl_pct, 4),
        "funding_bps_8h": qi.get("funding_bps_8h"),
        "oi_price_regime": qi.get("oi_price_regime"),
        "cvd_divergence_sigma": qi.get("cvd_divergence_sigma"),
        "basis_apr_pct": qi.get("basis_apr_pct"),
        "positioning_bias": bias,
    }


def _apply_size_rule(
    result: RuleResult, rule_id: str, name: str,
    mult: float, reason: str,
) -> None:
    """Stack a multiplicative size rule onto the result and record it."""
    result.size_multiplier *= mult
    result.triggered.append(RuleFiring(
        rule_id=rule_id,
        name=name,
        effect="size_mult",
        size_mult=mult,
        reason=reason,
    ))


def _count_stale_fields(qi: Dict[str, Any]) -> int:
    """Count how many of the tracked indicator fields are stale.

    A field counts as stale when it's None in the indicators dict.
    Callers (DerivativesStream, CVD) null-out fields whose freshness
    exceeds STALENESS_SECONDS_MAX — or, explicitly, callers can set
    any field to None before passing in. This module doesn't poll
    timestamps itself (that's the stream's job).

    We also honor an optional aggregate field `staleness_s`: if it
    exceeds STALENESS_SECONDS_MAX, treat the whole block as stale
    (counts as 5 stale fields — above the threshold regardless).

    Synthetic-pair awareness: if `synthetic_pair=True`, the OI- and
    basis-derived fields are None by construction (no direct perp
    exists, e.g. SOL/BTC). They are NOT stale — they are unavailable
    by design. Skip them; R10 only watches the fields the synthetic
    path actually populates (funding + cvd + regime).

    Uncovered-pair awareness: if `derivatives_covered=False`, the pair
    has no Kraken Futures mapping at all (portfolio satellites like
    NIGHT/USD). Every derivatives field is unavailable by design, so
    R10 tracks only the engine-internal CVD signal — otherwise every
    satellite would be structurally force-held on every tick.
    """
    aggregate = qi.get("staleness_s")
    try:
        if aggregate is not None and float(aggregate) > STALENESS_SECONDS_MAX:
            return 5  # treat entire block as stale
    except (TypeError, ValueError) as e:
        import logging; logging.warning(f"Ignored exception: {e}")

    if qi.get("derivatives_covered") is False:
        tracked = ("cvd_divergence_sigma",)
    elif qi.get("synthetic_pair"):
        tracked = ("funding_bps_8h", "cvd_divergence_sigma", "oi_price_regime")
    else:
        tracked = ("funding_bps_8h", "oi_delta_1h_pct", "oi_price_regime",
                   "basis_apr_pct", "cvd_divergence_sigma")
    return sum(1 for k in tracked if qi.get(k) is None)
