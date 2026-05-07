"""Offline backtest of APEX meme engine rules against real PLAY/USD 5-min OHLC data.

Uses the exact same indicator functions from hydra_meme_agent.py.
Simulates entry gates and exit triggers with configurable parameters.

OBI is NOT available in historical data (requires live orderbook), so the backtest
uses a synthetic OBI proxy derived from candle structure.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_meme_agent import (
    CandleBar, wilder_rsi, vol_ema, atr_pct, SignalEngine, Position,
    TAKER_FEE_RATE, MAKER_FEE_RATE, TAKER_SLIPPAGE_BPS, WARMUP_BARS, CANDLE_BUFFER_SIZE,
)

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play_ohlc_raw.json")

V1_DEFAULTS = {
    "rsi_entry_low": 45,
    "rsi_entry_high": 78,
    "rsi_exhaust": 82,
    "vol_spike_mult": 1.8,
    "vol_death_mult": 0.4,
    "obi_entry": 0.20,
    "obi_book_fade": -0.20,
    "profit_target_pct": 0.025,
    "hard_stop_pct": -0.013,
    "time_stop_candles": 3,
    "position_size": 300.0,
    "daily_cap": 30.0,
    "require_uptrend": False,
    "ema_trend_fast": 5,
    "ema_trend_slow": 15,
    "trailing_stop_pct": None,
    "partial_profit_at": None,
    "partial_profit_frac": 0.5,
    "extension_max_pct": None,
    "reentry_cooldown": 0,
    "atr_min_pct": None,
    "fee_mode": "taker",
}

DEFAULTS = {
    "rsi_entry_low": 45,
    "rsi_entry_high": 78,
    "rsi_exhaust": 82,
    "vol_spike_mult": 1.8,
    "vol_death_mult": 0.4,
    "obi_entry": 0.20,
    "obi_book_fade": -0.20,
    "profit_target_pct": 0.030,
    "hard_stop_pct": -0.010,
    "time_stop_candles": 3,
    "position_size": 300.0,
    "daily_cap": 30.0,
    "require_uptrend": True,
    "ema_trend_fast": 8,
    "ema_trend_slow": 21,
    "trailing_stop_pct": None,
    "partial_profit_at": None,
    "partial_profit_frac": 0.5,
    "extension_max_pct": 0.20,
    "reentry_cooldown": 2,
    "atr_min_pct": 0.015,
    "fee_mode": "maker",
}


def load_bars(path: str) -> list[CandleBar]:
    with open(path) as f:
        data = json.load(f)
    key = [k for k in data if k != "last"][0]
    raw = data[key]
    return [CandleBar(
        ts=int(b[0]), open=float(b[1]), high=float(b[2]),
        low=float(b[3]), close=float(b[4]), vwap=float(b[5]),
        volume=float(b[6]), count=int(b[7]),
    ) for b in raw]


def synthetic_obi(bar: CandleBar) -> float:
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    body_pct = (bar.close - bar.open) / rng
    wick_bias = ((bar.close - bar.low) - (bar.high - bar.close)) / rng
    return 0.4 * body_pct + 0.6 * wick_bias


def ema_val(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def run_backtest(bars: list[CandleBar], cfg: dict = None) -> dict:
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    fee_rate = MAKER_FEE_RATE if c.get("fee_mode") == "maker" else TAKER_FEE_RATE

    engine = SignalEngine()
    position = None
    trades = []
    daily_pnl = 0.0
    daily_loss = 0.0
    halted = False
    entry_signals = 0
    gate_failures = {"volume_spike": 0, "obi": 0, "vwap_align": 0, "rsi_window": 0, "trend": 0, "extension": 0, "cooldown": 0, "atr_regime": 0}
    bars_processed = 0
    last_exit_bar = -c.get("reentry_cooldown", 0)

    for i, bar in enumerate(bars):
        engine.add_bar(bar)
        bars_processed += 1

        if not engine.is_warmed_up():
            continue
        if halted:
            continue

        closes = [b.close for b in engine._bars]
        volumes = [b.volume for b in engine._bars]

        # --- Exit checks ---
        if position is not None:
            position.candles_held += 1
            pct_high = (bar.high - position.entry_price) / position.entry_price
            pct_low = (bar.low - position.entry_price) / position.entry_price

            exit_reason = None
            exit_price = None

            # Trailing stop
            if c["trailing_stop_pct"] is not None:
                peak_since_entry = max(b.high for b in engine._bars[-position.candles_held:]) if position.candles_held > 0 else bar.high
                trail_level = peak_since_entry * (1 + c["trailing_stop_pct"])
                if bar.low <= trail_level and pct_low < 0:
                    exit_reason = "trailing_stop"
                    exit_price = trail_level

            if exit_reason is None and pct_high >= c["profit_target_pct"]:
                exit_reason = "profit_target"
                exit_price = position.entry_price * (1 + c["profit_target_pct"])
            if exit_reason is None and pct_low <= c["hard_stop_pct"]:
                exit_reason = "hard_stop"
                exit_price = position.entry_price * (1 + c["hard_stop_pct"])

            if exit_reason is None:
                rsi = wilder_rsi(closes)
                if rsi > c["rsi_exhaust"]:
                    exit_reason = "rsi_exhaust"
                    exit_price = bar.close
                elif position.candles_held >= c["time_stop_candles"]:
                    exit_reason = "time_stop"
                    exit_price = bar.close
                else:
                    vol_baseline = vol_ema(volumes)
                    if vol_baseline > 0 and bar.volume < c["vol_death_mult"] * vol_baseline:
                        exit_reason = "volume_death"
                        exit_price = bar.close

            if exit_reason is None:
                obi = synthetic_obi(bar)
                if obi < c["obi_book_fade"]:
                    exit_reason = "book_fade"
                    exit_price = bar.close

            if exit_reason:
                slippage = exit_price * TAKER_SLIPPAGE_BPS / 10_000
                fill_price = exit_price - slippage
                gross = (fill_price - position.entry_price) * position.qty
                entry_fee = position.notional_usd * fee_rate
                exit_fee = fill_price * position.qty * fee_rate
                net = gross - entry_fee - exit_fee
                daily_pnl += net
                if net < 0:
                    daily_loss += net
                trades.append({
                    "entry_ts": position.entry_ts,
                    "exit_ts": bar.ts,
                    "entry_price": position.entry_price,
                    "exit_price": fill_price,
                    "qty": position.qty,
                    "gross_pnl": gross,
                    "fees": entry_fee + exit_fee,
                    "net_pnl": net,
                    "exit_reason": exit_reason,
                    "hold_candles": position.candles_held,
                    "bar_index": i,
                })
                position = None
                last_exit_bar = i
                if daily_loss <= -c["daily_cap"]:
                    halted = True
                continue

        # --- Entry checks ---
        if position is not None:
            continue

        vol_baseline = vol_ema(volumes)
        rsi = wilder_rsi(closes)
        vwap = engine.session_vwap
        obi = synthetic_obi(bar)

        vol_pass = bar.volume > c["vol_spike_mult"] * vol_baseline
        obi_pass = obi > c["obi_entry"]
        vwap_pass = bar.close > vwap if vwap > 0 else False
        rsi_pass = c["rsi_entry_low"] <= rsi <= c["rsi_entry_high"]

        trend_pass = True
        if c["require_uptrend"] and len(closes) >= c["ema_trend_slow"]:
            ema_fast = ema_val(closes, c["ema_trend_fast"])
            ema_slow = ema_val(closes, c["ema_trend_slow"])
            trend_pass = ema_fast > ema_slow

        ext_pass = True
        if c.get("extension_max_pct") is not None and len(closes) >= c["ema_trend_slow"]:
            ema_slow_val = ema_val(closes, c["ema_trend_slow"])
            if ema_slow_val > 0:
                extension = (bar.close - ema_slow_val) / ema_slow_val
                ext_pass = extension <= c["extension_max_pct"]

        cooldown_pass = (i - last_exit_bar) >= c.get("reentry_cooldown", 0)

        atr_pass = True
        if c.get("atr_min_pct") is not None:
            cur_atr = atr_pct(engine._bars)
            atr_pass = cur_atr >= c["atr_min_pct"]

        if not vol_pass:
            gate_failures["volume_spike"] += 1
        if not obi_pass:
            gate_failures["obi"] += 1
        if not vwap_pass:
            gate_failures["vwap_align"] += 1
        if not rsi_pass:
            gate_failures["rsi_window"] += 1
        if not trend_pass:
            gate_failures["trend"] += 1
        if not ext_pass:
            gate_failures["extension"] += 1
        if not cooldown_pass:
            gate_failures["cooldown"] += 1
        if not atr_pass:
            gate_failures["atr_regime"] += 1

        if vol_pass and obi_pass and vwap_pass and rsi_pass and trend_pass and ext_pass and cooldown_pass and atr_pass:
            entry_signals += 1
            ask_est = bar.close * 1.001
            limit_price = ask_est * (1 + TAKER_SLIPPAGE_BPS / 10_000)
            qty = c["position_size"] / limit_price
            position = Position(
                entry_price=limit_price,
                qty=qty,
                notional_usd=c["position_size"],
                entry_ts=bar.ts,
            )

    # Close any remaining position
    if position is not None:
        last = bars[-1]
        fill_price = last.close * (1 - TAKER_SLIPPAGE_BPS / 10_000)
        gross = (fill_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * fee_rate
        exit_fee = fill_price * position.qty * fee_rate
        net = gross - entry_fee - exit_fee
        daily_pnl += net
        trades.append({
            "entry_ts": position.entry_ts, "exit_ts": last.ts,
            "entry_price": position.entry_price, "exit_price": fill_price,
            "qty": position.qty, "gross_pnl": gross, "fees": entry_fee + exit_fee,
            "net_pnl": net, "exit_reason": "eod_close",
            "hold_candles": position.candles_held, "bar_index": len(bars) - 1,
        })

    # Stats
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    total_gross = sum(t["gross_pnl"] for t in trades)
    total_fees = sum(t["fees"] for t in trades)
    total_net = sum(t["net_pnl"] for t in trades)
    max_dd = peak = equity = 0.0
    for t in trades:
        equity += t["net_pnl"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    exit_reasons = {}
    for t in trades:
        exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1

    price_start = bars[WARMUP_BARS].close if len(bars) > WARMUP_BARS else bars[0].close
    return {
        "bars_total": len(bars),
        "bars_processed": bars_processed,
        "warmup_bars": WARMUP_BARS,
        "first_bar": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(bars[0].ts)),
        "last_bar": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(bars[-1].ts)),
        "price_start": price_start,
        "price_end": bars[-1].close,
        "price_change_pct": (bars[-1].close - price_start) / price_start * 100,
        "entry_signals": entry_signals,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(len(trades), 1),
        "total_gross_pnl": total_gross,
        "total_fees": total_fees,
        "total_net_pnl": total_net,
        "avg_net_per_trade": total_net / max(len(trades), 1),
        "avg_win": sum(t["net_pnl"] for t in wins) / max(len(wins), 1),
        "avg_loss": sum(t["net_pnl"] for t in losses) / max(len(losses), 1),
        "max_drawdown": max_dd,
        "halted": halted,
        "exit_reasons": exit_reasons,
        "gate_failures": gate_failures,
        "fee_mode": c.get("fee_mode", "maker"),
        "atr_min_pct": c.get("atr_min_pct"),
        "trades": trades,
    }


def print_report(result: dict) -> None:
    print("=" * 72)
    print("  APEX Meme Engine -- V3+ATR Backtest Report (PLAY/USD)")
    print("=" * 72)
    print()
    print(f"  Data:  {result['first_bar']} -> {result['last_bar']}")
    print(f"  Bars:  {result['bars_total']} (5-min) | Warmup: {result['warmup_bars']}")
    print(f"  Price: ${result['price_start']:.6f} -> ${result['price_end']:.6f}  ({result['price_change_pct']:+.1f}%)")
    atr_str = f"{result['atr_min_pct']*100:.1f}%" if result.get('atr_min_pct') else "OFF"
    print(f"  Fees:  {result.get('fee_mode','maker')} | ATR gate: {atr_str}")
    print()
    print("-" * 72)
    print("  PERFORMANCE")
    print("-" * 72)
    print(f"  Entry signals:     {result['entry_signals']}")
    print(f"  Total trades:      {result['total_trades']}")
    print(f"  Wins / Losses:     {result['wins']} / {result['losses']}")
    print(f"  Win rate:          {result['win_rate']:.1%}")
    print(f"  Gross P&L:         ${result['total_gross_pnl']:+.2f}")
    print(f"  Fees:              ${result['total_fees']:.2f}")
    print(f"  Net P&L:           ${result['total_net_pnl']:+.2f}")
    print(f"  Avg net/trade:     ${result['avg_net_per_trade']:+.2f}")
    print(f"  Avg win:           ${result['avg_win']:+.2f}")
    print(f"  Avg loss:          ${result['avg_loss']:+.2f}")
    print(f"  Max drawdown:      ${result['max_drawdown']:.2f}")
    print(f"  Daily cap hit:     {'YES' if result['halted'] else 'No'}")
    print()
    print("-" * 72)
    print("  EXIT REASONS")
    print("-" * 72)
    for reason, count in sorted(result["exit_reasons"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<20s} {count:>3d}")
    print()
    print("-" * 72)
    print("  GATE FAILURE FREQUENCY (higher = more often blocking)")
    print("-" * 72)
    total_eval = result["bars_processed"] - result["warmup_bars"]
    for gate, count in sorted(result["gate_failures"].items(), key=lambda x: -x[1]):
        if count == 0:
            continue
        pct = count / max(total_eval, 1) * 100
        print(f"    {gate:<20s} {count:>5d} / {total_eval} ({pct:.0f}%)")
    print()
    print("-" * 72)
    print("  TRADE LOG")
    print("-" * 72)
    print(f"  {'#':>3s}  {'Entry':>10s}  {'Exit':>10s}  {'Gross':>8s}  {'Fees':>6s}  {'Net':>8s}  {'Reason':<16s}  Hold")
    for j, t in enumerate(result["trades"], 1):
        print(f"  {j:3d}  {t['entry_price']:10.6f}  {t['exit_price']:10.6f}  "
              f"${t['gross_pnl']:+7.2f}  ${t['fees']:5.2f}  ${t['net_pnl']:+7.2f}  "
              f"{t['exit_reason']:<16s}  {t['hold_candles']}c")
    print()


def run_sensitivity(bars: list[CandleBar]) -> None:
    print("=" * 72)
    print("  PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 72)
    print()

    configs = [
        ("v1 ORIGINAL (taker)", {k: v for k, v in V1_DEFAULTS.items() if v != DEFAULTS.get(k)}),
        ("v2 no-ATR (taker)", {"atr_min_pct": None, "fee_mode": "taker"}),
        ("v3 ATR+maker (current)", {}),
        ("ATR 1.0%", {"atr_min_pct": 0.010}),
        ("ATR 1.5% (default)", {"atr_min_pct": 0.015}),
        ("ATR 2.0%", {"atr_min_pct": 0.020}),
        ("ATR 2.5%", {"atr_min_pct": 0.025}),
        ("ATR 3.0%", {"atr_min_pct": 0.030}),
        ("ATR off (maker fees)", {"atr_min_pct": None}),
        ("ATR off (taker fees)", {"atr_min_pct": None, "fee_mode": "taker"}),
        ("Wider RSI 35-82", {"rsi_entry_low": 35, "rsi_entry_high": 82}),
        ("RSI oversold only 25-55", {"rsi_entry_low": 25, "rsi_entry_high": 55}),
        ("Vol spike 1.5x", {"vol_spike_mult": 1.5}),
        ("Vol spike 1.3x", {"vol_spike_mult": 1.3}),
        ("OBI 0.10", {"obi_entry": 0.10}),
        ("OBI 0.05", {"obi_entry": 0.05}),
        ("OBI 0.00 (off)", {"obi_entry": 0.00}),
        ("Profit 2.0%", {"profit_target_pct": 0.020}),
        ("Profit 1.5%", {"profit_target_pct": 0.015}),
        ("Stop -2.0%", {"hard_stop_pct": -0.020}),
        ("Stop -0.8%", {"hard_stop_pct": -0.008}),
        ("Time stop 5c", {"time_stop_candles": 5}),
        ("Time stop 2c", {"time_stop_candles": 2}),
        ("Scalp: 1.5%/1%/2c", {"profit_target_pct": 0.015, "hard_stop_pct": -0.010, "time_stop_candles": 2}),
        ("Scalp+low gates+ATR", {"profit_target_pct": 0.015, "hard_stop_pct": -0.010,
                                  "time_stop_candles": 2, "vol_spike_mult": 1.3, "obi_entry": 0.05}),
        ("ATR+no trend filter", {"require_uptrend": False}),
        ("ATR+relaxed gates", {"vol_spike_mult": 1.3, "obi_entry": 0.05,
                                "rsi_entry_low": 35, "rsi_entry_high": 82}),
    ]

    header = f"  {'Config':<28s}  {'Trades':>6s}  {'W/L':>7s}  {'WR':>5s}  {'Net P&L':>9s}  {'Avg':>7s}  {'MaxDD':>7s}  {'Halt':>4s}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for label, overrides in configs:
        result = run_backtest(bars, overrides)
        wl = f"{result['wins']}/{result['losses']}"
        print(f"  {label:<28s}  {result['total_trades']:>6d}  {wl:>7s}  "
              f"{result['win_rate']:>4.0%}  ${result['total_net_pnl']:>+8.2f}  "
              f"${result['avg_net_per_trade']:>+6.2f}  ${result['max_drawdown']:>6.2f}  "
              f"{'YES' if result['halted'] else 'no':>4s}")
    print()


if __name__ == "__main__":
    bars = load_bars(DATA_PATH)
    result = run_backtest(bars)
    print_report(result)
    run_sensitivity(bars)
