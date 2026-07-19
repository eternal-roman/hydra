"""Per-asset evaluation reports (markdown + JSON sidecar).

Includes: event count, class balance, AUC/Brier/separation per
checkpoint, calibration curve, lead-time, and the 5 worst-classified
events with pointers to their tape ranges for post-mortem.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Sequence

from .labeler import BounceEvent
from .metrics import checkpoint_table


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%d %H:%M")


def worst_events(events: Sequence[BounceEvent], cp: str = "bounce+3",
                 n: int = 5) -> list[dict]:
    """Events where the posterior was most confidently WRONG at `cp`."""
    scored = []
    for e in events:
        p = e.p_at.get(cp)
        if p is None:
            continue
        err = (1.0 - p) if e.label == "reversal" else p
        scored.append((err, e, p))
    scored.sort(key=lambda t: (-t[0], t[1].low_ts))
    return [{
        "low_ts": _iso(e.low_ts), "low_ts_epoch": e.low_ts,
        "label": e.label, "p_up_at_cp": round(p, 4),
        "error": round(err, 4), "low_price": e.low_price,
        "atr": round(e.atr, 6), "tainted": e.tainted,
        "tape_hint": f"candles [{e.low_idx}..{e.resolve_idx}]",
    } for err, e, p in scored[:n]]


def build_report(pair: str, tf: str, events: Sequence[BounceEvent],
                 config: dict, extra: dict | None = None) -> dict:
    checkpoints = [f"bounce+{k}" for k in
                   config.get("eval", {}).get("checkpoints", [1, 2, 3])]
    checkpoints.append("progress_2atr")
    auc_promote = float(config.get("eval", {}).get("auc_promote", 0.70))
    clean = [e for e in events if not e.tainted]
    table = checkpoint_table(clean, checkpoints, auc_promote)
    report = {
        "pair": pair, "tf": tf,
        "events_total": len(events),
        "events_tainted_excluded": len(events) - len(clean),
        "metrics": table,
        "min_events_required": int(config.get("labeler", {}).get("min_events", 60)),
        "sufficient_events": table["n_events"] >= int(
            config.get("labeler", {}).get("min_events", 60)),
        "promote_criterion": f"AUC >= {auc_promote} by bounce+3, walk-forward",
        "worst_events": worst_events(clean),
    }
    if extra:
        report.update(extra)
    return report


def render_markdown(report: dict) -> str:
    m = report["metrics"]
    lines = [
        f"# heartbeat eval — {report['pair']} {report['tf']}",
        "",
        f"- events: **{m['n_events']}** (reversal {m['n_reversal']} / "
        f"fake {m['n_fake']}; {report['events_tainted_excluded']} tainted excluded)",
        f"- sufficient events (>= {report['min_events_required']}): "
        f"**{report['sufficient_events']}**",
        f"- promote criterion: {report['promote_criterion']}",
        f"- lead-time (earliest checkpoint AUC >= 0.70): "
        f"**{m['lead_time'] or 'not reached'}**",
        "",
        "| checkpoint | n | AUC | Brier | separation |",
        "|---|---|---|---|---|",
    ]
    for cp, r in m["checkpoints"].items():
        lines.append(f"| {cp} | {r['n']} | {r['auc']} | {r['brier']} | "
                     f"{r['separation']} |")
    lines += ["", "## Calibration (bounce+3)", "",
              "| bin | n | mean pred | obs freq |", "|---|---|---|---|"]
    for b in m["checkpoints"].get("bounce+3", {}).get("calibration", []):
        lines.append(f"| {b['bin']} | {b['n']} | {b['mean_pred']} | {b['obs_freq']} |")
    lines += ["", "## 5 worst-classified events (bounce+3)", ""]
    if report["worst_events"]:
        lines += ["| low ts | label | P(up) | error | tape |", "|---|---|---|---|---|"]
        for w in report["worst_events"]:
            lines.append(f"| {w['low_ts']} | {w['label']} | {w['p_up_at_cp']} | "
                         f"{w['error']} | {w['tape_hint']} |")
    else:
        lines.append("(none)")
    if "walk_forward" in report:
        lines += ["", "## Walk-forward folds", "",
                  "| fold | train range | test range | test n | AUC bounce+3 |",
                  "|---|---|---|---|---|"]
        for f in report["walk_forward"]:
            lines.append(f"| {f['fold']} | {f['train_range']} | {f['test_range']} "
                         f"| {f['test_n']} | {f['auc_bounce3']} |")
    return "\n".join(lines) + "\n"


def write_report(report: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"eval_{report['pair'].replace('/', '_')}_{report['tf']}"
    jpath = out / f"{stem}.json"
    mpath = out / f"{stem}.md"
    jpath.write_text(json.dumps(report, indent=2, default=str),
                     encoding="utf-8")
    mpath.write_text(render_markdown(report), encoding="utf-8")
    return mpath, jpath
