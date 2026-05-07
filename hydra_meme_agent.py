"""APEX Meme Engine — standalone competition-token trading agent.

Isolation guarantee: imports nothing from hydra_engine, hydra_agent,
hydra_brain, hydra_quant_rules, or hydra_pair_registry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
import websockets

# Load .env file if present (same loader as hydra_agent.py — no dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                    _v = _v[1:-1]
                if _v and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v


# ─── Constants ────────────────────────────────────────────────────────────────

WS_PORT = 8770
CANDLE_INTERVAL = 5          # minutes
WARMUP_BARS = 15
CANDLE_BUFFER_SIZE = 100
OBI_POLL_INTERVAL = 10       # seconds
COMPETITION_SCAN_INTERVAL = 900  # 15 minutes
KRAKEN_REST_FLOOR = 2.0      # seconds between CLI calls
RSI_PERIOD = 9
VOL_EMA_PERIOD = 10
OBI_ENTRY_THRESHOLD = 0.20
OBI_BOOK_FADE = -0.20
RSI_ENTRY_LOW = 45
RSI_ENTRY_HIGH = 78
RSI_EXHAUST = 82
VOLUME_SPIKE_MULTIPLIER = 1.8
VOLUME_DEATH_MULTIPLIER = 0.4
ASK_WALL_USD_LIMIT = 500.0
PROFIT_TARGET_PCT = 0.025    # 2.5%
HARD_STOP_PCT = -0.013       # -1.3%
TIME_STOP_CANDLES = 3
OBI_LEVELS = 5
TAKER_SLIPPAGE_BPS = 5       # 0.05% — limit at ask+0.05% for BUY
SLIPPAGE_CAP_BPS = 10        # 0.10% — reject if book moves more
SELL_MAX_RETRIES = 5         # abandon after N failed sell attempts

COMPETITION_ANOMALY_RATIO = 5.0
COMPETITION_EMA_ALPHA = 1 / 7

EXTENSION_MAX_PCT = 0.20  # block entry when price is >20% above slow EMA
REENTRY_COOLDOWN_BARS = 2  # bars to wait after exit before re-entering

COMPETITION_SEED_PAIRS = [
    # Meme tokens
    "WIF/USD", "POPCAT/USD", "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
    # Gaming / metaverse tokens
    "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
    # Newer ecosystem tokens Kraken actively promotes
    "NEAR/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
    "TIA/USD", "SEI/USD", "PYTH/USD",
]


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class CandleBar:
    ts: int           # Unix timestamp of bar open
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


@dataclass
class Position:
    entry_price: float
    qty: float
    notional_usd: float
    entry_ts: int
    candles_held: int = 0
    order_id: str = ""


@dataclass
class TradeRecord:
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees_usd: float
    net_pnl: float
    exit_reason: str
    hold_candles: int


# ─── Pure Indicator Functions ──────────────────────────────────────────────────

def wilder_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    """Wilder EMA RSI. Returns 50.0 when insufficient data (neutral)."""
    if len(closes) < period + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def vol_ema(values: list[float], period: int = VOL_EMA_PERIOD) -> float:
    """Standard EMA with alpha = 2/(period+1). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def compute_obi(
    bids: list[tuple],
    asks: list[tuple],
    levels: int = OBI_LEVELS,
) -> float:
    """Order Book Imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth).

    Each entry is (price, qty) as floats or strings. Returns 0.0 on empty book.
    """
    bid_depth = sum(float(p) * float(q) for p, q in bids[:levels])
    ask_depth = sum(float(p) * float(q) for p, q in asks[:levels])
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 0.0 else 0.0


