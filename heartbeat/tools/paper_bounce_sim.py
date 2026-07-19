"""Paper bounce-trade simulator: does the heartbeat posterior convert
into paper P&L when it both confirms the ENTRY and predicts the EXIT?

Everything the trader sees is causal:
  * setup: the labeler's bounce geometry re-derived point-in-time —
    swing low L0 (confirmed 2 candles later), established down-leg
    (>=2 lower swing lows below MA9, all past-only), crash exclusion,
    then the bounce trigger high >= L0 + 1.0*ATR;
  * entry decision at the close of bounce+3 (the calibrated checkpoint):
    paper BUY at that close iff calibrated P(up) >= entry threshold.
    Setups whose low is undercut before bounce+3 die untraded;
  * exit, first hit per candle (stop checked before target — the
    conservative ordering when both print in one candle):
      stop   low < L0            -> fill min(close, L0)
      target high >= L0+3.3*ATR  -> fill L0+3.3*ATR   (variant B only)
      flow   P(up) < exit thr    -> fill close        (heartbeat exit)
      time   horizon exceeded    -> fill close
  * one position per pair at a time; fees 26 bps taker per side
    (conservative vs HYDRA's 16 bps maker).

Honesty controls, all reported with equal prominence:
  * walk-forward: weights AND thresholds fit only on events resolved
    before the time split; the OOS segment is the verdict;
  * entry arms: ALL setups (raw edge, no heartbeat), gated at the
    P25/P50/P75 of TRAIN event posteriors, and INVERSE-gated at P50 —
    if inverse matches gated, the posterior adds nothing;
  * exit variants: A = stop+flow+time (heartbeat-predicted exit),
    B = A + 3.3*ATR label-mirror target;
  * buy-and-hold over the same segment printed alongside.

Usage (from heartbeat/):
    PYTHONPATH=src python tools/paper_bounce_sim.py --pairs BTC/USD,ETH/USD
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HEARTBEAT_ROOT = Path(__file__).resolve().parents[1]
HYDRA_ROOT = HEARTBEAT_ROOT.parent
sys.path.insert(0, str(HEARTBEAT_ROOT / "src"))
sys.path.insert(0, str(HEARTBEAT_ROOT / "tools"))

from heartbeat.config import load_config              # noqa: E402
from heartbeat.eval.labeler import _ma, _swing_lows   # noqa: E402
from heartbeat.features.tier0 import robust_atr       # noqa: E402

from hydra_bakeoff import replay_pair, train_weights_and_series  # noqa: E402

FEE = 0.0026          # taker per side (override with --fee-bps)
BOUNCE_ATR = 1.0
TARGET_ATR = 3.3
CRASH_ATR = 3.0
HORIZON = 200
SW = 2
MA_P = 9
LOOKBACK = 30


def causal_setups(candles, config) -> list[dict]:
    """Bounce setups exactly as the labeler frames them, but keeping only
    quantities known by the entry checkpoint. Returns dicts with
    {low_idx, low_px, atr, bounce_idx, entry_idx} (entry_idx = bounce+3),
    entry candidates only (low not undercut through entry_idx)."""
    closes = [c.close for c in candles]
    swings = _swing_lows(candles, SW)
    out = []
    for i in swings:
        atr = robust_atr(candles[:i], 14, 3.0)
        if atr is None or atr <= 0:
            continue
        prior = [j for j in swings if i - LOOKBACK <= j < i]
        idx_seq = prior + [i]
        lower = 0
        for a, b in zip(idx_seq[:-1], idx_seq[1:]):
            ma_a = _ma(closes, MA_P, a)
            if ma_a is None:
                continue
            if candles[b].low < candles[a].low and candles[a].low < ma_a:
                lower += 1
        ma_i = _ma(closes, MA_P, i)
        if lower < 2 or ma_i is None or candles[i].close >= ma_i:
            continue
        if any(c.range > CRASH_ATR * atr for c in candles[max(0, i - 3): i + 1]):
            continue
        low_px = candles[i].low
        bounce_idx = None
        for j in range(i + 1, min(len(candles), i + 1 + HORIZON)):
            if candles[j].low < low_px:
                break
            if candles[j].high >= low_px + BOUNCE_ATR * atr:
                bounce_idx = j
                break
        if bounce_idx is None:
            continue
        # ORACLE-ONLY label (future data; used solely for the upper-bound
        # arm, never in any causal decision): labeler resolution semantics.
        label = None
        tgt = low_px + TARGET_ATR * atr
        for j in range(bounce_idx, min(len(candles), i + 1 + HORIZON)):
            if candles[j].low < low_px:
                label = "fake"
                break
            if candles[j].high >= tgt:
                label = "reversal"
                break
        out.append({"low_idx": i, "low_px": low_px, "atr": atr,
                    "bounce_idx": bounce_idx, "label": label})
    return out


def entry_index(candles, s, offset: int):
    """Entry checkpoint bounce+offset, or None if the setup is already
    RESOLVED by then — low undercut (fake) or 3.3*ATR target reached
    (reversal). Entering resolved setups is nonsense: the first buggy run
    'entered' post-target setups above the target and booked guaranteed
    losses on the target exit."""
    e = s["bounce_idx"] + offset
    if e >= len(candles):
        return None
    tgt = s["low_px"] + TARGET_ATR * s["atr"]
    for k in range(s["bounce_idx"], e + 1):
        if candles[k].low < s["low_px"] or candles[k].high >= tgt:
            return None
    return e


def simulate(candles, p_series, setups, entry_offset, thr_entry, thr_exit,
             use_target, inverse=False, lo_ts=None, hi_ts=None) -> list[dict]:
    """Run the paper book over one time segment. Returns closed trades.

    thr_exit=None disables the flow exit (stop/target/time only)."""
    trades = []
    in_pos_until = -1
    for s in setups:
        e = entry_index(candles, s, entry_offset)
        if e is None:
            continue
        ts_entry = candles[e].open_ts
        if lo_ts is not None and ts_entry < lo_ts:
            continue
        if hi_ts is not None and ts_entry > hi_ts:
            continue
        if e <= in_pos_until:
            continue
        p = p_series.get(int(candles[e].open_ts))
        if p is None:
            continue
        if thr_entry is not None:
            take = (p < thr_entry) if inverse else (p >= thr_entry)
            if not take:
                continue
        entry_px = candles[e].close
        exit_px = None
        reason = None
        k_exit = None
        for k in range(e + 1, len(candles)):
            c = candles[k]
            if c.low < s["low_px"]:
                exit_px, reason = min(c.close, s["low_px"]), "stop"
            elif use_target and c.high >= s["low_px"] + TARGET_ATR * s["atr"]:
                exit_px, reason = s["low_px"] + TARGET_ATR * s["atr"], "target"
            else:
                pk = p_series.get(int(c.open_ts))
                if thr_exit is not None and pk is not None and pk < thr_exit:
                    exit_px, reason = c.close, "flow"
                elif k - s["low_idx"] > HORIZON:
                    exit_px, reason = c.close, "time"
            if exit_px is not None:
                k_exit = k
                break
        if exit_px is None:      # tape ended in-position: mark at last close
            k_exit = len(candles) - 1
            exit_px, reason = candles[-1].close, "eod"
        ret = exit_px / entry_px - 1.0 - 2 * FEE
        trades.append({"entry_ts": ts_entry, "entry": entry_px,
                       "exit": exit_px, "ret": ret, "reason": reason,
                       "hold": k_exit - e, "p_entry": round(p, 4)})
        in_pos_until = k_exit
    return trades


def stats(trades) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"n": len(trades), "win_rate": round(len(wins) / len(rets), 3),
            "avg_ret_pct": round(sum(rets) / len(rets) * 100, 3),
            "total_ret_pct": round((eq - 1) * 100, 2),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
            if losses and sum(losses) != 0 else None,
            "avg_hold_h": round(sum(t["hold"] for t in trades) / len(trades), 1),
            "exit_reasons": reasons}


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def run_pair(pair: str, cfg: dict, train_frac: float) -> dict:
    candles, rows = replay_pair(pair, cfg)
    lo = int(candles[0].open_ts)
    hi = int(candles[-1].open_ts)
    split = int(lo + (hi - lo) * train_frac)
    info = train_weights_and_series(pair, cfg, candles, rows, split)
    p_series = {ts: v["p_up"] for ts, v in info["series"].items()
                if not v["tainted"]}
    setups = causal_setups(candles, cfg)

    def bh(a, b):
        cs = [c for c in candles if a <= c.open_ts <= b]
        return round((cs[-1].close / cs[0].close - 1) * 100, 2) if len(cs) > 1 else None

    out = {"pair": pair, "window": [lo, hi], "split": split,
           "n_setups": len(setups),
           "oos_auc_bounce3": info["test_auc_bounce3"],
           "buy_hold_train_pct": bh(lo, split),
           "buy_hold_test_pct": bh(split, hi), "arms": {},
           "thresholds": {}}

    for off in (1, 3):
        # train-segment posterior distribution at THIS entry checkpoint
        tps = []
        for s in setups:
            e = entry_index(candles, s, off)
            if e is None or candles[e].open_ts > split:
                continue
            p = p_series.get(int(candles[e].open_ts))
            if p is not None:
                tps.append(p)
        tps.sort()
        thr = {"p25": pct(tps, 0.25), "p50": pct(tps, 0.50),
               "p75": pct(tps, 0.75)}
        out["thresholds"][f"bounce+{off}"] = thr
        flow_thr = pct(tps, 0.25)
        entry_arms = [("all", None, False)]
        if thr["p50"] is not None:  # gates need a train distribution
            entry_arms += [("gate_p50", thr["p50"], False),
                           ("gate_p75", thr["p75"], False),
                           ("inverse_p50", thr["p50"], True)]
        exit_arms = [("exitA_flow", flow_thr, False),
                     ("exitB_tgt+flow", flow_thr, True),
                     ("exitC_tgt", None, True)]
        for seg, a, b in (("train", lo, split), ("test", split, hi)):
            for name, t_e, inv in entry_arms:
                for ex_name, t_x, use_tgt in exit_arms:
                    trades = simulate(candles, p_series, setups, off, t_e,
                                      t_x, use_tgt, inverse=inv,
                                      lo_ts=a, hi_ts=b)
                    key = f"{seg}.b{off}.{name}.{ex_name}"
                    out["arms"][key] = stats(trades)
            # ORACLE upper bound: perfect foresight enters only labeled
            # reversals (future data, explicitly non-causal). If even this
            # loses, the trade construction cannot be monetized at these
            # fees regardless of classifier quality.
            oracle = [s for s in setups if s["label"] == "reversal"]
            for ex_name, t_x, use_tgt in exit_arms:
                trades = simulate(candles, p_series, oracle, off, None,
                                  t_x, use_tgt, lo_ts=a, hi_ts=b)
                out["arms"][f"{seg}.b{off}.ORACLE.{ex_name}"] = stats(trades)

    # Mechanical selection: best TRAIN arm (heartbeat-gated, non-inverse,
    # >=8 trades) by total return; its TEST row is the one OOS verdict.
    best, best_ret = None, None
    for key, s in out["arms"].items():
        seg, boff, name, ex = key.split(".")
        if seg != "train" or s.get("n", 0) < 8:
            continue
        if not name.startswith("gate"):
            continue
        if best_ret is None or s["total_ret_pct"] > best_ret:
            best, best_ret = key, s["total_ret_pct"]
    if best:
        test_key = best.replace("train.", "test.")
        out["selected"] = {"train_arm": best, "train": out["arms"][best],
                           "oos_verdict_arm": test_key,
                           "oos_verdict": out["arms"].get(test_key)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC/USD,ETH/USD")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--fee-bps", type=float, default=26.0,
                    help="per-side fee in bps (26 taker default; 16 maker "
                         "for sensitivity)")
    ap.add_argument("--out", default=str(HEARTBEAT_ROOT / "evidence" /
                                         "paper_bounce_sim.json"))
    args = ap.parse_args()
    global FEE
    FEE = args.fee_bps / 10000.0
    cfg = load_config(None)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "fee_per_side": FEE, "pairs": {}}
    for pair in [p.strip() for p in args.pairs.split(",")]:
        r = run_pair(pair, cfg, args.train_frac)
        report["pairs"][pair] = r
        print(f"\n== {pair}: {r['n_setups']} setups, OOS AUC {r['oos_auc_bounce3']}, "
              f"B&H test {r['buy_hold_test_pct']}%")
        for arm, s in r["arms"].items():
            if arm.startswith("test."):
                print(f"  {arm:>28}: {s}")
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
