"""Evidence-gated strategy selection: which pairs trade the daily trend sleeve.

HYDRA_TREND_SLEEVE
  1 / true / yes / on    every pair runs the sleeve (operator override)
  0 / false / no / off   no pair runs it
  unset or "auto"        a pair runs it only when the pre-registered gate
                         passed on the operator's own data for that asset

The gate is ``tools/trend_sleeve_gate.py --engine`` (registration:
``research/data/trend_sleeve_REGISTRATION.md``). Its report must:

  * carry the overall verdict PASS (``PASS_DAILY_ONLY`` means the engine
    arms did not run, which the registration says is not enough) and the
    ``significant`` flag (a PASS without it is the operator's call);
  * have been built for the rules the engine runs now (re-size period and
    band, long threshold);
  * be no older than ``HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS`` (default 180);
  * show, for the pair's base asset, an asset verdict PASS and an engine
    check that is complete, beat the current engine, and tracked the
    daily simulation (fidelity).

A pair quoted in another stable coin (BTC/USDC) uses its base asset's
result: the gate measures the BTC price, not the quote.

No file, a malformed file, or any failed condition leaves the pair on the
current engine. Nothing here places orders or touches state.
"""
from __future__ import annotations

import calendar
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional

DEFAULT_GATE_PATH = os.path.join("research", "data", "trend_sleeve_gate.json")
DEFAULT_MAX_AGE_DAYS = 180.0
_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class SleeveDecision:
    enabled: bool
    reason: str
    gate_generated_at: Optional[str] = None


def sleeve_mode(raw: Optional[str]) -> str:
    """'on', 'off' or 'auto' (unset, empty and unknown values are auto)."""
    text = (raw or "").strip().lower()
    if text in _ON:
        return "on"
    if text in _OFF:
        return "off"
    return "auto"


def _base(pair: str) -> str:
    base = str(pair or "").split("/")[0].upper()
    try:
        from hydra_pair_registry import normalize_asset
        return normalize_asset(base) or base
    except Exception:
        return base


def _age_days(generated_at: Optional[str], now: float) -> Optional[float]:
    try:
        ts = calendar.timegm(time.strptime(str(generated_at), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return (now - ts) / 86400.0


def load_gate_report(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def resolve_trend_sleeve(
    pairs: Iterable[str],
    env: Optional[Mapping[str, str]] = None,
    report: Optional[dict] = None,
    gate_path: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, SleeveDecision]:
    """Decide, per pair, whether the engine runs the trend sleeve."""
    from hydra_engine import HydraEngine

    env = os.environ if env is None else env
    pairs = list(pairs)
    mode = sleeve_mode(env.get("HYDRA_TREND_SLEEVE"))
    if mode == "on":
        return {p: SleeveDecision(True, "HYDRA_TREND_SLEEVE=1 (operator override)")
                for p in pairs}
    if mode == "off":
        return {p: SleeveDecision(False, "HYDRA_TREND_SLEEVE=0") for p in pairs}

    path = gate_path or env.get("HYDRA_TREND_SLEEVE_GATE") or DEFAULT_GATE_PATH
    if report is None:
        report = load_gate_report(path)
    if report is None:
        return {p: SleeveDecision(False, f"no gate result at {path}") for p in pairs}

    generated = report.get("generated_at")

    def off(reason: str) -> SleeveDecision:
        return SleeveDecision(False, reason, generated)

    verdict = report.get("verdict")
    if verdict != "PASS":
        return {p: off(f"gate verdict {verdict} (needs PASS with the engine arms)")
                for p in pairs}
    if report.get("significant") is not True:
        # The registration: a PASS without significance is the operator's
        # call (at reduced capital), not a decision the evidence makes.
        return {p: off("gate PASS but not significant: operator's call "
                       "(HYDRA_TREND_SLEEVE=1 to run it)") for p in pairs}

    try:
        max_age = float(env.get("HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS") or DEFAULT_MAX_AGE_DAYS)
    except ValueError:
        max_age = DEFAULT_MAX_AGE_DAYS
    age = _age_days(generated, time.time() if now is None else now)
    if age is None or age < -1.0 or age > max_age:
        shown = "unknown" if age is None else f"{age:.0f} days"
        return {p: off(f"gate result too old or undated ({shown}; limit {max_age:.0f})")
                for p in pairs}

    params = report.get("params") or {}
    expected = {"resize_days": HydraEngine.SLEEVE_RESIZE_DAYS,
                "resize_tol": HydraEngine.SLEEVE_RESIZE_TOL,
                "long_at": HydraEngine.TREND_SCORE_LONG}
    for key, value in expected.items():
        got = params.get(key)
        if not isinstance(got, (int, float)) or abs(float(got) - float(value)) > 1e-12:
            return {p: off(f"gate built for other rules ({key}={got!r}, engine {value!r})")
                    for p in pairs}

    by_base: Dict[str, dict] = {}
    for name, asset in (report.get("assets") or {}).items():
        if isinstance(asset, dict):
            by_base.setdefault(_base(name), asset)

    out: Dict[str, SleeveDecision] = {}
    for pair in pairs:
        asset = by_base.get(_base(pair))
        if asset is None:
            out[pair] = off(f"{_base(pair)} not in the gate result")
            continue
        if asset.get("verdict") != "PASS":
            out[pair] = off(f"{_base(pair)} gate {asset.get('verdict')}")
            continue
        check = asset.get("engine_check") or {}
        failed = [k for k in ("complete", "G_beats_F", "fidelity") if check.get(k) is not True]
        if failed:
            out[pair] = off(f"{_base(pair)} engine check failed: {', '.join(failed)}")
            continue
        out[pair] = SleeveDecision(True, f"{_base(pair)} gate PASS ({generated})", generated)
    return out


def main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Show which pairs Hydra will trade "
                                             "with the daily trend sleeve, and why.")
    ap.add_argument("--pairs", default="BTC/USD,ETH/USD,ZEC/USD")
    ap.add_argument("--gate", default=None, help="gate report path (default: "
                    "HYDRA_TREND_SLEEVE_GATE or research/data/trend_sleeve_gate.json)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    for pair, decision in resolve_trend_sleeve(pairs, gate_path=args.gate).items():
        label = "trend sleeve" if decision.enabled else "1h rails engine"
        print(f"{pair:<10} {label:<16} {decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