def compute_vwap(bars: list[CandleBar]) -> float:
    """Close-price VWAP across all provided bars (close * volume weighted).

    Uses close price, not typical price (H+L+C)/3 — intentional for
    compatibility with Kraken OHLC candle format. Returns 0.0 for empty list.
    """
    total_pv = sum(b.close * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    return total_pv / total_v if total_v > 0.0 else 0.0


EMA_TREND_FAST = 8
EMA_TREND_SLOW = 21


def ema(values: list[float], period: int) -> float:
    """Standard EMA with alpha = 2/(period+1). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


# ─── Signal Engine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """Evaluates 5 entry gates and 6 exit triggers against candle history."""

    def __init__(self):
        self._bars: list[CandleBar] = []
        self._vwap_cum_pv: float = 0.0
        self._vwap_cum_v: float = 0.0

    def add_bar(self, bar: CandleBar) -> None:
        """Add a closed bar to the buffer. Trims to CANDLE_BUFFER_SIZE."""
        self._bars.append(bar)
        self._vwap_cum_pv += bar.close * bar.volume
        self._vwap_cum_v += bar.volume
        if len(self._bars) > CANDLE_BUFFER_SIZE:
            oldest = self._bars.pop(0)
            self._vwap_cum_pv -= oldest.close * oldest.volume
            self._vwap_cum_v -= oldest.volume

    def is_warmed_up(self) -> bool:
        return len(self._bars) >= WARMUP_BARS

    @property
    def session_vwap(self) -> float:
        return self._vwap_cum_pv / self._vwap_cum_v if self._vwap_cum_v > 0 else 0.0

    @property
    def current_rsi(self) -> float:
        closes = [b.close for b in self._bars]
        return wilder_rsi(closes)

    @property
    def vol_ema_baseline(self) -> float:
        volumes = [b.volume for b in self._bars]
        return vol_ema(volumes)

    def evaluate_entry_gates(
        self,
        latest_bar: CandleBar,
        obi: float,
        ask_wall_usd: float,
    ) -> dict:
        """Evaluate all 6 entry gates. Returns dict with gate booleans + all_pass."""
        vol_baseline = self.vol_ema_baseline
        # bar is already in self._bars (add_bar called before evaluate); don't duplicate
        closes = [b.close for b in self._bars]
        rsi = wilder_rsi(closes)
        vwap = self.session_vwap

        trend_aligned = True
        if len(self._bars) >= EMA_TREND_SLOW:
            ema_fast = ema(closes, EMA_TREND_FAST)
            ema_slow = ema(closes, EMA_TREND_SLOW)
            trend_aligned = ema_fast > ema_slow

        not_extended = True
        if len(self._bars) >= EMA_TREND_SLOW:
            ema_slow_val = ema(closes, EMA_TREND_SLOW)
            if ema_slow_val > 0:
                extension = (latest_bar.close - ema_slow_val) / ema_slow_val
                not_extended = extension <= EXTENSION_MAX_PCT

        gates = {
            "volume_spike": latest_bar.volume > VOLUME_SPIKE_MULTIPLIER * vol_baseline,
            "obi": obi > OBI_ENTRY_THRESHOLD,
            "vwap_align": latest_bar.close > vwap if vwap > 0 else False,
            "rsi_window": RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH,
            "ask_wall_clear": ask_wall_usd < ASK_WALL_USD_LIMIT,
            "trend_aligned": trend_aligned,
            "not_extended": not_extended,
            "rsi_value": round(rsi, 1),
            "vwap_value": round(vwap, 8),
            "vol_ema_value": round(vol_baseline, 2),
        }
        gates["all_pass"] = all(gates[k] for k in
                                ["volume_spike", "obi", "vwap_align", "rsi_window",
                                 "ask_wall_clear", "trend_aligned", "not_extended"])
        return gates

    def evaluate_exit_bar(self, position, latest_bar: CandleBar) -> Optional[str]:
        """Bar-close exit triggers: RSI exhaust, time stop, volume death.

        position is a Position dataclass. Returns exit reason string or None.
        """
        # bar is already in self._bars (add_bar called before evaluate); don't duplicate
        rsi = wilder_rsi([b.close for b in self._bars])
        if rsi > RSI_EXHAUST:
            return "rsi_exhaust"
        if position.candles_held >= TIME_STOP_CANDLES:
            return "time_stop"
        vol_baseline = self.vol_ema_baseline
        if vol_baseline > 0 and latest_bar.volume < VOLUME_DEATH_MULTIPLIER * vol_baseline:
            return "volume_death"
        return None

    def evaluate_exit_intracandle(
        self,
        position,
        mid_price: float,
        obi: float,
    ) -> Optional[str]:
        """10-second exit triggers: profit target, hard stop, book fade.

        position is a Position dataclass. Returns exit reason string or None.
        """
        pct_change = (mid_price - position.entry_price) / position.entry_price
        if pct_change >= PROFIT_TARGET_PCT:
            return "profit_target"
        if pct_change <= HARD_STOP_PCT:
            return "hard_stop"
        if obi < OBI_BOOK_FADE:
            return "book_fade"
        return None


# ─── Competition Detector ──────────────────────────────────────────────────────

class CompetitionDetector:
    """Monitors token volume baselines and detects competition anomalies."""

    def __init__(self, watchlist_path: str):
        self._path = watchlist_path
        self._lock = threading.Lock()
        self._data: dict = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path) as f:
                return json.load(f)
        data = {
            "tokens": [
                {
                    "pair": p,
                    "baseline_volume_7d": None,
                    "last_updated": None,
                    "competition_type": None,
                    "competition_type_confirmed": False,
                    "alert_suppressed_until": None,
                }
                for p in COMPETITION_SEED_PAIRS
            ],
            "last_scan": None,
        }
        self._save(data)
        return data

    def _save(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def _find_token(self, pair: str) -> Optional[dict]:
        for t in self._data["tokens"]:
            if t["pair"] == pair:
                return t
        return None

    def _find_or_add_token(self, pair: str) -> dict:
        token = self._find_token(pair)
        if token is None:
            token = {
                "pair": pair,
                "baseline_volume_7d": None,
                "last_updated": None,
                "competition_type": None,
                "competition_type_confirmed": False,
                "alert_suppressed_until": None,
            }
            self._data["tokens"].append(token)
        return token

    def _set_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["baseline_volume_7d"] = volume
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _get_baseline(self, pair: str) -> Optional[float]:
        token = self._find_token(pair)
        return token["baseline_volume_7d"] if token else None

    def _update_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            old = token["baseline_volume_7d"]
            if old is None:
                token["baseline_volume_7d"] = volume
            else:
                token["baseline_volume_7d"] = (
                    COMPETITION_EMA_ALPHA * volume + (1 - COMPETITION_EMA_ALPHA) * old
                )
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _is_anomaly(self, pair: str, current_volume: float) -> bool:
        baseline = self._get_baseline(pair)
        if baseline is None or baseline <= 0:
            return False
        return (current_volume / baseline) >= COMPETITION_ANOMALY_RATIO

    def _suppress(self, pair: str, until: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["alert_suppressed_until"] = until
            self._save(self._data)

    def _is_suppressed(self, pair: str) -> bool:
        token = self._find_token(pair)
        if token is None:
            return False
        until = token.get("alert_suppressed_until")
        return until is not None and time.time() < until

    def infer_competition_type(self, pair: str) -> str:
        """Volume-pattern heuristic. Returns 'volume', 'pnl', 'rebate', or 'unknown'."""
        token = self._find_token(pair)
        if token and token.get("competition_type_confirmed"):
            return token["competition_type"]
        baseline = self._get_baseline(pair)
        if baseline is None:
            return "unknown"
        return "volume"

    def get_all_tokens(self) -> list[dict]:
        return list(self._data.get("tokens", []))


# ─── Session State ─────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    pair: str = ""
    engine_state: str = "idle"   # idle | warmup | running | halted
    candle_buffer: list = field(default_factory=list)
    open_position: Optional[dict] = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trade_count: int = 0


def save_session(state: SessionState, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)


_journal_lock = threading.Lock()


def append_journal(record: TradeRecord, path: str) -> None:
    with _journal_lock:
        existing: list = []
        try:
            if os.path.exists(path):
                with open(path) as f:
                    existing = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[APEX] Warning: journal read failed, appending to fresh list: {e}")
        existing.append(asdict(record))
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[APEX] ERROR: journal write failed — trade record may be lost: {e}")


def load_journal(path: str) -> list[TradeRecord]:
    """Load trade records from journal file. Skips corrupt entries, keeps valid ones."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[APEX] Warning: could not load journal {path}: {e}")
        return []
    records: list[TradeRecord] = []
    for i, entry in enumerate(entries):
        try:
            records.append(TradeRecord(**entry))
        except (TypeError, KeyError) as e:
            print(f"[APEX] Warning: skipping corrupt journal entry {i}: {e}")
    return records


