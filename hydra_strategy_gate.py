"""Evidence-gated strategy selection: which pairs trade the daily trend sleeve.

HYDRA_TREND_SLEEVE
  1 / true / yes / on    every pair runs the sleeve (operator override)
  0 / false / no / off   no pair runs it
  unset, empty or auto   a pair runs it only when the pre-registered gate
                         passed on the operator's own data for that asset
  anything else          no pair runs it, and the reason says why (a typo
                         must never switch the sleeve on)

The gate is ``tools/trend_sleeve_gate.py --engine`` (registration:
``research/data/trend_sleeve_REGISTRATION.md``). Its report must:

  * carry the overall verdict PASS (``PASS_DAILY_ONLY`` means the engine
    arms did not run, which the registration says is not enough) and the
    ``significant`` flag (a PASS without it is the operator's call);
  * have been built for the rules the engine runs now: re-size period and
    band, long threshold, the competition position cap and the vol target
    (``HYDRA_TREND_TARGET_VOL``);
  * be recent: both the run and the last day of data it tested no older
    than ``HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS`` (default 180), so a run
    on a store whose refresh failed does not count as fresh evidence;
  * show, for every stable-quoted entry of the pair's base asset, an asset
    verdict PASS and an engine check that is complete, beat the current
    engine, and tracked the daily simulation (fidelity).

Only stable-quoted pairs can run it: BTC/USDC uses BTC/USD's result
because the gate measures the BTC price in dollars, but ETH/BTC or BTC/EUR
is a different series the gate never tested.

No file, a malformed file, or any failed condition leaves the pair on the
current engine. Nothing here places orders or touches state.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_GATE_PATH = os.path.join("research", "data", "trend_sleeve_gate.json")
DEFAULT_MAX_AGE_DAYS = 180.0
# The agent reads this file at import (environment wins); the CLI below
# must see the same settings or it reports a different strategy.
DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")
_AUTO = ("", "auto")


@dataclass(frozen=True)
class SleeveDecision:
    enabled: bool
    reason: str
    gate_generated_at: Optional[str] = None


def sleeve_mode(raw: Optional[str]) -> str:
    """'on', 'off', 'auto' (unset, empty or auto) or 'invalid'."""
    text = (raw or "").strip().lower()
    if text in _ON:
        return "on"
    if text in _OFF:
        return "off"
    if text in _AUTO:
        return "auto"
    return "invalid"


def _asset(name: str) -> str:
    name = str(name or "").strip().upper()
    try:
        from hydra_pair_registry import normalize_asset
        return normalize_asset(name) or name
    except Exception:
        return name


def _split(pair: str) -> Tuple[str, str]:
    base, _, quote = str(pair or "").partition("/")
    return _asset(base), _asset(quote)


def _base(pair: str) -> str:
    return _split(pair)[0]


def _stable_quoted(pair: str) -> bool:
    try:
        from hydra_pair_registry import STABLE_QUOTES
    except Exception:
        STABLE_QUOTES = frozenset({"USD", "USDC", "USDT"})
    return _split(pair)[1] in STABLE_QUOTES


def _number(value) -> Optional[float]:
    """A finite JSON number; never a bool, NaN or infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _age_days(generated_at: Optional[str], now: float) -> Optional[float]:
    try:
        ts = calendar.timegm(time.strptime(str(generated_at), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return (now - ts) / 86400.0


def _max_age_days(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get("HYDRA_TREND_SLEEVE_GATE_MAX_AGE_DAYS") or DEFAULT_MAX_AGE_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS
    return value if math.isfinite(value) and value > 0 else DEFAULT_MAX_AGE_DAYS


def _read_dotenv(path: str) -> Dict[str, str]:
    """KEY=VALUE pairs from ``path``, parsed the way hydra_agent loads .env:
    comments and blank values skipped, one level of matching quotes stripped."""
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value:
            values.setdefault(key.strip(), value)
    return values


def load_gate_report(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def _asset_problem(name: str, label: str, asset, now: float, max_age: float) -> Optional[str]:
    """Why one report entry does not license the sleeve, or None."""
    if not isinstance(asset, dict):
        return f"{label} gate result malformed"
    if asset.get("verdict") != "PASS":
        return f"{label} gate {asset.get('verdict')}"
    check = asset.get("engine_check")
    check = check if isinstance(check, dict) else {}
    failed = [k for k in ("complete", "G_beats_F", "fidelity") if check.get(k) is not True]
    if failed:
        return f"{label} engine check failed: {', '.join(failed)}"
    window = asset.get("window")
    end_ts = _number(window.get("end_ts")) if isinstance(window, dict) else None
    if end_ts is None:
        return f"{label} gate result has no data end date"
    data_age = (now - end_ts) / 86400.0
    if data_age > max_age:
        return (f"{label} gate data ends {data_age:.0f} days ago (limit {max_age:.0f}): "
                "refresh the history and re-run run_strategy_gate.bat")
    return None


def resolve_trend_sleeve(
    pairs: Iterable[str],
    env: Optional[Mapping[str, str]] = None,
    report: Optional[dict] = None,
    gate_path: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, SleeveDecision]:
    """Decide, per pair, whether the engine runs the trend sleeve."""
    from hydra_engine import HydraEngine, SIZING_COMPETITION, trend_target_vol_setting

    env = os.environ if env is None else env
    pairs = list(pairs)
    raw_mode = env.get("HYDRA_TREND_SLEEVE")
    mode = sleeve_mode(raw_mode)
    if mode == "on":
        return {p: SleeveDecision(True, "HYDRA_TREND_SLEEVE=1 (operator override)")
                for p in pairs}
    if mode == "off":
        return {p: SleeveDecision(False, "HYDRA_TREND_SLEEVE=0") for p in pairs}
    if mode == "invalid":
        return {p: SleeveDecision(False, f"HYDRA_TREND_SLEEVE={raw_mode!r} is not "
                                         "auto, 1 or 0, so the sleeve stays off")
                for p in pairs}

    path = gate_path or env.get("HYDRA_TREND_SLEEVE_GATE") or DEFAULT_GATE_PATH
    if report is None:
        report = load_gate_report(path)
    if report is None:
        return {p: SleeveDecision(False, f"no gate result at {path}: run "
                                         "run_strategy_gate.bat, then restart")
                for p in pairs}

    generated = report.get("generated_at")
    generated = generated if isinstance(generated, str) else None

    def off(reason: str) -> SleeveDecision:
        return SleeveDecision(False, reason, generated)

    verdict = report.get("verdict")
    if verdict == "INSUFFICIENT_DATA":
        return {p: off("gate verdict INSUFFICIENT_DATA: it needs 5+ years of daily "
                       "history per asset (build it once with python -m "
                       "tools.bootstrap_history --zip <Kraken trade archive>)")
                for p in pairs}
    if verdict != "PASS":
        return {p: off(f"gate verdict {verdict} (needs PASS with the engine arms)")
                for p in pairs}
    if report.get("significant") is not True:
        # The registration: a PASS without significance is the operator's
        # call (at reduced capital), not a decision the evidence makes.
        return {p: off("gate PASS but not significant: operator's call "
                       "(HYDRA_TREND_SLEEVE=1 to run it)") for p in pairs}

    now = time.time() if now is None else now
    max_age = _max_age_days(env)
    age = _age_days(generated, now)
    if age is None or age < -1.0 or age > max_age:
        shown = "unknown" if age is None else f"{age:.0f} days"
        return {p: off(f"gate result too old or undated ({shown}; limit {max_age:.0f})")
                for p in pairs}

    params = report.get("params")
    if not isinstance(params, dict):
        return {p: off("gate result has no params") for p in pairs}
    # The rules the sleeve trades with now. A conservative-mode engine caps
    # below the competition cap the gate tests, which is less exposure.
    expected = {"resize_days": HydraEngine.SLEEVE_RESIZE_DAYS,
                "resize_tol": HydraEngine.SLEEVE_RESIZE_TOL,
                "long_at": HydraEngine.TREND_SCORE_LONG,
                "cap": SIZING_COMPETITION["max_position_pct"],
                "target_vol": trend_target_vol_setting(env)}
    for key, value in expected.items():
        got = _number(params.get(key))
        if got is None or abs(got - float(value)) > 1e-9:
            return {p: off(f"gate built for other rules ({key}={params.get(key)!r}, "
                           f"engine {value!r}): re-run run_strategy_gate.bat")
                    for p in pairs}

    assets = report.get("assets")
    if not isinstance(assets, dict):
        return {p: off("gate result has no per-asset results") for p in pairs}
    by_base: Dict[str, List[Tuple[str, object]]] = {}
    for name, asset in assets.items():
        if _stable_quoted(name):
            by_base.setdefault(_base(name), []).append((str(name), asset))

    out: Dict[str, SleeveDecision] = {}
    for pair in pairs:
        base = _base(pair)
        if not _stable_quoted(pair):
            out[pair] = off(f"{pair} is not quoted in a stable coin; the gate "
                            f"tested {base} in dollars")
            continue
        entries = by_base.get(base)
        if not entries:
            out[pair] = off(f"{base} not in the gate result")
            continue
        problem = None
        for name, asset in entries:
            label = base if len(entries) == 1 else name
            problem = _asset_problem(name, label, asset, now, max_age)
            if problem:
                break
        out[pair] = off(problem) if problem else SleeveDecision(
            True, f"{base} gate PASS ({generated})", generated)
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
    env = _read_dotenv(DOTENV_PATH)
    env.update(os.environ)
    raw = env.get("HYDRA_TREND_SLEEVE")
    if raw:
        source = "environment" if "HYDRA_TREND_SLEEVE" in os.environ else ".env"
        print(f"HYDRA_TREND_SLEEVE={raw} (from {source}) -> {sleeve_mode(raw)}")
    else:
        print("HYDRA_TREND_SLEEVE unset -> auto (each pair follows the gate)")
    for pair, decision in resolve_trend_sleeve(pairs, env=env, gate_path=args.gate).items():
        label = "trend sleeve" if decision.enabled else "1h rails engine"
        print(f"{pair:<10} {label:<16} {decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
