"""heartbeat CLI.

    heartbeat backfill  --pair BTC/USD --tf 1h --days 90
    heartbeat run       --pair BTC/USD --tf 1h
    heartbeat eval      --pair BTC/USD --tf 1h
    heartbeat calibrate --pairs BTC/USD,ETH/USD --walk-forward
    heartbeat replay    --tape data/BTC_2026-07.parquet
    heartbeat status
    heartbeat synth     --pair BTC/USD --tf 1h --days 90 --seed 7

`run` line format (machine-parseable, one line per heartbeat):
    ts pair tf candle_progress P_up L OFI CLV vol_z [TAINTED]
plus a candle-close summary line prefixed `CLOSE`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

from .api import TcpStatusServer, status_payload, write_status_file
from .config import load_config
from .engine.calibrate import event_vectors, fit_weights, walk_forward
from .engine.candle import candles_from_trades
from .engine.pipeline import HeartbeatPipeline, run_tape
from .engine.posterior import PosteriorEngine
from .eval.labeler import extract_events
from .eval.report import build_report, render_markdown, write_report
from .feed.kraken_rest import KrakenRest
from .feed.kraken_ws import KrakenWsClient
from .feed.tape import TapeMonitor
from .store import Store

log = logging.getLogger("heartbeat")


def _fmt(x, nd=4) -> str:
    return "na" if x is None else f"{x:.{nd}f}"


def _hb_line(pair: str, tf: str, out, progress: float) -> str:
    parts = [f"{out.ts:.3f}", pair, tf, f"{progress:.3f}",
             f"{out.p_up:.5f}", f"{out.L:+.5f}",
             _fmt(out.raw.get('ofi')), _fmt(out.raw.get('clv')),
             _fmt(out.raw.get('vol_z'), 2)]
    if out.tainted:
        parts.append("TAINTED")
    return " ".join(parts)


def _close_line(pair: str, tf: str, row: dict) -> str:
    t = " TAINTED" if row["tainted"] else ""
    return (f"CLOSE {row['ts']:.0f} {pair} {tf} O={row['open']:.1f} "
            f"H={row['high']:.1f} L={row['low']:.1f} C={row['close']:.1f} "
            f"V={row['volume']:.4f} n={row['trade_count']} "
            f"P_up={row['p_up']:.5f} L={row['L']:+.5f}{t}")


def _rows_digest(rows: list[dict]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_backfill(args, cfg) -> int:
    """Pull `--days` of trades via REST (paginated `since` cursor), build
    candles, persist tape + candles to the parquet store.

    NOTE: Kraken's public Trades endpoint serves ~1000 trades/request at
    ~1 req/s — 90 days of BTC/USD is millions of trades and takes hours.
    That is a Kraken limit, not a heartbeat one. Progress is streamed and
    every page is persisted, so the command is resumable-by-rerun."""
    store = Store(cfg["store"]["root"])
    rest = KrakenRest(cfg["feed"]["rest_url"], cfg["feed"]["rest_rate_per_s"],
                      cfg["feed"]["rest_burst"])
    now = time.time()
    ts_start = now - args.days * 86400
    total = 0

    def on_page(page):
        nonlocal total
        store.append_tape(args.pair, args.tf, page)
        total += len(page)
        print(f"backfill {args.pair}: {total} trades "
              f"(through {page[-1].ts:.0f})", flush=True)

    trades, complete = rest.trades_range(args.pair, ts_start, now,
                                         on_page=on_page)
    candles = candles_from_trades(trades, args.tf, include_final=False)
    print(f"backfill complete={complete}: {len(trades)} trades -> "
          f"{len(candles)} closed {args.tf} candles")
    if not complete:
        print("WARNING: pagination did not reach the requested end — "
              "the uncovered tail must be treated as tainted", file=sys.stderr)
    # Also snapshot OHLC (720 most recent) for scaler warmup cross-checks.
    ohlc = rest.ohlc(args.pair, args.tf)
    print(f"OHLC bootstrap: {len(ohlc)} candles from REST")
    return 0 if complete else 1


def cmd_run(args, cfg) -> int:
    """Live: stream trades over WS v2, print one line per heartbeat, and
    serve P(up) via the status file + TCP API (see README contract)."""
    pair, tf = args.pair, args.tf
    store = Store(cfg["store"]["root"])
    rest = KrakenRest(cfg["feed"]["rest_url"], cfg["feed"]["rest_rate_per_s"],
                      cfg["feed"]["rest_burst"])
    monitor = TapeMonitor(cfg["feed"]["clock_skew_alert_s"])
    engine = PosteriorEngine(cfg)
    scaler_state = store.load_scalers(pair, tf)
    if scaler_state:
        engine.load_scaler_state(scaler_state)
        print(f"loaded persisted scalers for {pair} {tf}")

    pipe = HeartbeatPipeline(cfg, pair, tf, engine=engine, monitor=monitor)
    pending_rows: list[dict] = []
    pending_trades = []

    def on_hb(out, progress):
        print(_hb_line(pair, tf, out, progress), flush=True)
        _write_status(cfg, pair, tf, pipe, monitor)

    def on_candle(row):
        print(_close_line(pair, tf, row), flush=True)
        pending_rows.append(row)
        if len(pending_rows) >= 1:
            store.append_posterior(pair, tf, list(pending_rows))
            pending_rows.clear()
        store.save_scalers(pair, tf, engine.scaler_state())

    pipe.on_heartbeat = on_hb
    pipe.on_candle = on_candle

    # -- bootstrap: warm scalers from up to 720 REST OHLC candles + recent trades
    if not scaler_state:
        try:
            ohlc = rest.ohlc(pair, tf)
            from .engine.candle import ClosedCandle, tf_seconds
            hist = [ClosedCandle(o.ts, o.ts + tf_seconds(tf), o.open, o.high,
                                 o.low, o.close, o.volume, 0.0, 0.0, o.count,
                                 o.vwap) for o in ohlc]
            pushed = pipe.bootstrap(hist)
            print(f"bootstrap: {len(hist)} OHLC candles, {pushed} scaler values")
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: OHLC bootstrap failed ({e}); scalers warm from live",
                  file=sys.stderr)

    async def on_trade(trade, local_ts):
        pending_trades.append(trade)
        if len(pending_trades) >= 100:
            store.append_tape(pair, tf, list(pending_trades))
            pending_trades.clear()
        pipe.feed_trade(trade, local_ts=local_ts, observe=False)

    ws = KrakenWsClient(pair, on_trade, monitor, rest=rest,
                        ws_url=cfg["feed"]["ws_url"],
                        reconnect_base_s=cfg["feed"]["reconnect_base_s"],
                        reconnect_max_s=cfg["feed"]["reconnect_max_s"])
    # WS client observes trades into the monitor itself; pipeline must not
    # double-observe (observe=False above).

    async def main_async():
        api_task = asyncio.create_task(_serve_tcp(cfg, pair, tf, pipe, monitor))
        try:
            await ws.run()
        finally:
            api_task.cancel()
            if pending_trades:
                store.append_tape(pair, tf, list(pending_trades))
            if pending_rows:
                store.append_posterior(pair, tf, pending_rows)
            store.save_scalers(pair, tf, engine.scaler_state())

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    return 0


def _write_status(cfg, pair, tf, pipe, monitor) -> None:
    write_status_file(cfg["api"]["status_file"],
                      status_payload(pair, tf, pipe, monitor))


async def _serve_tcp(cfg, pair, tf, pipe, monitor):
    server = TcpStatusServer(lambda: status_payload(pair, tf, pipe, monitor))
    await server.start(cfg["api"]["tcp_host"], cfg["api"]["tcp_port"])
    await server.serve_forever()


def _posterior_series(cfg, pair, tf, trades):
    """Replay a stored tape through the pipeline -> (candles, rows)."""
    rows = run_tape(cfg, pair, tf, trades)
    candles = candles_from_trades(trades, tf, include_final=True)
    if len(candles) != len(rows):
        raise RuntimeError(f"candle/row misalignment: {len(candles)} vs {len(rows)}")
    return candles, rows


def cmd_eval(args, cfg) -> int:
    store = Store(cfg["store"]["root"])
    trades = store.read_tape(args.pair, args.tf)
    if not trades:
        print(f"no tape stored for {args.pair} {args.tf}; run backfill or synth",
              file=sys.stderr)
        return 2
    candles, rows = _posterior_series(cfg, args.pair, args.tf, trades)
    p_up = [r["p_up"] for r in rows]
    events = extract_events(args.pair, args.tf, candles, p_up, cfg)
    report = build_report(args.pair, args.tf, events, cfg)
    mpath, jpath = write_report(report, Path(cfg["store"]["root"]) / "reports")
    print(render_markdown(report))
    print(f"wrote {mpath} and {jpath}")
    return 0


def cmd_calibrate(args, cfg) -> int:
    store = Store(cfg["store"]["root"])
    ccfg = cfg.get("calibrate", {})
    any_output = False
    for pair in args.pairs.split(","):
        pair = pair.strip()
        trades = store.read_tape(pair, args.tf)
        if not trades:
            print(f"{pair}: no tape stored — skipping", file=sys.stderr)
            continue
        candles, rows = _posterior_series(cfg, pair, args.tf, trades)
        p_up = [r["p_up"] for r in rows]
        events = extract_events(pair, args.tf, candles, p_up, cfg)
        clean = [e for e in events if not e.tainted]
        names = [f.name for f in PosteriorEngine(cfg).features]
        vecs = event_vectors(clean, rows, names)
        print(f"\n== {pair} {args.tf}: {len(clean)} events "
              f"({sum(1 for e in clean if e.label=='reversal')} reversal / "
              f"{sum(1 for e in clean if e.label=='fake')} fake)")
        if args.walk_forward:
            folds = walk_forward(vecs, names,
                                 folds=int(ccfg.get("walk_forward_folds", 4)),
                                 min_train=int(ccfg.get("min_train_events", 20)),
                                 l2=float(ccfg.get("l2", 1.0)),
                                 lr=float(ccfg.get("lr", 0.05)),
                                 iters=int(ccfg.get("iters", 2000)))
            print(f"{'fold':>4} {'train range':>24} {'test range':>24} "
                  f"{'n':>4} {'AUC+1':>7} {'AUC+2':>7} {'AUC+3':>7} overlap")
            for f in folds:
                print(f"{f['fold']:>4} {f['train_range']:>24} "
                      f"{f['test_range']:>24} {f['test_n']:>4} "
                      f"{str(f['auc_bounce1']):>7} {str(f['auc_bounce2']):>7} "
                      f"{str(f['auc_bounce3']):>7} "
                      f"{'NONE' if f['no_overlap'] else 'OVERLAP!'}")
        try:
            weights = fit_weights(vecs, names,
                                  l2=float(ccfg.get("l2", 1.0)),
                                  lr=float(ccfg.get("lr", 0.05)),
                                  iters=int(ccfg.get("iters", 2000)))
        except ValueError as e:
            print(f"{pair}: cannot fit weights ({e})", file=sys.stderr)
            continue
        out = Path(cfg["store"]["root"]) / "reports" / \
            f"weights_{pair.replace('/', '_')}_{args.tf}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"pair": pair, "tf": args.tf,
                                   "weights": weights}, indent=2))
        print(f"final weights (fit on all {len(vecs)} events) -> {out}")
        for k, v in weights.items():
            print(f"    {k:>16}: {v:+.4f}")
        any_output = True
    return 0 if any_output else 2


def cmd_replay(args, cfg) -> int:
    store = Store(cfg["store"]["root"])
    trades = store.read_tape_file(args.tape)
    pair = args.pair or cfg["pair"]
    tf = args.tf or cfg["timeframe"]
    printed = [0]

    def on_hb(out, progress):
        if args.verbose:
            print(_hb_line(pair, tf, out, progress))
        printed[0] += 1

    rows = run_tape(cfg, pair, tf, trades, on_heartbeat=on_hb)
    for row in rows if args.verbose else rows[-3:]:
        print(_close_line(pair, tf, row))
    print(f"replay: {len(trades)} trades -> {printed[0]} heartbeats, "
          f"{len(rows)} candles")
    print(f"digest: {_rows_digest(rows)}")
    return 0


def cmd_status(args, cfg) -> int:
    path = Path(cfg["api"]["status_file"])
    if not path.exists():
        print(f"no status file at {path} — is `heartbeat run` active?",
              file=sys.stderr)
        return 2
    print(path.read_text())
    return 0


def cmd_synth(args, cfg) -> int:
    """Generate the deterministic synthetic tape into the store (offline
    pipeline validation; see synth.py honesty note)."""
    from .synth import SynthSpec, generate_tape
    from .engine.candle import tf_seconds
    spec = SynthSpec(seed=args.seed, days=args.days,
                     tf_s=tf_seconds(args.tf))
    trades, injected = generate_tape(spec)
    store = Store(cfg["store"]["root"])
    p = store.append_tape(args.pair, args.tf, trades)
    print(f"synth tape: {len(trades)} trades, {len(injected)} injected events "
          f"({sum(1 for e in injected if e['kind']=='reversal')} reversal / "
          f"{sum(1 for e in injected if e['kind']=='fake')} fake) -> {p}")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="heartbeat")
    ap.add_argument("--config", help="user config yaml merged over default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, pairs=False):
        if pairs:
            p.add_argument("--pairs", required=True)
        else:
            p.add_argument("--pair", default=None)
        p.add_argument("--tf", default=None)

    p = sub.add_parser("backfill"); common(p)
    p.add_argument("--days", type=int, default=90)
    p = sub.add_parser("run"); common(p)
    p = sub.add_parser("eval"); common(p)
    p = sub.add_parser("calibrate"); common(p, pairs=True)
    p.add_argument("--walk-forward", action="store_true")
    p = sub.add_parser("replay")
    p.add_argument("--tape", required=True)
    p.add_argument("--pair", default=None)
    p.add_argument("--tf", default=None)
    p.add_argument("--verbose", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("synth"); common(p)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--seed", type=int, default=7)
    return ap


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "pair", None) is None and hasattr(args, "pair"):
        args.pair = cfg["pair"]
    if getattr(args, "tf", None) is None and hasattr(args, "tf"):
        args.tf = cfg["timeframe"]
    handlers = {"backfill": cmd_backfill, "run": cmd_run, "eval": cmd_eval,
                "calibrate": cmd_calibrate, "replay": cmd_replay,
                "status": cmd_status, "synth": cmd_synth}
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