# ─── Kraken CLI ────────────────────────────────────────────────────────────────

_cli_lock = threading.Lock()
_cli_last_call: float = 0.0


def _kraken_cli(args: list[str], timeout: int = 20) -> dict:
    """Execute a kraken CLI command via WSL and return parsed JSON.

    All args are shlex-quoted to prevent injection (matches hydra_kraken_cli.py pattern).
    Global lock + 2s floor enforces rate limit across all concurrent callers.
    """
    global _cli_last_call
    with _cli_lock:
        now = time.time()
        wait = KRAKEN_REST_FLOOR - (now - _cli_last_call)
        if wait > 0:
            time.sleep(wait)
        _cli_last_call = time.time()
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    cmd_str = "source ~/.cargo/env"
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if api_key and api_secret:
        cmd_str += (f" && export KRAKEN_API_KEY={shlex.quote(api_key)}"
                    f" && export KRAKEN_API_SECRET={shlex.quote(api_secret)}")
    cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
    cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd_str]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        rc = result.returncode
        if not stdout:
            return {"error": f"Empty response (exit code {rc})"}
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data:
            return data
        if rc != 0:
            return {"error": f"Non-zero exit code {rc}", "partial": data}
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "retryable": True}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}


def _query_fill(txid: str) -> Optional[dict]:
    """Query order fill status via CLI. Returns {status, avg_price, vol_exec} or None."""
    if not txid:
        return None
    result = _kraken_cli(["query-orders", txid])
    if "error" in result:
        return None
    order_data = result.get(txid)
    if not order_data:
        order_data = next(iter(result.values()), None) if result else None
    if not order_data or not isinstance(order_data, dict):
        return None
    status = order_data.get("status", "")
    return {
        "status": "filled" if status == "closed" else status,
        "avg_price": float(order_data.get("price", 0)),
        "vol_exec": float(order_data.get("vol_exec", 0)),
    }


def _cancel_order(txid: str) -> dict:
    """Cancel a specific order by txid."""
    if not txid:
        return {"error": "no txid"}
    return _kraken_cli(["order", "cancel", txid, "--yes"])


# ─── Meme Executor ─────────────────────────────────────────────────────────────

TAKER_FEE_RATE = 0.004   # 0.40% taker fee on competition tokens


def _query_pair_precision(pair: str) -> tuple[int, int, float, float]:
    """Query Kraken for pair decimals. Returns (price_dec, lot_dec, ordermin, costmin)."""
    pair_nodash = pair.replace("/", "")
    result = _kraken_cli(["pairs", "--pair", pair_nodash])
    if "error" not in result:
        pdata = result.get(pair_nodash) or next(iter(result.values()), {})
        return (
            int(pdata.get("pair_decimals", 8)),
            int(pdata.get("lot_decimals", 8)),
            float(pdata.get("ordermin", 0)),
            float(pdata.get("costmin", 0)),
        )
    return (8, 8, 0.0, 0.0)


class MemeExecutor:
    """Places taker limit orders and tracks position + daily P&L."""

    def __init__(self, pair: str, position_size: float, daily_cap: float,
                 price_decimals: int = 8, lot_decimals: int = 8,
                 ordermin: float = 0.0, costmin: float = 0.0):
        if daily_cap <= 0:
            raise ValueError(f"daily_cap must be positive, got {daily_cap}")
        self.pair = pair
        self.position_size = position_size
        self.daily_cap = daily_cap
        self.price_decimals = price_decimals
        self.lot_decimals = lot_decimals
        self.ordermin = ordermin
        self.costmin = costmin
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._halted: bool = False
        self._last_reset_date: str = time.strftime("%Y-%m-%d", time.gmtime())
        self._pair_nodash = pair.replace("/", "")

    def is_halted(self) -> bool:
        return self._halted or self._daily_loss <= -self.daily_cap

    def record_pnl(self, net_pnl: float) -> None:
        self._daily_pnl += net_pnl
        if net_pnl < 0:
            self._daily_loss += net_pnl
        if self._daily_loss <= -self.daily_cap:
            self._halted = True

    def maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._last_reset_date:
            self._daily_pnl = 0.0
            self._daily_loss = 0.0
            self._halted = False
            self._last_reset_date = today

    def _buy_limit_price(self, ask: float) -> float:
        return ask * (1 + TAKER_SLIPPAGE_BPS / 10_000)

    def _sell_limit_price(self, bid: float) -> float:
        return bid * (1 - TAKER_SLIPPAGE_BPS / 10_000)

    def _buy_qty(self, ask: float) -> float:
        return self.position_size / ask

    def _compute_net_pnl(self, position: Position, exit_price: float) -> float:
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_notional = exit_price * position.qty
        exit_fee = exit_notional * TAKER_FEE_RATE
        return gross - entry_fee - exit_fee

    def place_buy(self, ask: float, mid: Optional[float] = None) -> Optional[Position]:
        """Place a taker BUY limit order. Returns Position on success, None on failure."""
        if self.is_halted():
            return None
        limit_price = self._buy_limit_price(ask)
        if mid and mid > 0:
            slippage_bps = (limit_price - mid) / mid * 10_000
            if slippage_bps > SLIPPAGE_CAP_BPS:
                return None
        qty = self._buy_qty(ask)
        pfmt = f"{{:.{self.price_decimals}f}}"
        qfmt = f"{{:.{self.lot_decimals}f}}"
        result = _kraken_cli([
            "order", "buy",
            self.pair,
            qfmt.format(qty),
            "--type", "limit",
            "--price", pfmt.format(limit_price),
            "--yes",
        ])
        if "error" in result:
            return None
        order_id = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        fill = _query_fill(str(order_id))
        if fill and fill["status"] == "filled" and fill["avg_price"] > 0:
            actual_price = fill["avg_price"]
            actual_qty = fill["vol_exec"] if fill["vol_exec"] > 0 else qty
        else:
            actual_price = limit_price
            actual_qty = qty
            if fill:
                print(f"[APEX] BUY fill check: status={fill['status']} — using limit price as estimate")
        return Position(
            entry_price=actual_price,
            qty=actual_qty,
            notional_usd=actual_price * actual_qty,
            entry_ts=int(time.time()),
            order_id=str(order_id),
        )

    def place_sell(self, position: Position, bid: float, reason: str,
                   mid: Optional[float] = None) -> Optional[dict]:
        """Place a taker SELL limit order. Returns trade record dict, or None on failure."""
        limit_price = self._sell_limit_price(bid)
        if mid and mid > 0:
            slippage_bps = (mid - limit_price) / mid * 10_000
            if slippage_bps > SLIPPAGE_CAP_BPS:
                limit_price = mid * (1 - SLIPPAGE_CAP_BPS / 10_000)
        pfmt = f"{{:.{self.price_decimals}f}}"
        qfmt = f"{{:.{self.lot_decimals}f}}"
        result = _kraken_cli([
            "order", "sell",
            self.pair,
            qfmt.format(position.qty),
            "--type", "limit",
            "--price", pfmt.format(limit_price),
            "--yes",
        ])
        if "error" in result:
            return None
        sell_txid = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        fill = _query_fill(str(sell_txid))
        if fill and fill["status"] == "filled" and fill["avg_price"] > 0:
            exit_price = fill["avg_price"]
        else:
            exit_price = limit_price
            if fill:
                print(f"[APEX] SELL fill check: status={fill['status']} — using limit price as estimate")
        net_pnl = self._compute_net_pnl(position, exit_price)
        self.record_pnl(net_pnl)
        gross = (exit_price - position.entry_price) * position.qty
        entry_fee = position.notional_usd * TAKER_FEE_RATE
        exit_fee = exit_price * position.qty * TAKER_FEE_RATE
        record = TradeRecord(
            entry_ts=position.entry_ts,
            exit_ts=int(time.time()),
            entry_price=position.entry_price,
            exit_price=exit_price,
            qty=position.qty,
            gross_pnl=gross,
            fees_usd=entry_fee + exit_fee,
            net_pnl=net_pnl,
            exit_reason=reason,
            hold_candles=position.candles_held,
        )
        return {"record": record, "order_result": result}


