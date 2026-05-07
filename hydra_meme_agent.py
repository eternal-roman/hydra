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


# ─── Constants ────────────────────────────────────────────────────────────────

WS_PORT = 8766
CANDLE_INTERVAL = 5          # minutes
WARMUP_BARS = 15
CANDLE_BUFFER_SIZE = 20
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

COMPETITION_ANOMALY_RATIO = 5.0
COMPETITION_EMA_ALPHA = 1 / 7

COMPETITION_SEED_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOT/USD", "LINK/USD", "AVAX/USD", "ATOM/USD", "NEAR/USD",
    "FIL/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
    "TIA/USD", "SEI/USD", "PYTH/USD", "WIF/USD", "POPCAT/USD",
    "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
    "MATIC/USD", "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
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
        """Evaluate all 5 entry gates. Returns dict with gate booleans + all_pass."""
        vol_baseline = self.vol_ema_baseline
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
        vwap = self.session_vwap

        gates = {
            "volume_spike": latest_bar.volume > VOLUME_SPIKE_MULTIPLIER * vol_baseline,
            "obi": obi > OBI_ENTRY_THRESHOLD,
            "vwap_align": latest_bar.close > vwap if vwap > 0 else False,
            "rsi_window": RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH,
            "ask_wall_clear": ask_wall_usd < ASK_WALL_USD_LIMIT,
            "rsi_value": round(rsi, 1),
            "vwap_value": round(vwap, 8),
            "vol_ema_value": round(vol_baseline, 2),
        }
        gates["all_pass"] = all(gates[k] for k in
                                ["volume_spike", "obi", "vwap_align", "rsi_window", "ask_wall_clear"])
        return gates

    def evaluate_exit_bar(self, position, latest_bar: CandleBar) -> Optional[str]:
        """Bar-close exit triggers: RSI exhaust, time stop, volume death.

        position is a Position dataclass. Returns exit reason string or None.
        """
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
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


def load_session(path: str) -> SessionState:
    with open(path) as f:
        data = json.load(f)
    return SessionState(**{k: v for k, v in data.items() if k in SessionState.__dataclass_fields__})


def append_journal(record: TradeRecord, path: str) -> None:
    existing: list = []
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing.append(asdict(record))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, path)


# ─── Kraken CLI ────────────────────────────────────────────────────────────────

def _kraken_cli(args: list[str], timeout: int = 20) -> dict:
    """Execute a kraken CLI command via WSL and return parsed JSON.

    All args are shlex-quoted to prevent injection (matches hydra_kraken_cli.py pattern).
    """
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
        if not stdout:
            return {"error": f"Empty response (exit {result.returncode})"}
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data:
            return data
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "retryable": True}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}


# ─── Meme Executor ─────────────────────────────────────────────────────────────

TAKER_FEE_RATE = 0.004   # 0.40% taker fee on competition tokens


