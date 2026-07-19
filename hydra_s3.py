"""HYDRA adapter for the s3bounce package — S3 daily bounce signal
surface + (env-gated) shadow strategy phase.

The algorithm itself lives in the standalone `s3bounce/` package folder
(stdlib-only; imported here by path — the standard consumption route).
This module adapts it to the agent:

  * daily bars are built from data the agent already fetches: the boot
    daily seed (Kraken 1440m OHLC) + the live candle feed; universe
    members not among the running pairs are seeded/refreshed once per
    UTC day via the kraken CLI (signal input only, "no REST for market
    data" compliant);
  * candle folding is CONFIRMATION-BASED: an incoming candle is folded
    into its UTC day only when a candle with a later timestamp arrives
    (streams re-push the in-progress bar; folding early would corrupt
    the daily close). The strategy's clock advances with folded data,
    never wall time, so a stalled stream can never fabricate a
    completed day;
  * every public method is inert on error (logs once, returns an
    inactive block) — the signal surface must never take down a tick;
  * `HYDRA_S3_DISABLED=1` (read per call) removes the surface entirely.

Shadow phase (`HYDRA_S3_STRATEGY=1`): gated entryable_b1 signals become
proposals in the `.hydra-s3/` ledger with per-exit-arm paper positions.
There is NO code path from this module to an order — enforced by
tests/test_s3_shadow.py's grep guard.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_S3_DIR = str(Path(__file__).resolve().parent / "s3bounce")
if _S3_DIR not in sys.path:
    sys.path.insert(0, _S3_DIR)

from s3bounce import S3Strategy, ShadowLedger, load_artifact  # noqa: E402
from s3bounce.strategy import S3Signal  # noqa: E402

from hydra_pair_registry import normalize_asset  # noqa: E402

logger = logging.getLogger("hydra.s3")

UNIVERSE = ("BTC/USD", "ETH/USD", "ZEC/USD")
LEDGER_DIR = ".hydra-s3"
CONFIRMER_STATUS_DIR = os.environ.get("HYDRA_S3_HEARTBEAT_STATUS_DIR",
                                      str(Path("heartbeat") / "data"))
CONFIRMER_STALE_S = 300.0


def _canonical(pair: str) -> Optional[str]:
    """Agent pair name -> universe asset name via registry normalization
    (XBT->BTC, ZUSD->USD, case, slashless), never a local alias dict."""
    try:
        if "/" in pair:
            base, quote = pair.split("/", 1)
        else:
            return None
        canon = f"{normalize_asset(base)}/{normalize_asset(quote)}"
        return canon if canon in UNIVERSE else None
    except Exception:
        return None


class S3Adapter:
    def __init__(self, pairs: List[str], interval_min: int,
                 ledger_dir: str = LEDGER_DIR):
        self.interval_s = int(interval_min) * 60
        self.asset_by_pair: Dict[str, str] = {}
        for p in pairs:
            a = _canonical(p)
            if a is not None:
                self.asset_by_pair[p] = a
        self.strategy = S3Strategy(load_artifact())
        self.ledger_dir = ledger_dir
        self._ledger: Optional[ShadowLedger] = None
        self._pending: Dict[str, dict] = {}       # asset -> unconfirmed candle
        self._fold_clock: Dict[str, float] = {}   # asset -> data-time "now"
        self._member_seed_day: Dict[str, int] = {}
        self._error_logged = False
        self._last_signal: Dict[str, S3Signal] = {}

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def disabled() -> bool:
        return os.environ.get("HYDRA_S3_DISABLED") == "1"

    @staticmethod
    def shadow_enabled() -> bool:
        return os.environ.get("HYDRA_S3_STRATEGY") == "1"

    def _inert(self, where: str, e: Exception) -> None:
        if not self._error_logged:
            logger.warning("S3 surface inert after error in %s: %r", where, e)
            self._error_logged = True

    def _running_assets(self) -> set:
        return set(self.asset_by_pair.values())

    # ---- boot seeding -----------------------------------------------------
    def seed_boot(self, pair: str, daily_rows: List[dict],
                  intraday: List[dict]) -> None:
        """Seed one running pair from the warmup fetches the agent already
        performed: daily 1440m rows + recent intraday candles (all but
        the newest fold immediately; the newest stays pending)."""
        if self.disabled():
            return
        asset = self.asset_by_pair.get(pair)
        if asset is None:
            return
        try:
            self.strategy.seed(asset, [{
                "ts": r["timestamp"], "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": r["volume"],
            } for r in daily_rows])
            for c in intraday[:-1]:
                self._fold(asset, c)
            if intraday:
                self._pending[asset] = dict(intraday[-1])
        except Exception as e:
            self._inert("seed_boot", e)

    def seed_absent_members(self, fetch_daily) -> None:
        """Seed universe members with no running engine (their bars feed
        the breadth feature only). fetch_daily(asset) -> 1440m rows."""
        if self.disabled():
            return
        try:
            for member in self.strategy.universe:
                if member in self._running_assets():
                    continue
                rows = fetch_daily(member) or []
                self.strategy.seed(member, [{
                    "ts": r["timestamp"], "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"],
                    "volume": r["volume"]} for r in rows])
                if rows:
                    self._member_seed_day[member] = \
                        int(rows[-1]["timestamp"]) // 86400
                time.sleep(2)  # Kraken REST floor
        except Exception as e:
            self._inert("seed_absent_members", e)

    def refresh_absent_members(self, fetch_daily, now_ts: float) -> None:
        """Once per UTC day, re-seed non-running members so breadth stays
        current. Cheap no-op the rest of the time."""
        if self.disabled():
            return
        try:
            today = int(now_ts) // 86400
            for member in self.strategy.universe:
                if member in self._running_assets():
                    continue
                if self._member_seed_day.get(member, -1) >= today:
                    continue
                rows = fetch_daily(member) or []
                self.strategy.seed(member, [{
                    "ts": r["timestamp"], "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"],
                    "volume": r["volume"]} for r in rows])
                self._member_seed_day[member] = today
                time.sleep(2)  # Kraken REST floor
        except Exception as e:
            self._inert("refresh_absent_members", e)

    # ---- live candle feed -------------------------------------------------
    def _fold(self, asset: str, candle: dict) -> None:
        ts = float(candle["timestamp"])
        self.strategy.on_1h(asset, ts, float(candle["open"]),
                            float(candle["high"]), float(candle["low"]),
                            float(candle["close"]), float(candle["volume"]))
        self._fold_clock[asset] = max(self._fold_clock.get(asset, 0.0),
                                      ts + self.interval_s)

    def on_candle(self, pair: str, candle: dict) -> None:
        """Feed the same candle dict the engine ingests. Folds the
        previously pending candle once a NEWER timestamp arrives."""
        if self.disabled():
            return
        asset = self.asset_by_pair.get(pair)
        if asset is None:
            return
        try:
            ts = float(candle.get("timestamp") or 0.0)
            pending = self._pending.get(asset)
            if pending is None:
                self._pending[asset] = dict(candle)
                return
            p_ts = float(pending.get("timestamp") or 0.0)
            if ts > p_ts:
                self._fold(asset, pending)
                self._pending[asset] = dict(candle)
            elif ts == p_ts:
                self._pending[asset] = dict(candle)   # in-place bar update
            # ts < p_ts: stale push, drop
        except Exception as e:
            self._inert("on_candle", e)

    def data_now(self, asset: str) -> float:
        """The strategy's clock: advances only with folded candles."""
        return self._fold_clock.get(asset, 0.0)

    # ---- signal surface ---------------------------------------------------
    def indicator_block(self, pair: str) -> Dict[str, Any]:
        """quant_indicators["s3"] block for one pair; {} when the pair is
        outside the universe (caller then omits the key)."""
        if self.disabled():
            return {}
        asset = self.asset_by_pair.get(pair)
        if asset is None:
            return {}
        try:
            now = self.data_now(asset)
            if now <= 0:
                return {"active": False, "reason": "no_data"}
            sig = self.strategy.evaluate(asset, now)
            self._last_signal[asset] = sig
            return {
                "active": True,
                "model_loaded": sig.model_loaded,
                "stage": sig.stage,
                "score": round(sig.score, 4) if sig.score is not None else None,
                "gated": sig.gated,
                "degraded": sig.degraded,
                "n_daily_bars": sig.n_bars,
                "reasons": sig.reasons[:4],
            }
        except Exception as e:
            self._inert("indicator_block", e)
            return {"active": False, "reason": "error"}

    # ---- shadow phase (HYDRA_S3_STRATEGY=1; no order path) ----------------
    def _read_confirmer(self, asset: str, now_wall: float) -> dict:
        """Heartbeat 1h flow posterior status file (read-only, stdlib).
        Missing/stale/tainted => no_opinion — recorded, never blocking
        the shadow log (both arms are written)."""
        name = f"heartbeat_status_{asset.replace('/', '_')}.json"
        path = Path(CONFIRMER_STATUS_DIR) / name
        try:
            import json
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("tainted"):
                return {"status": "no_opinion", "why": "tainted"}
            ts = float(raw.get("ts") or 0.0)
            if now_wall - ts > CONFIRMER_STALE_S:
                return {"status": "no_opinion", "why": "stale"}
            return {"status": "ok", "p_up": raw.get("p_up"), "ts": ts}
        except (OSError, ValueError, TypeError):
            return {"status": "no_opinion", "why": "missing"}

    def ledger(self) -> ShadowLedger:
        if self._ledger is None:
            self._ledger = ShadowLedger(self.ledger_dir)
        return self._ledger

    def shadow_step(self, pair: str, mark_price: float) -> Optional[dict]:
        """Phase 2.6: advance shadow positions on newly completed bars and
        log a proposal when the current signal is gated entryable_b1.
        Returns the proposal event dict if one was logged."""
        if self.disabled() or not self.shadow_enabled():
            return None
        asset = self.asset_by_pair.get(pair)
        if asset is None:
            return None
        try:
            now = self.data_now(asset)
            if now <= 0:
                return None
            bars = self.strategy.series[asset].completed_bars(now)
            led = self.ledger()
            n = len(bars)
            for i, b in enumerate(bars[-3:]):       # advance recent bars
                j = n - min(3, n) + i               # index of b in bars
                ma9 = (sum(x.close for x in bars[j - 8:j + 1]) / 9
                       if j >= 8 else None)         # trail arms (x4a/x5)
                led.mark_bar(asset, b, ma9)
            sig = self._last_signal.get(asset) or \
                self.strategy.evaluate(asset, now)
            if sig.stage != "entryable_b1" or not sig.gated:
                return None
            model = self.strategy.artifact.models[asset]
            s = sig.setup
            entry_bar = bars[sig.entry_idx]
            confirmer = self._read_confirmer(asset, time.time())
            accepted = led.propose(
                asset=asset,
                low_ts=bars[s.low_idx].open_ts, low_px=s.low_px, atr=s.atr,
                low_idx=s.low_idx, entry_idx=sig.entry_idx,
                entry_ts=entry_bar.open_ts, entry_px=entry_bar.close,
                score=sig.score, arms=list(model.shadow_arms),
                premium_cut=model.premium_cut,
                confirmer=confirmer,
                extra={"pair": pair, "mark_price": mark_price,
                       "decision_s3_only": True,
                       "decision_s3_plus_confirmer":
                           confirmer.get("status") == "ok"
                           and (confirmer.get("p_up") or 0) >= 0.5})
            if accepted:
                logger.info("S3 shadow proposal %s score=%.3f arms=%s",
                            asset, sig.score, list(model.shadow_arms))
                return {"asset": asset, "score": sig.score}
            return None
        except Exception as e:
            self._inert("shadow_step", e)
            return None