# ─── OBI Poller ────────────────────────────────────────────────────────────────

class OBIPoller:
    """Polls kraken orderbook every 10s and caches OBI + best bid/ask."""

    def __init__(self, pair: str):
        self.pair = pair
        self._pair_nodash = pair.replace("/", "")
        self._obi: float = 0.0
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0
        self._ask_wall: float = 999_999.0
        self._last_success: float = 0.0

    @property
    def is_stale(self) -> bool:
        """True if last successful poll was >60s ago."""
        return self._last_success == 0.0 or (time.time() - self._last_success > 60)

    def poll(self) -> None:
        """Fetch orderbook and update cached values."""
        result = _kraken_cli(["orderbook", self._pair_nodash, "--count", str(OBI_LEVELS)])
        if "error" in result:
            return
        # Kraken book response: {PAIR: {bids: [[price, qty, ts], ...], asks: [...]}}
        # Key may be Kraken-normalized (e.g. XBTUSDT) — fall back to first key.
        book = (result.get(self._pair_nodash)
                or result.get(self.pair)
                or (next(iter(result.values())) if result else {}))
        if not isinstance(book, dict):
            return
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            self._obi = compute_obi(
                [(b[0], b[1]) for b in bids],
                [(a[0], a[1]) for a in asks],
            )
            self._best_bid = float(bids[0][0])
            self._best_ask = float(asks[0][0])
            self._ask_wall = sum(float(a[0]) * float(a[1]) for a in asks[:3])
            self._last_success = time.time()

    @property
    def obi(self) -> float:
        return self._obi

    @property
    def mid_price(self) -> float:
        return (self._best_bid + self._best_ask) / 2 if self._best_bid else 0.0

    @property
    def best_bid(self) -> float:
        return self._best_bid

    @property
    def best_ask(self) -> float:
        return self._best_ask

    @property
    def ask_wall_usd(self) -> float:
        return self._ask_wall


# ─── Candle Aggregator ─────────────────────────────────────────────────────────

class CandleAggregator:
    """Subscribes to Kraken public WebSocket ohlc-5 channel.

    Fires on_bar callback with each newly closed CandleBar.
    Reconnects with exponential backoff on disconnect.
    """
    WS_URL = "wss://ws.kraken.com"

    def __init__(self, pair: str, on_bar):
        self.pair = pair          # e.g. "PLAY/USD"
        self._on_bar = on_bar
        self._last_etime: str = ""
        self._last_bar: Optional[CandleBar] = None
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        backoff = 5
        while self._running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    backoff = 5  # reset on successful connect
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": [self.pair],
                        "subscription": {"name": "ohlc", "interval": CANDLE_INTERVAL},
                    }))
                    async for raw in ws:
                        if not self._running:
                            return
                        self._handle(raw)
            except Exception:
                if not self._running:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        # Subscription confirmation / heartbeat — skip
        if not isinstance(msg, list) or len(msg) < 4:
            return
        channel_name = msg[2] if len(msg) > 2 else ""
        if not str(channel_name).startswith("ohlc"):
            return
        ohlc = msg[1]  # [time, etime, open, high, low, close, vwap, volume, count]
        if len(ohlc) < 9:
            return
        etime = str(ohlc[1])
        # New etime means previous bar closed; emit that bar
        if etime != self._last_etime and self._last_bar is not None:
            self._on_bar(self._last_bar)
        # Update running bar
        self._last_etime = etime
        self._last_bar = CandleBar(
            ts=int(float(ohlc[0])),
            open=float(ohlc[2]),
            high=float(ohlc[3]),
            low=float(ohlc[4]),
            close=float(ohlc[5]),
            vwap=float(ohlc[6]),
            volume=float(ohlc[7]),
            count=int(ohlc[8]),
        )