class MemeExecutor:
    """Places taker limit orders and tracks position + daily P&L."""

    def __init__(self, pair: str, position_size: float, daily_cap: float):
        if daily_cap <= 0:
            raise ValueError(f"daily_cap must be positive, got {daily_cap}")
        self.pair = pair
        self.position_size = position_size
        self.daily_cap = daily_cap
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._halted: bool = False
        self._pair_nodash = pair.replace("/", "")

    def is_halted(self) -> bool:
        return self._halted or self._daily_loss <= -self.daily_cap

    def record_pnl(self, net_pnl: float) -> None:
        self._daily_pnl += net_pnl
        if net_pnl < 0:
            self._daily_loss += net_pnl
        if self._daily_loss <= -self.daily_cap:
            self._halted = True

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
        result = _kraken_cli([
            "order", "buy",
            self.pair,
            f"{qty:.8f}",
            "--type", "limit",
            "--price", f"{limit_price:.8f}",
            "--yes",
        ])
        if "error" in result:
            return None
        order_id = result.get("txid", [""])[0] if isinstance(result.get("txid"), list) else result.get("txid", "")
        return Position(
            entry_price=limit_price,
            qty=qty,
            notional_usd=self.position_size,
            entry_ts=int(time.time()),
            order_id=str(order_id),
        )

    def place_sell(self, position: Position, bid: float, reason: str,
                   mid: Optional[float] = None) -> dict:
        """Place a taker SELL limit order. Returns trade record dict."""
        limit_price = self._sell_limit_price(bid)
        if mid and mid > 0:
            slippage_bps = (mid - limit_price) / mid * 10_000
            if slippage_bps > SLIPPAGE_CAP_BPS:
                limit_price = mid * (1 - SLIPPAGE_CAP_BPS / 10_000)
        result = _kraken_cli([
            "order", "sell",
            self.pair,
            f"{position.qty:.8f}",
            "--type", "limit",
            "--price", f"{limit_price:.8f}",
            "--yes",
        ])
        exit_price = limit_price  # assume fill at limit
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
        self._last_poll: float = 0.0

    def poll(self) -> None:
        """Fetch orderbook and update cached values. Enforces 2s floor."""
        now = time.time()
        if now - self._last_poll < KRAKEN_REST_FLOOR:
            return
        self._last_poll = now
        result = _kraken_cli(["book", self._pair_nodash, "--depth", str(OBI_LEVELS)])
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

    def ask_wall_usd(self) -> float:
        """Compute top-3 ask levels total USD depth (for ask_wall_clear gate)."""
        result = _kraken_cli(["book", self._pair_nodash, "--depth", "3"])
        if "error" in result:
            return 999_999.0
        book = (result.get(self._pair_nodash)
                or result.get(self.pair)
                or (next(iter(result.values())) if result else {}))
        if not isinstance(book, dict):
            return 999_999.0
        asks = book.get("asks", [])
        return sum(float(a[0]) * float(a[1]) for a in asks[:3])


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
        self._executor = MemeExecutor(pair, position_size, daily_cap)
        self._detector = CompetitionDetector(watchlist_path)
        self._candle_agg = CandleAggregator(pair, self._on_bar)
        self._position: Optional[Position] = None
        self._exit_lock: asyncio.Lock = asyncio.Lock()
        self._session_path = session_path
        self._journal_path = journal_path
        self._trade_log: list[TradeRecord] = []
        self._engine_state = "warmup"

    # ── WebSocket server ──

    async def _ws_handler(self, websocket) -> None:
        self._clients.add(websocket)
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "dismiss_alert":
                        pair = msg.get("pair", "")
                        if pair:
                            self._detector._suppress(pair, time.time() + 7200)
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

    def _broadcast_sync(self, msg: dict) -> None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._broadcast(msg))
        except Exception:
            pass

    # ── Bar callback (from CandleAggregator, scheduled into event loop) ──

    def _on_bar(self, bar: CandleBar) -> None:
        """Called by CandleAggregator; schedules async work into the event loop."""
        asyncio.ensure_future(self._handle_bar(bar))

    async def _handle_bar(self, bar: CandleBar) -> None:
        self._signal_engine.add_bar(bar)
        if not self._signal_engine.is_warmed_up():
            self._engine_state = "warmup"
            return
        self._engine_state = "running"
        # Broadcast signal state
        gates = self._signal_engine.evaluate_entry_gates(
            bar, self._obi_poller.obi, await asyncio.to_thread(self._obi_poller.ask_wall_usd)
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
        # Entry check (only when no position)
        if self._position is None and not self._executor.is_halted():
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
            result = self._executor.place_sell(
                self._position, self._obi_poller.best_bid, reason,
                mid=self._obi_poller.mid_price or None,
            )
            record: TradeRecord = result["record"]
            self._trade_log.append(record)
            await asyncio.to_thread(append_journal, record, self._journal_path)
            self._position = None
        await self._broadcast({"type": "trade_closed",
                               "net_pnl": record.net_pnl,
                               "exit_reason": reason,
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
            if os.environ.get("HYDRA_APEX_DISABLED") == "1":
                self._engine_state = "halted"
                await self._broadcast({"type": "engine_halted", "reason": "kill_switch"})
                return
            await asyncio.to_thread(self._obi_poller.poll)
            if self._position is not None and self._engine_state == "running":
                reason = self._signal_engine.evaluate_exit_intracandle(
                    self._position, self._obi_poller.mid_price, self._obi_poller.obi
                )
                if reason:
                    await self._exit_position(reason)
            if self._position is not None:
                pos = self._position
                await self._broadcast({
                    "type": "position_update",
                    "price": self._obi_poller.mid_price,
                    "obi": self._obi_poller.obi,
                    "entry": {
                        "entry_price": pos.entry_price,
                        "qty": pos.qty,
                        "candles_held": pos.candles_held,
                        "notional_usd": pos.notional_usd,
                        "entry_ts": pos.entry_ts,
                    },
                    "unrealised_pnl": (self._obi_poller.mid_price - pos.entry_price) * pos.qty,
                })
            await asyncio.sleep(OBI_POLL_INTERVAL)

    async def _run_competition_scan(self) -> None:
        """Single competition scan pass: fetch ticker for all watchlist tokens."""
        tokens = self._detector.get_all_tokens()
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
            if self._detector._get_baseline(token["pair"]) is None:
                self._detector._set_baseline(token["pair"], volume)
                continue
            self._detector._update_baseline(token["pair"], volume)
            if (not self._detector._is_suppressed(token["pair"])
                    and self._detector._is_anomaly(token["pair"], volume)):
                comp_type = self._detector.infer_competition_type(token["pair"])
                token_obj = self._detector._find_token(token["pair"]) or {}
                await self._broadcast({
                    "type": "competition_alert",
                    "pair": token["pair"],
                    "volume": volume,
                    "baseline": self._detector._get_baseline(token["pair"]),
                    "ratio": volume / self._detector._get_baseline(token["pair"]),
                    "competition_type": comp_type,
                    "competition_type_confirmed": token_obj.get("competition_type_confirmed", False),
                })
        # Broadcast full token list after scan
        await self._broadcast({
            "type": "watchlist_update",
            "tokens": self._detector.get_all_tokens(),
        })

    # ── 15-minute competition scan loop ──

    async def _competition_loop(self) -> None:
        # Run one scan immediately on startup so Discover view is populated
        await self._run_competition_scan()
        while True:
            await asyncio.sleep(COMPETITION_SCAN_INTERVAL)
            await self._run_competition_scan()

    # ── Main run ──

    async def run(self) -> None:
        server = await websockets.serve(self._ws_handler, "localhost", WS_PORT)
        print(f"[APEX] WebSocket server on ws://localhost:{WS_PORT}")
        print(f"[APEX] Trading {self.pair} | Warmup: {WARMUP_BARS} bars ({WARMUP_BARS * CANDLE_INTERVAL} min)")
        try:
            await asyncio.gather(
                self._candle_agg.run(),
                self._obi_loop(),
                self._competition_loop(),
            )
        finally:
            server.close()
            await server.wait_closed()


# ─── Entry Point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="APEX Meme Engine")
    p.add_argument("--pair", required=True, help="Trading pair e.g. PLAY/USD")
    p.add_argument("--position-size", type=float, default=600.0)
    p.add_argument("--daily-cap", type=float, default=30.0)
    p.add_argument("--session-path", default="hydra_meme_session.json")
    p.add_argument("--journal-path", default="hydra_meme_journal.json")
    p.add_argument("--watchlist-path", default="hydra_meme_watchlist.json")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    agent = MemeAgent(
        pair=args.pair,
        position_size=args.position_size,
        daily_cap=args.daily_cap,
        session_path=args.session_path,
        journal_path=args.journal_path,
        watchlist_path=args.watchlist_path,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
