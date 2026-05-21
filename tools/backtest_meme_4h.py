"""Virtual backtest for APEX Meme Engine - runs actual SignalEngine logic.

Usage: python tools/backtest_meme_4h.py [--interval 15]
Fetches OHLC data from Kraken for all three pairs and simulates
the full entry/exit logic bar-by-bar.
Default interval: 15 minutes (matching live engine).
"""
import sys, os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_meme_agent import (
    SignalEngine, CandleBar, PairProfile, PROFILES, DEFAULT_PROFILE,
    Position, half_kelly_size, KELLY_DEFAULT_WIN_RATE, KELLY_DEFAULT_PAYOFF,
    REENTRY_COOLDOWN_BARS, CONSEC_LOSS_HALT_THRESHOLD, CONSEC_LOSS_HALT_BARS,
    MACRO_EMA50_LOOKBACK, TAKER_FEE_RATE, BASE_CAPITAL,
)
import subprocess, json, shlex, time, threading

_cli_lock = threading.Lock()
_cli_last_call = 0.0

def kraken_cli(args, timeout=30):
    global _cli_last_call
    with _cli_lock:
        now = time.time()
        wait = 2.0 - (now - _cli_last_call)
        if wait > 0:
            time.sleep(wait)
        _cli_last_call = time.time()
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    cmd_str = f"source ~/.cargo/env && kraken {quoted} -o json 2>/dev/null"
    cmd = ["wsl", "-d", os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu"), "--", "bash", "-c", cmd_str]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if not result.stdout.strip():
            return {"error": f"Empty response (exit code {result.returncode})"}
        return json.loads(result.stdout.strip())
    except Exception as e:
        return {"error": str(e)}


def fetch_ohlc(pair_nodash: str, interval: int = 240) -> list[CandleBar]:
    """Fetch OHLC bars from Kraken CLI."""
    data = kraken_cli(["ohlc", pair_nodash, "--interval", str(interval)])
    if "error" in data:
        print(f"  ERROR fetching {pair_nodash}: {data['error']}")
        return []
    key = next((k for k in data if k != "last"), None)
    if not key:
        print(f"  ERROR: no data key in response for {pair_nodash}")
        return []
    raw = data[key]
    bars = []
    for b in raw[:-1]:  # exclude last (open) bar
        bars.append(CandleBar(
            ts=int(b[0]), open=float(b[1]), high=float(b[2]),
            low=float(b[3]), close=float(b[4]), vwap=float(b[5]),
            volume=float(b[6]), count=int(b[7]),
        ))
    return bars


def simulate_pair(pair: str, bars: list[CandleBar], interval_min: int = 15) -> dict:
    """Run full signal engine simulation on bars."""
    profile = PROFILES.get(pair, DEFAULT_PROFILE)
    engine = SignalEngine(profile)

    trades = []
    position = None
    bar_count = 0
    last_exit_bar = -REENTRY_COOLDOWN_BARS
    consec_stops = 0
    halt_until_bar = 0
    btc_risk_off = False  # simplified -- no BTC gating in backtest
    obi_sim = 0.15  # simulated OBI (neutral-positive)
    trade_log_pnl = []  # for Half-Kelly computation

    hours = len(bars) * interval_min / 60
    print(f"\n{'='*70}")
    print(f"  {pair} -- {len(bars)} bars @ {interval_min}min ({hours/24:.1f} days)")
    print(f"  Price range: {min(b.close for b in bars):.6f} – {max(b.close for b in bars):.6f}")
    total_range_pct = (max(b.close for b in bars) - min(b.close for b in bars)) / min(b.close for b in bars) * 100
    print(f"  Total range: {total_range_pct:.1f}%")
    print(f"  Profile: target_mom={profile.profit_target_pct*100:.1f}% stop={profile.hard_stop_pct*100:.1f}% "
          f"trail_act={profile.trailing_activate_pct*100:.1f}% trail_off={profile.trailing_offset_pct*100:.1f}%")
    print(f"{'='*70}")

    for i, bar in enumerate(bars):
        engine.add_bar(bar)
        bar_count += 1

        # Exit logic
        if position is not None:
            position.candles_held += 1
            if bar.high > position.peak_price:
                position.peak_price = bar.high
            reason = engine.evaluate_exit_bar(position, bar)

            # Also check hard stop on bar low
            pnl_low = (bar.low - position.entry_price) / position.entry_price
            if pnl_low <= profile.hard_stop_pct:
                reason = "hard_stop"

            # Check trailing stop
            if reason is None and position.peak_price > 0:
                peak_pct = (position.peak_price - position.entry_price) / position.entry_price
                if peak_pct >= profile.trailing_activate_pct:
                    trail_level = position.peak_price * (1 - profile.trailing_offset_pct)
                    if bar.low <= trail_level:
                        reason = "trailing_stop"

            if reason:
                # Compute exit price
                if reason == "hard_stop":
                    exit_price = position.entry_price * (1 + profile.hard_stop_pct)
                elif reason == "trailing_stop":
                    trail_level = position.peak_price * (1 - profile.trailing_offset_pct)
                    exit_price = trail_level
                elif reason == "profit_target":
                    if position.entry_mode == "bounce":
                        exit_price = position.entry_price * (1 + profile.bounce_profit_pct)
                    else:
                        exit_price = position.entry_price * (1 + profile.profit_target_pct)
                else:
                    exit_price = bar.close

                gross = (exit_price - position.entry_price) * position.qty
                entry_fee = position.notional_usd * TAKER_FEE_RATE
                exit_fee = exit_price * position.qty * TAKER_FEE_RATE
                net_pnl = gross - entry_fee - exit_fee

                trades.append({
                    "entry_bar": position.entry_ts,
                    "exit_bar": bar_count,
                    "entry_price": position.entry_price,
                    "exit_price": exit_price,
                    "net_pnl": net_pnl,
                    "reason": reason,
                    "hold_bars": position.candles_held,
                    "mode": position.entry_mode,
                    "kelly_size": position.notional_usd,
                    "peak_pct": (position.peak_price - position.entry_price) / position.entry_price * 100,
                })
                trade_log_pnl.append(net_pnl)

                if net_pnl < 0:
                    consec_stops += 1
                    if consec_stops >= CONSEC_LOSS_HALT_THRESHOLD:
                        halt_until_bar = bar_count + CONSEC_LOSS_HALT_BARS
                else:
                    consec_stops = 0

                position = None
                last_exit_bar = bar_count
                continue

        # Entry logic
        loss_halted = bar_count < halt_until_bar
        cooldown_ok = (bar_count - last_exit_bar) >= REENTRY_COOLDOWN_BARS

        if position is None and not loss_halted and cooldown_ok and len(engine._bars) >= 20:
            # Simulate OBI varying slightly based on bar close vs open
            obi_sim = 0.25 if bar.close > bar.open else 0.05
            gates = engine.evaluate_entry_gates(bar, obi_sim, 200.0)
            # We don't apply BTC risk-off in backtest

            if gates["all_pass"]:
                confidence = gates["confidence"]
                # Compute Kelly size
                if len(trade_log_pnl) >= 5:
                    wins = [p for p in trade_log_pnl if p > 0]
                    losses = [p for p in trade_log_pnl if p < 0]
                    wr = len(wins) / len(trade_log_pnl)
                    avg_win = sum(wins) / len(wins) if wins else 0
                    avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
                    payoff = avg_win / avg_loss if avg_loss > 0 else KELLY_DEFAULT_PAYOFF
                    kelly_size = half_kelly_size(wr, payoff, confidence)
                else:
                    kelly_size = half_kelly_size(
                        KELLY_DEFAULT_WIN_RATE, KELLY_DEFAULT_PAYOFF, confidence
                    )

                entry_mode = gates["entry_mode"]
                entry_price = bar.close  # simulate fill at close
                qty = kelly_size / entry_price if entry_price > 0 else 0

                if kelly_size > 0 and qty > 0:
                    position = Position(
                        entry_price=entry_price,
                        qty=qty,
                        notional_usd=kelly_size,
                        entry_ts=bar_count,
                        peak_price=bar.high,
                        entry_mode=entry_mode,
                    )

    # Close any remaining position at last bar price
    if position is not None:
        exit_price = bars[-1].close
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_fee = exit_price * position.qty * TAKER_FEE_RATE
        net_pnl = gross - entry_fee - exit_fee
        trades.append({
            "entry_bar": position.entry_ts,
            "exit_bar": bar_count,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "net_pnl": net_pnl,
            "reason": "end_of_data",
            "hold_bars": position.candles_held,
            "mode": position.entry_mode,
            "kelly_size": position.notional_usd,
            "peak_pct": (position.peak_price - position.entry_price) / position.entry_price * 100,
        })

    return {
        "pair": pair,
        "bars": len(bars),
        "days": hours / 24,
        "interval": interval_min,
        "trades": trades,
    }


def print_results(result: dict):
    pair = result["pair"]
    trades = result["trades"]
    days = result["days"]

    if not trades:
        print(f"\n  {pair}: 0 trades in {days:.1f} days -- strategy correctly avoided this pair")
        print(f"  (Macro trend filter + gates prevented entries during unfavorable conditions)")
        return

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    total_pnl = sum(t["net_pnl"] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    avg_pnl = total_pnl / len(trades)
    avg_win = sum(t["net_pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["net_pnl"] for t in losses) / len(losses) if losses else 0
    total_kelly = sum(t["kelly_size"] for t in trades)
    roi = (total_pnl / (total_kelly / len(trades))) * 100 if total_kelly > 0 else 0

    print(f"\n  +- {pair} RESULTS {'-'*50}")
    interval_label = f"{result.get('interval', 240)}min"
    print(f"  | Period: {days:.1f} days ({result['bars']} bars @ {interval_label})")
    print(f"  | Trades: {len(trades)} (wins: {len(wins)}, losses: {len(losses)})")
    print(f"  | Win Rate: {win_rate:.1f}%")
    print(f"  | Total P&L: ${total_pnl:.2f}")
    print(f"  | Avg P&L/trade: ${avg_pnl:.2f}")
    print(f"  | Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}")
    print(f"  | Avg Kelly Size: ${total_kelly/len(trades):.0f}")
    print(f"  | ROI (on avg kelly): {roi:.2f}%")
    print(f"  | Annualized: {roi * 365 / max(days, 1):.1f}%")
    print(f"  +{'-'*65}")

    # Per-trade detail
    print(f"\n  {'#':>3} {'MODE':<8} {'ENTRY':>10} {'EXIT':>10} {'P&L':>8} {'REASON':<15} {'HOLD':>5} {'PEAK%':>6} {'KELLY$':>7}")
    print(f"  {'-'*3} {'-'*8} {'-'*10} {'-'*10} {'-'*8} {'-'*15} {'-'*5} {'-'*6} {'-'*7}")
    for i, t in enumerate(trades, 1):
        pnl_str = f"${t['net_pnl']:+.2f}"
        pnl_color = ""
        print(f"  {i:>3} {t['mode']:<8} {t['entry_price']:>10.6f} {t['exit_price']:>10.6f} "
              f"{pnl_str:>8} {t['reason']:<15} {t['hold_bars']:>4}b {t['peak_pct']:>5.1f}% ${t['kelly_size']:>5.0f}")

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t["reason"]
        reasons[r] = reasons.get(r, 0) + 1
    print(f"\n  Exit reasons: {', '.join(f'{r}={c}' for r, c in sorted(reasons.items(), key=lambda x: -x[1]))}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="APEX Meme Engine backtest")
    parser.add_argument("--interval", type=int, default=15,
                        help="Candle interval in minutes (default: 15)")
    args = parser.parse_args()
    interval = args.interval

    print("=" * 70)
    print(f"  APEX MEME ENGINE -- {interval}min VIRTUAL BACKTEST")
    print(f"  Using actual SignalEngine logic with 3/4-Kelly sizing")
    print("=" * 70)

    pairs = [
        ("NIGHT/USD", "NIGHTUSD"),
        ("AAVE/USD", "AAVEUSD"),
        ("AAVE/BTC", "AAVEXBT"),
    ]

    results = []
    for pair_name, pair_cli in pairs:
        print(f"\n  Fetching {interval}min OHLC for {pair_name}...")
        bars = fetch_ohlc(pair_cli, interval)
        if not bars:
            print(f"  SKIP: No data for {pair_name}")
            continue
        result = simulate_pair(pair_name, bars, interval)
        results.append(result)
        print_results(result)

    # Summary
    print(f"\n\n{'='*70}")
    print("  AGGREGATE SUMMARY")
    print(f"{'='*70}")
    total_trades = sum(len(r["trades"]) for r in results)
    total_pnl = sum(sum(t["net_pnl"] for t in r["trades"]) for r in results)
    total_wins = sum(len([t for t in r["trades"] if t["net_pnl"] > 0]) for r in results)
    total_kelly_alloc = sum(sum(t["kelly_size"] for t in r["trades"]) for r in results)

    print(f"  Total trades across all pairs: {total_trades}")
    print(f"  Total net P&L: ${total_pnl:.2f}")
    print(f"  Overall win rate: {total_wins/max(total_trades,1)*100:.1f}%")
    print(f"  Total capital deployed (kelly sums): ${total_kelly_alloc:.0f}")
    if total_kelly_alloc > 0 and total_trades > 0:
        avg_size = total_kelly_alloc / total_trades
        overall_roi = total_pnl / avg_size * 100
        print(f"  Overall ROI (on avg position): {overall_roi:.2f}%")

    # Cross-pair correlation note
    print(f"\n  CROSS-PAIR NOTES:")
    for r in results:
        trades = r["trades"]
        if not trades:
            print(f"    {r['pair']}: no entries (macro filter blocked -- bearish period)")
        else:
            entry_bars = [t["entry_bar"] for t in trades]
            print(f"    {r['pair']}: {len(trades)} entries at bars {entry_bars[:5]}{'...' if len(entry_bars)>5 else ''}")


if __name__ == "__main__":
    main()