# ─── Meme Agent ────────────────────────────────────────────────────────────────

class MemeAgent:
    """Orchestrates all components and broadcasts state on port 8766."""

    def __init__(self, pair: str, position_size: float, daily_cap: float,
                 session_path: str = "hydra_meme_session.json",
                 journal_path: str = "hydra_meme_journal.json",
                 watchlist_path: str = "hydra_meme_watchlist.json"):
        self.pair = pair
        self._clients: set = set()
        self._signal_engine = SignalEngine()
        self._obi_poller = OBIPoller(pair)
        price_dec, lot_dec, ordermin, costmin = _query_pair_precision(pair)
        print(f"[APEX] {pair}: price_decimals={price_dec}  lot_decimals={lot_dec}  ordermin={ordermin}  costmin=${costmin}")
        self._executor = MemeExecutor(pair, position_size, daily_cap,
                                      price_dec, lot_dec, ordermin, costmin)
        self._detector = CompetitionDetector(watchlist_path)
        self._candle_agg = CandleAggregator(pair, self._on_bar)
        self._position: Optional[Position] = None
        self._sell_pending_reason: Optional[str] = None
        self._sell_retry_count: int = 0
        self._exit_lock: asyncio.Lock = asyncio.Lock()
        self._session_path = session_path
        self._journal_path = journal_path
        self._trade_log: list[TradeRecord] = load_journal(journal_path)
        if self._trade_log:
            for t in self._trade_log:
                self._executor.record_pnl(t.net_pnl)
            total_pnl = sum(t.net_pnl for t in self._trade_log)
            print(f"[APEX] Loaded {len(self._trade_log)} trades from journal (net P&L: ${total_pnl:+.2f})")
        self._engine_state = "warmup"
        self._last_exit_bar_count: int = -REENTRY_COOLDOWN_BARS
        self._bar_count: int = 0

    # ── History seed ──

    async def _seed_history(self) -> None:
        """Fetch recent 5-min candles via CLI to skip warmup wait."""
        pair_nodash = self.pair.replace("/", "")
        result = await asyncio.to_thread(
            _kraken_cli,
            ["ohlc", pair_nodash, "--interval", str(CANDLE_INTERVAL)],
        )
        if "error" in result:
            print(f"[APEX] History seed failed: {result.get('error')} — falling back to live warmup")
            return
        key = next((k for k in result if k != "last"), None)
        if not key:
            return
        raw_bars = result[key]
        # Take the most recent WARMUP_BARS bars (exclude the very last — it's still open)
        seed_count = CANDLE_BUFFER_SIZE
        closed = raw_bars[-(seed_count + 1):-1] if len(raw_bars) > seed_count else raw_bars[:-1]
        for b in closed:
            bar = CandleBar(
                ts=int(b[0]), open=float(b[1]), high=float(b[2]),
                low=float(b[3]), close=float(b[4]), vwap=float(b[5]),
                volume=float(b[6]), count=int(b[7]),
            )
            self._signal_engine.add_bar(bar)
        n = len(self._signal_engine._bars)
        print(f"[APEX] Seeded {n} historical bars — {'warmed up' if n >= WARMUP_BARS else f'{WARMUP_BARS - n} more needed'}")
        if self._signal_engine.is_warmed_up():
            self._engine_state = "running"

    # ── WebSocket server ──

    async def _ws_handler(self, websocket) -> None:
        self._clients.add(websocket)
        pos_data = None
        if self._position is not None:
            p = self._position
            pos_data = {"entry_price": p.entry_price, "qty": p.qty,
                        "notional_usd": p.notional_usd, "entry_ts": p.entry_ts,
                        "candles_held": p.candles_held}
        win_count = sum(1 for t in self._trade_log if t.net_pnl > 0)
        await websocket.send(json.dumps({
            "type": "initial_state",
            "engine_state": self._engine_state,
            "pair": self.pair,
            "bars_ready": len(self._signal_engine._bars),
            "bars_required": WARMUP_BARS,
            "position": pos_data,
            "session_pnl": self._executor._daily_pnl,
            "trade_count": len(self._trade_log),
            "daily_loss": self._executor._daily_loss,
            "win_rate": win_count / max(len(self._trade_log), 1),
            "trades": [{"entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
                        "entry_price": t.entry_price, "exit_price": t.exit_price,
                        "net_pnl": t.net_pnl, "exit_reason": t.exit_reason,
                        "hold_candles": t.hold_candles} for t in self._trade_log],
        }))
        bars = self._signal_engine._bars
        if bars:
            await websocket.send(json.dumps({
                "type": "candle_history",
                "bars": [{"ts": b.ts, "open": b.open, "high": b.high,
                           "low": b.low, "close": b.close, "volume": b.volume,
                           "vwap": b.vwap} for b in bars],
            }))
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "dismiss_alert":
                        pair = msg.get("pair", "")
                        if pair:
                            self._detector._suppress(pair, time.time() + 7200)
                    elif msg.get("type") == "stop_engine":
                        if self._position is not None:
                            await self._exit_position("manual_stop")
                        self._engine_state = "idle"
                        await self._broadcast({"type": "engine_state", "state": "idle"})
                    elif msg.get("type") == "scan_now":
                        asyncio.ensure_future(self._run_competition_scan())
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)

    async def _broadcast(self, msg: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(msg)
        await asyncio.gather(*[c.send(data) for c in list(self._clients)],
                             return_exceptions=True)

    # ── Bar callback (from CandleAggregator, scheduled into event loop) ──

    def _on_bar(self, bar: CandleBar) -> None:
        """Called by CandleAggregator; schedules async work into the event loop."""
        task = asyncio.ensure_future(self._handle_bar(bar))
        task.add_done_callback(self._task_error_cb)

    @staticmethod
    def _task_error_cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[APEX] Task error: {exc}")

    async def _handle_bar(self, bar: CandleBar) -> None:
        if os.environ.get("HYDRA_APEX_DISABLED") == "1":
            return
        self._signal_engine.add_bar(bar)
        self._bar_count += 1
        self._executor.maybe_reset_daily()
        # Broadcast bar so frontend chart updates on every close
        await self._broadcast({
            "type": "bar_update",
            "bar": {"ts": bar.ts, "open": bar.open, "high": bar.high,
                    "low": bar.low, "close": bar.close, "volume": bar.volume,
                    "vwap": bar.vwap},
        })
        if not self._signal_engine.is_warmed_up():
            self._engine_state = "warmup"
            await self._broadcast({
                "type": "warmup_progress",
                "bars_ready": len(self._signal_engine._bars),
                "bars_required": WARMUP_BARS,
            })
            return
        self._engine_state = "running"
        # Broadcast signal state
        gates = self._signal_engine.evaluate_entry_gates(
            bar, self._obi_poller.obi, self._obi_poller.ask_wall_usd
        )
        await self._broadcast({"type": "signal_state", "gates": gates,
                                "pair": self.pair, "ts": bar.ts})
        # Bar-close exit check
        if self._position is not None:
            self._position.candles_held += 1
            reason = self._signal_engine.evaluate_exit_bar(self._position, bar)
            if reason:
                await self._exit_position(reason)
                return
        # Entry check (only when no position, no pending sell, and OBI data is fresh)
        if (self._position is None and not self._executor.is_halted()
                and not self._sell_pending_reason and not self._obi_poller.is_stale
                and (self._bar_count - self._last_exit_bar_count) >= REENTRY_COOLDOWN_BARS):
            if gates["all_pass"]:
                mid = self._obi_poller.mid_price or None
                pos = await asyncio.to_thread(
                    self._executor.place_buy, self._obi_poller.best_ask, mid
                )
                if pos:
                    self._position = pos
                    await self._broadcast({"type": "order_placed",
                                           "side": "buy",
                                           "price": pos.entry_price,
                                           "qty": pos.qty})

    async def _exit_position(self, reason: str) -> None:
        """Exit current position. Lock prevents double-exit from concurrent OBI/bar tasks."""
        async with self._exit_lock:
            if self._position is None:
                return
            result = await asyncio.to_thread(
                self._executor.place_sell,
                self._position, self._obi_poller.best_bid, reason,
                self._obi_poller.mid_price or None,
            )
            if result is None:
                self._sell_retry_count += 1
                if self._sell_retry_count >= SELL_MAX_RETRIES:
                    print(f"[APEX] SELL FAILED after {SELL_MAX_RETRIES} retries — "
                          f"abandoning auto-sell for {self.pair} "
                          f"(qty={self._position.qty}, entry={self._position.entry_price})")
                    print(f"[APEX] WARNING: position remains open on exchange — close manually")
                    self._sell_pending_reason = None
                    self._sell_retry_count = 0
                    await self._broadcast({"type": "sell_abandoned",
                                           "reason": reason, "pair": self.pair,
                                           "retries": SELL_MAX_RETRIES})
                else:
                    self._sell_pending_reason = reason
                    await self._broadcast({"type": "sell_failed", "reason": reason,
                                           "pair": self.pair,
                                           "retry": self._sell_retry_count})
                return
            record: TradeRecord = result["record"]
            self._trade_log.append(record)
            await asyncio.to_thread(append_journal, record, self._journal_path)
            self._position = None
            self._sell_pending_reason = None
            self._sell_retry_count = 0
            self._last_exit_bar_count = self._bar_count
        await self._broadcast({"type": "trade_closed",
                               "net_pnl": record.net_pnl,
                               "exit_reason": reason,
                               "exit_ts": record.exit_ts,
                               "entry_price": record.entry_price,
                               "exit_price": record.exit_price,
                               "hold_candles": record.hold_candles})
        if self._executor.is_halted():
            self._engine_state = "halted"
            await self._broadcast({"type": "engine_halted",
                                   "reason": "daily_cap",
                                   "daily_pnl": self._executor._daily_pnl})
        win_count = sum(1 for t in self._trade_log if t.net_pnl > 0)
        await self._broadcast({
            "type": "session_stats",
            "session_pnl": self._executor._daily_pnl,
            "daily_loss": self._executor._daily_loss,
            "trade_count": len(self._trade_log),
            "win_rate": win_count / max(len(self._trade_log), 1),
            "daily_cap_remaining": self._executor.daily_cap + self._executor._daily_loss,
        })
        state = SessionState(
            pair=self.pair,
            engine_state=self._engine_state,
            session_pnl=self._executor._daily_pnl,
            daily_pnl=self._executor._daily_pnl,
            trade_count=len(self._trade_log),
        )
        await asyncio.to_thread(save_session, state, self._session_path)

    # ── 10-second OBI loop ──

    async def _obi_loop(self) -> None:
        while True:
            try:
                if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                    self._engine_state = "halted"
                    await self._broadcast({"type": "engine_halted", "reason": "kill_switch"})
                    await asyncio.sleep(OBI_POLL_INTERVAL)
                    continue
                await asyncio.to_thread(self._obi_poller.poll)
                mid = self._obi_poller.mid_price
                # Retry failed sell with fresh bid data
                if self._sell_pending_reason and self._position is not None and mid > 0:
                    await self._exit_position(self._sell_pending_reason)
                if self._position is not None and self._engine_state == "running" and mid > 0:
                    reason = self._signal_engine.evaluate_exit_intracandle(
                        self._position, mid, self._obi_poller.obi
                    )
                    if reason:
                        await self._exit_position(reason)
                bid = self._obi_poller.best_bid
                ask = self._obi_poller.best_ask
                spread_bps = ((ask - bid) / mid * 10_000) if mid > 0 else 0.0
                if self._position is not None:
                    pos = self._position
                    await self._broadcast({
                        "type": "position_update",
                        "price": mid,
                        "obi": self._obi_poller.obi,
                        "spread_bps": round(spread_bps, 1),
                        "entry": {
                            "entry_price": pos.entry_price,
                            "qty": pos.qty,
                            "candles_held": pos.candles_held,
                            "notional_usd": pos.notional_usd,
                            "entry_ts": pos.entry_ts,
                        },
                        "unrealised_pnl": (mid - pos.entry_price) * pos.qty if mid > 0 else 0.0,
                    })
                else:
                    await self._broadcast({
                        "type": "ticker",
                        "price": mid,
                        "obi": self._obi_poller.obi,
                        "spread_bps": round(spread_bps, 1),
                    })
            except Exception as e:
                print(f"[APEX] OBI loop error: {e}")
            await asyncio.sleep(OBI_POLL_INTERVAL)

    async def _run_competition_scan(self) -> None:
        """Single competition scan pass: fetch ticker for all watchlist tokens."""
        tokens = self._detector.get_all_tokens()
        await self._broadcast({"type": "scan_started", "token_count": len(tokens)})
        last_call = 0.0
        for token in tokens:
            elapsed = time.time() - last_call
            if elapsed < KRAKEN_REST_FLOOR:
                await asyncio.sleep(KRAKEN_REST_FLOOR - elapsed)
            pair_nodash = token["pair"].replace("/", "")
            result = await asyncio.to_thread(_kraken_cli, ["ticker", pair_nodash])
            last_call = time.time()
            if "error" in result:
                continue
            # Kraken ticker key may be normalized — fall back to first key.
            ticker_data = (result.get(pair_nodash)
                           or result.get(token["pair"])
                           or (next(iter(result.values())) if result else {}))
            if not isinstance(ticker_data, dict):
                continue
            # Kraken ticker: v[1] = 24h volume
            vol_str = ticker_data.get("v", [None, None])[1]
            if not vol_str:
                continue
            volume = float(vol_str)
            first_scan = self._detector._get_baseline(token["pair"]) is None
            if first_scan:
                self._detector._set_baseline(token["pair"], volume)
                baseline = volume
                ratio = 1.0
                is_anomaly = False
            else:
                baseline = self._detector._get_baseline(token["pair"])
                ratio = volume / baseline if baseline else 0.0
                is_anomaly = self._detector._is_anomaly(token["pair"], volume)
                self._detector._update_baseline(token["pair"], volume)
            # Store live data on token dict for watchlist_update
            with self._detector._lock:
                t = self._detector._find_token(token["pair"])
                if t is not None:
                    t["current_volume"] = volume
                    t["anomaly_ratio"] = ratio
            comp_type = self._detector.infer_competition_type(token["pair"])
            token_obj = self._detector._find_token(token["pair"]) or {}
            # Broadcast individual token immediately — don't make frontend wait 36 s
            await self._broadcast({
                "type": "token_update",
                "pair": token["pair"],
                "current_volume": volume,
                "baseline_volume_7d": baseline,
                "anomaly_ratio": ratio,
                "competition_type": comp_type,
                "competition_type_confirmed": token_obj.get("competition_type_confirmed", False),
            })
            if (is_anomaly
                    and not self._detector._is_suppressed(token["pair"])):
                await self._broadcast({
                    "type": "competition_alert",
                    "pair": token["pair"],
                    "volume": volume,
                    "baseline": baseline,
                    "ratio": ratio,
                    "competition_type": comp_type,
                    "competition_type_confirmed": token_obj.get("competition_type_confirmed", False),
                })
        # Final authoritative snapshot after full scan
        await self._broadcast({
            "type": "watchlist_update",
            "tokens": self._detector.get_all_tokens(),
        })

    # ── 15-minute competition scan loop ──

    async def _competition_loop(self) -> None:
        await self._run_competition_scan()
        while True:
            await asyncio.sleep(COMPETITION_SCAN_INTERVAL)
            if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                continue
            await self._run_competition_scan()

    # ── Test fire ──

    async def _test_fire(self) -> bool:
        """Execute one BUY→SELL cycle to verify the full pipeline.

        Queries pair minimums from Kraken to ensure the order clears both
        ordermin (token qty) and costmin (USD notional).
        """
        ex = self._executor
        ordermin = ex.ordermin
        costmin = ex.costmin
        pfmt = f"{{:.{ex.price_decimals}f}}"
        qfmt = f"{{:.{ex.lot_decimals}f}}"
        print(f"[APEX] TEST-FIRE: starting round-trip on {self.pair}")
        print(f"[APEX] TEST-FIRE: ordermin={ordermin}  costmin=${costmin}  price_dec={ex.price_decimals}  lot_dec={ex.lot_decimals}")
        print("[APEX] TEST-FIRE: polling orderbook for fresh bid/ask...")
        for attempt in range(6):
            await asyncio.to_thread(self._obi_poller.poll)
            if self._obi_poller.best_ask > 0:
                break
            print(f"[APEX] TEST-FIRE: no book data yet (attempt {attempt + 1}/6)")
            await asyncio.sleep(3)
        ask = self._obi_poller.best_ask
        bid = self._obi_poller.best_bid
        mid = self._obi_poller.mid_price
        obi = self._obi_poller.obi
        if ask <= 0 or bid <= 0:
            print("[APEX] TEST-FIRE: FAILED — could not get orderbook data")
            return False
        print(f"[APEX] TEST-FIRE: ask={ask:.8f}  bid={bid:.8f}  mid={mid:.8f}  OBI={obi:.4f}")
        # Compute qty that clears both minimums with 20% headroom
        qty_from_min = ordermin * 1.2
        qty_from_cost = (costmin * 1.2) / ask
        qty = max(qty_from_min, qty_from_cost)
        test_notional = qty * ask
        limit_buy = ask * (1 + TAKER_SLIPPAGE_BPS / 10_000)
        print(f"[APEX] TEST-FIRE: placing BUY  qty={qfmt.format(qty)}  limit={pfmt.format(limit_buy)}  (~${test_notional:.2f})")
        buy_result = await asyncio.to_thread(
            _kraken_cli,
            ["order", "buy", self.pair, qfmt.format(qty),
             "--type", "limit", "--price", pfmt.format(limit_buy), "--yes"],
        )
        if "error" in buy_result:
            print(f"[APEX] TEST-FIRE: BUY FAILED — {buy_result}")
            return False
        txid = buy_result.get("txid", "?")
        print(f"[APEX] TEST-FIRE: BUY OK — txid={txid}")
        await self._broadcast({"type": "order_placed", "side": "buy",
                                "price": limit_buy, "qty": qty,
                                "test_fire": True})
        # Wait for fill
        await asyncio.sleep(3)
        # SELL
        await asyncio.to_thread(self._obi_poller.poll)
        bid = self._obi_poller.best_bid
        limit_sell = bid * (1 - TAKER_SLIPPAGE_BPS / 10_000)
        print(f"[APEX] TEST-FIRE: placing SELL qty={qfmt.format(qty)}  limit={pfmt.format(limit_sell)}")
        sell_result = await asyncio.to_thread(
            _kraken_cli,
            ["order", "sell", self.pair, qfmt.format(qty),
             "--type", "limit", "--price", pfmt.format(limit_sell), "--yes"],
        )
        if "error" in sell_result:
            print(f"[APEX] TEST-FIRE: SELL FAILED — {sell_result}")
            print("[APEX] TEST-FIRE: WARNING — position still open on exchange — close manually")
            return False
        gross = (limit_sell - limit_buy) * qty
        fees = test_notional * TAKER_FEE_RATE * 2
        net = gross - fees
        print(f"[APEX] TEST-FIRE: SELL OK — txid={sell_result.get('txid', '?')}")
        print(f"[APEX] TEST-FIRE: gross={gross:+.6f}  fees={fees:.6f}  net={net:+.6f}")
        print("[APEX] TEST-FIRE: PASS — full pipeline verified — continuing normal operation")
        record = TradeRecord(
            entry_ts=int(time.time()) - 3,
            exit_ts=int(time.time()),
            entry_price=limit_buy,
            exit_price=limit_sell,
            qty=qty,
            gross_pnl=gross,
            fees_usd=fees,
            net_pnl=net,
            exit_reason="test_fire",
            hold_candles=0,
        )
        self._trade_log.append(record)
        await asyncio.to_thread(append_journal, record, self._journal_path)
        await self._broadcast({"type": "trade_closed", "net_pnl": net,
                                "exit_reason": "test_fire",
                                "exit_ts": record.exit_ts,
                                "entry_price": limit_buy,
                                "exit_price": limit_sell,
                                "hold_candles": 0,
                                "test_fire": True})
        return True

    # ── Main run ──

    async def run(self, test_fire: bool = False) -> None:
        if os.environ.get("HYDRA_APEX_DISABLED") == "1":
            print("[APEX] Kill switch HYDRA_APEX_DISABLED=1 — not starting")
            return
        await self._seed_history()
        server = await websockets.serve(self._ws_handler, "127.0.0.1", WS_PORT)
        print(f"[APEX] WebSocket server on ws://localhost:{WS_PORT}")
        if test_fire:
            await self._test_fire()
        print(f"[APEX] Trading {self.pair} | State: {self._engine_state}")
        try:
            results = await asyncio.gather(
                self._candle_agg.run(),
                self._obi_loop(),
                self._competition_loop(),
                return_exceptions=True,
            )
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    task_names = ["candle_agg", "obi_loop", "competition_loop"]
                    print(f"[APEX] Task {task_names[i]} crashed: {r}")
        except asyncio.CancelledError:
            print("[APEX] Shutting down...")
        finally:
            self._candle_agg.stop()
            if self._position is not None:
                print(f"[APEX] Shutdown with open position — attempting exit sell")
                try:
                    await asyncio.to_thread(self._obi_poller.poll)
                    if self._obi_poller.best_bid > 0:
                        await self._exit_position("shutdown")
                except Exception as e:
                    print(f"[APEX] Shutdown sell failed: {e}")
                if self._position is not None:
                    if self._position.order_id:
                        try:
                            await asyncio.to_thread(_cancel_order, self._position.order_id)
                        except Exception:
                            pass
                    print(f"[APEX] WARNING: open position remains — "
                          f"{self._position.qty} {self.pair} @ entry "
                          f"{self._position.entry_price} — close manually on Kraken")
            open_pos = None
            if self._position is not None:
                p = self._position
                open_pos = {"entry_price": p.entry_price, "qty": p.qty,
                            "notional_usd": p.notional_usd, "entry_ts": p.entry_ts,
                            "order_id": p.order_id}
            save_session(SessionState(
                pair=self.pair, engine_state="idle",
                session_pnl=self._executor._daily_pnl,
                daily_pnl=self._executor._daily_pnl,
                trade_count=len(self._trade_log),
                open_position=open_pos,
            ), self._session_path)
            server.close()
            await server.wait_closed()
            print("[APEX] Shutdown complete")


# ─── Entry Point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="APEX Meme Engine")
    p.add_argument("--pair", required=True, help="Trading pair e.g. PLAY/USD")
    p.add_argument("--position-size", type=float, default=300.0)
    p.add_argument("--daily-cap", type=float, default=30.0)
    p.add_argument("--session-path", default="hydra_meme_session.json")
    p.add_argument("--journal-path", default="hydra_meme_journal.json")
    p.add_argument("--watchlist-path", default="hydra_meme_watchlist.json")
    p.add_argument("--test-fire", action="store_true",
                   help="Execute one $5 BUY→SELL cycle on startup to verify pipeline")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if not os.environ.get("KRAKEN_API_KEY") or not os.environ.get("KRAKEN_API_SECRET"):
        print("[APEX] WARNING: KRAKEN_API_KEY/SECRET not found — orders will fail")
    agent = MemeAgent(
        pair=args.pair,
        position_size=args.position_size,
        daily_cap=args.daily_cap,
        session_path=args.session_path,
        journal_path=args.journal_path,
        watchlist_path=args.watchlist_path,
    )
    asyncio.run(agent.run(test_fire=args.test_fire))


if __name__ == "__main__":
    main()
