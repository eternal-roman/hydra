"""Export the frozen S3 model + golden parity fixtures for s3bounce/.

Runs the RESEARCH pipeline (candles_from_sqlite -> causal_setups ->
build_features -> 2026-fold scoring) on the last WINDOW daily bars per
asset and writes, per asset, s3bounce/tests/fixtures/parity_<A>_USD.json:

  { "bars": [...],            # the daily bars (research resample)
    "hourly_sample": [...],   # last HOURLY_DAYS days of raw 1h rows
    "setups": [...],          # low_idx/low_px/atr/bounce_idx/label (+x, resolve_ts)
    "scores": {setup_key: p}  # BTC/ETH only (artifact assets)
  }

It also cross-checks s3bounce/s3bounce/model_artifact.json against the
promoted bakeoff JSON (final_fold_model + 2026 thr_p75) and FAILS on any
mismatch — this tool is the future yearly-refit path, so drift between
the artifact and the evidence is a hard error.

Usage (from heartbeat/): PYTHONPATH=src python tools/export_s3_model.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.engine.posterior import sigmoid          # noqa: E402

import paper_bounce_sim as sim                          # noqa: E402
from bounce_geometry_study import candles_from_sqlite   # noqa: E402
from bakeoff_s3_daily_classifier import (               # noqa: E402
    DB, ASSETS, FEATURES, shock_flags, fresh_low_days, build_features)

WINDOW = 400
HOURLY_DAYS = 30
FIXTURE_DIR = HYDRA_ROOT / "s3bounce" / "tests" / "fixtures"
ARTIFACT = HYDRA_ROOT / "s3bounce" / "s3bounce" / "model_artifact.json"
EVIDENCE = HEARTBEAT_ROOT / "evidence" / "bakeoffs" / "s3_daily_classifier.json"


def bar_dict(c) -> dict:
    return {"ts": int(c.open_ts), "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume}


def hourly_rows(pair: str, lo_ts: int) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM ohlc "
        "WHERE pair=? AND grain_sec=3600 AND ts>=? ORDER BY ts",
        (pair, lo_ts)).fetchall()
    con.close()
    return [{"ts": int(t), "open": o, "high": h, "low": l, "close": c,
             "volume": v} for t, o, h, l, c, v in rows]


def verify_artifact() -> dict:
    art = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    errors = []
    for asset, m in art["models"].items():
        ffm = ev["assets"][asset]["final_fold_model"]
        thr = [f for f in ev["assets"][asset]["folds"]
               if f.get("year") == ffm["year"]][0]["thr_p75"]
        checks = [("intercept", m["intercept"], ffm["intercept"]),
                  ("threshold", m["threshold"], thr)]
        for feat in FEATURES:
            checks += [(f"w.{feat}", m["weights_std_space"][feat],
                        ffm["weights_std_space"][feat]),
                       (f"mu.{feat}", m["feature_means"][feat],
                        ffm["feature_means"][feat]),
                       (f"sd.{feat}", m["feature_stds"][feat],
                        ffm["feature_stds"][feat])]
        for name, a, b in checks:
            if abs(a - b) > 1e-9:
                errors.append(f"{asset} {name}: artifact {a} != evidence {b}")
    if errors:
        raise SystemExit("ARTIFACT MISMATCH:\n  " + "\n  ".join(errors))
    return art


def main() -> int:
    art = verify_artifact()
    print("model_artifact.json matches promoted evidence (all assets)")

    # research daily bars per asset, sliced to the window
    daily = {p: candles_from_sqlite(DB, p, 24)[-WINDOW:] for p in ASSETS}
    low_days = {p: fresh_low_days(c) for p, c in daily.items()}

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for pair in ASSETS:
        candles = daily[pair]
        setups = sim.causal_setups(candles, {})
        flags = shock_flags(candles)
        build_features(candles, setups, flags, low_days)
        scores = {}
        if pair in art["models"]:
            m = art["models"][pair]
            for s in setups:
                z = m["intercept"]
                for f in FEATURES:
                    z += m["weights_std_space"][f] * \
                        (s["x"][f] - m["feature_means"][f]) / m["feature_stds"][f]
                scores[f"{s['low_idx']}@{s['low_px']:.10g}"] = sigmoid(z)
        fixture = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pair": pair, "window": WINDOW,
            "bars": [bar_dict(c) for c in candles],
            "hourly_sample": hourly_rows(
                pair, int(candles[-HOURLY_DAYS].open_ts)),
            "setups": [{"low_idx": s["low_idx"], "low_px": s["low_px"],
                        "atr": s["atr"], "bounce_idx": s["bounce_idx"],
                        "label": s["label"], "x": s["x"],
                        "resolve_ts": s["resolve_ts"]} for s in setups],
            "scores": scores}
        out = FIXTURE_DIR / f"parity_{pair.replace('/', '_')}.json"
        out.write_text(json.dumps(fixture, indent=1))
        print(f"{pair}: {len(candles)} bars, {len(setups)} setups, "
              f"{len(scores)} scored -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
