#!/usr/bin/env python3
"""
HYDRA Derivatives Stream — Kraken Futures read-only poller via kraken CLI.

════════════════════════════════════════════════════════════════════════
HARD INVARIANT — SPOT-ONLY EXECUTION
════════════════════════════════════════════════════════════════════════
Hydra trades ONLY these Kraken SPOT pairs:
    SOL/<stable>, SOL/BTC, BTC/<stable> (where stable ∈ {USD, USDC, USDT})
This module reads derivatives data (funding rates, open interest,
mark prices, quarterly basis) via the kraken CLI's PUBLIC
`futures tickers` subcommand. No authentication. No order placement.
Signal input only.

If you ever find yourself adding ANY of the authenticated `futures`
subcommands (order, edit-order, cancel, positions, leverage, transfer,
etc.) to this module, STOP. That is a bug, and it violates the
spot-only invariant documented in CLAUDE.md.
════════════════════════════════════════════════════════════════════════

Data source (public, no auth):
  `kraken -o json futures tickers` via WSL Ubuntu, matching the rest
  of Hydra's Kraken CLI bridge (see KrakenCLI._run in hydra_agent.py).

Fields consumed per ticker:
  symbol, markPrice, indexPrice, fundingRate,
  fundingRatePrediction, openInterest

Signals surfaced per spot pair:
  funding_bps_8h            : current 8h funding rate as bps (markPrice-relative)
  funding_predicted_bps     : next 8h prediction as bps (markPrice-relative)
  oi_delta_1h_pct           : OI change over 1h window
  oi_delta_24h_pct          : OI change over 24h window
  oi_price_regime           : trend_confirm_long | trend_confirm_short
                              | short_squeeze | liquidation_cascade
                              | balanced | unknown
  basis_apr_pct             : quarterly futures premium annualized

Spot-pair → derivatives mapping:
  BTC/<stable> → PF_XBTUSD (perp),  FF_XBTUSD_YYMMDD (dated)
  SOL/<stable> → PF_SOLUSD (perp),  FF_SOLUSD_YYMMDD (dated)
  SOL/BTC      → synthetic from SOL/USD and BTC/USD perps (no direct perp)

Kraken Futures perp symbols (PF_*) are denominated in USD regardless of
which spot stable quote the operator is using on the spot side. So
BTC/USD, BTC/USDC, and BTC/USDT all share the same PF_XBTUSD perp for
funding/OI signals — the SPOT_TO_DERIVATIVES map below registers every
stable variant explicitly so the lookup is O(1) and deliberate (no
implicit "any stable will work" — adding USDT support means adding the
USDT entries here).
"""

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

# Spot pair → derivatives metadata. Do NOT add order-placement endpoints here.
# Kraken Futures has one perp per (base, USD-side) — PF_XBTUSD covers BTC
# regardless of which stable the spot pair uses, etc. We register every
# stable variant explicitly so the map stays O(1) and the supported
# universe is deliberate (adding USDT means adding USDT rows below).
SPOT_TO_DERIVATIVES: Dict[str, Dict[str, object]] = {
    # USD-quoted spot pairs (v2.19+ default)
    "BTC/USD":  {"perp": "PF_XBTUSD", "quarterly_prefix": "FF_XBTUSD"},
    "SOL/USD":  {"perp": "PF_SOLUSD", "quarterly_prefix": "FF_SOLUSD"},
    # USDC-quoted spot pairs (pre-v2.19 default; still supported as opt-in)
    "BTC/USDC": {"perp": "PF_XBTUSD", "quarterly_prefix": "FF_XBTUSD"},
    "SOL/USDC": {"perp": "PF_SOLUSD", "quarterly_prefix": "FF_SOLUSD"},
    # USDT-quoted spot (PR-E / E7 — first-class stable quote in STABLE_QUOTES)
    "BTC/USDT": {"perp": "PF_XBTUSD", "quarterly_prefix": "FF_XBTUSD"},
    "SOL/USDT": {"perp": "PF_SOLUSD", "quarterly_prefix": "FF_SOLUSD"},
    # ETH — full coverage: PF perp + FF quarterlies exist on Kraken Futures
    "ETH/USD":  {"perp": "PF_ETHUSD", "quarterly_prefix": "FF_ETHUSD"},
    "ETH/USDC": {"perp": "PF_ETHUSD", "quarterly_prefix": "FF_ETHUSD"},
    "ETH/USDT": {"perp": "PF_ETHUSD", "quarterly_prefix": "FF_ETHUSD"},
    # ZEC — perp-only: PF_ZECUSD exists but Kraken lists NO quarterlies, so
    # basis_apr_pct is unavailable by construction (not stale). The derived
    # basis_available=False flag tells R10 to track 4 fields instead of 5;
    # without it ZEC would sit permanently at 1 stale field and any transient
    # miss would trip the R10 blackout.
    "ZEC/USD":  {"perp": "PF_ZECUSD", "quarterly_prefix": None},
    # Bridge — no direct perp, synthetic from SOL/USD ÷ BTC/USD
    "SOL/BTC":  {"perp": None, "quarterly_prefix": None, "synthetic": True},
}


# Kraken Futures returns PF_* fundingRate as absolute (quote currency per
# contract per funding period), NOT as a decimal rate. Convert to relative
# bps by dividing by markPrice first. Pre-v2.15.2 the parser multiplied by
# 10000 unconditionally, producing values that were wrong by markPrice (~70000x
# for BTC, ~80x for SOL). BTC's garbage tripped R1/R2; SOL's looked plausible
# but misled the Quant.
#
# Sanity bound is defense-in-depth against future API drift. Real funding
# extremes on Kraken Futures perps live in ±100 bps/8h; ±500 leaves 5x
# headroom. Past that, null + warn rather than feed R1/R2 a poisoned input.
FUNDING_BPS_SANITY_MAX = 500.0


def _absolute_to_relative_bps(
    fr: Optional[float], mark_price: Optional[float],
    pair: str, source: str,
) -> Optional[float]:
    """Convert Kraken Futures absolute fundingRate to relative bps.
    Returns None if either input is missing/zero/non-finite, or if the
    resulting magnitude exceeds the sanity bound."""
    if fr is None or mark_price is None:
        return None
    try:
        if mark_price == 0:
            return None
        bps = round((fr / mark_price) * 10000, 2)
    except (TypeError, ZeroDivisionError):
        return None
    # Defense against NaN/Inf upstream: float('nan') passes _maybe_float
    # and survives every comparison silently. Null it before it reaches
    # R1/R2 (where NaN comparisons are always False = wrong "no fire").
    if math.isnan(bps) or math.isinf(bps):
        return None
    if abs(bps) > FUNDING_BPS_SANITY_MAX:
        print(
            f"  [DerivativesStream] {pair} funding {bps:+.1f} bps from {source} "
            f"exceeds sanity bound ±{FUNDING_BPS_SANITY_MAX:.0f}; nulling. "
            f"Investigate Kraken Futures API units or WSL bridge.",
            file=sys.stderr,
        )
        return None
    return bps


@dataclass
class DerivativesSnapshot:
    """Per-spot-pair derivatives signal snapshot. All fields optional —
    None means "data not yet fetched" or "stale". Consumers must handle
    nulls; the Quant prompt treats null as 'data_stale' which can
    contribute to R10 force_hold if enough fields are missing."""
    pair: str
    perp_symbol: Optional[str] = None
    funding_bps_8h: Optional[float] = None
    funding_predicted_bps: Optional[float] = None
    open_interest: Optional[float] = None
    oi_delta_1h_pct: Optional[float] = None
    oi_delta_24h_pct: Optional[float] = None
    mark_price: Optional[float] = None
    basis_apr_pct: Optional[float] = None
    oi_price_regime: str = "unknown"
    last_updated_ts: float = 0.0
    staleness_s: float = 0.0
    synthetic: bool = False
    # v2.29.0: structural flag — False when the pair's Kraken Futures listing
    # has no quarterly contracts (perp-only, e.g. PF_ZECUSD), so basis_apr_pct
    # can never populate. Derived from the SPOT_TO_DERIVATIVES map at
    # construction (quarterly_prefix is None), never from data presence.
    # R10 (_count_stale_fields) drops basis from the tracked set when False.
    basis_available: bool = True
    # v2.14.1: consecutive-error streak (resets to 0 on successful populate).
    # Distinguishes "a transient blip at offset 17" from "this pair has been
    # dark for 4 polling cycles." R10 staleness already catches the latter,
    # but exposing the streak lets the Quant/Risk Manager prompt weigh a
    # fresh-but-suspicious-looking indicator differently from a steady one.
    fetch_error_streak: int = 0

    def freshness_s(self, now: Optional[float] = None) -> float:
        if self.last_updated_ts == 0:
            return float("inf")
        return (now if now is not None else time.time()) - self.last_updated_ts


class DerivativesStream:
    """Polls Kraken Futures public tickers endpoint on a daemon thread.

    SPOT-ONLY INVARIANT: this class has no authenticated methods, no
    order-placement methods, and should never gain any. It reads and
    caches public market data.
    """

    POLL_INTERVAL_S = 30
    HISTORY_WINDOW_S = 24 * 3600
    HTTP_TIMEOUT_S = 10

    # v2.18.0: restore gate. If the newest persisted sample is older
    # than this on `--resume`, the entire history for that symbol is
    # dropped and the native 1 H warmup kicks in instead. 30 min chosen
    # because the shortest consumer (`oi_delta_1h_pct`) tolerates a
    # half-window gap before the delta would bias meaningfully against
    # a stale baseline. The 24 H delta is a weaker consumer and is
    # accepted to be blunter on long restarts by design.
    MAX_RESTORE_GAP_S = 1800

    # OI/price thresholds for regime classification. Tunable but
    # deliberately conservative — a classifier that fires "squeeze"
    # on noise produces false signals the Quant must reason around.
    OI_REGIME_OI_THRESHOLD_PCT = 0.5
    OI_REGIME_PX_THRESHOLD_PCT = 0.3

    # v2.14.1: warn once per pair after this many consecutive failed polls
    # so a dark WSL/kraken-CLI bridge doesn't hide behind staleness alone.
    FETCH_ERROR_WARN_STREAK = 3

    def __init__(self, pairs: List[str]):
        self.pairs: List[str] = [p for p in pairs if p in SPOT_TO_DERIVATIVES]
        self._snapshots: Dict[str, DerivativesSnapshot] = {}
        self._oi_history: Dict[str, Deque[Tuple[float, float]]] = {}
        self._price_history: Dict[str, Deque[Tuple[float, float]]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        for pair in self.pairs:
            info = SPOT_TO_DERIVATIVES[pair]
            self._snapshots[pair] = DerivativesSnapshot(
                pair=pair,
                perp_symbol=info.get("perp"),  # type: ignore[arg-type]
                synthetic=bool(info.get("synthetic", False)),
                basis_available=info.get("quarterly_prefix") is not None
                or bool(info.get("synthetic", False)),
            )

    # ─── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="DerivativesStream"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ─── Persistence (v2.18.0) ───────────────────────────────────
    #
    # Shortens the 1 H / 24 H OI-delta warmup after `--resume` by letting
    # the agent snapshot persist `_oi_history` / `_price_history` across
    # restarts. Read-only by design: no new CLI paths, no auth surface.

    def snapshot(self) -> Dict[str, Dict[str, List[List[float]]]]:
        """Return JSON-serialisable OI + mark-price history per perp
        symbol. Sizes are bounded by the existing `HISTORY_WINDOW_S`
        prune (~8.6 k tuples per symbol at 30 s cadence × 24 h)."""
        with self._lock:
            return {
                "oi_history": {
                    sym: [[t, v] for (t, v) in dq]
                    for sym, dq in self._oi_history.items()
                },
                "price_history": {
                    sym: [[t, v] for (t, v) in dq]
                    for sym, dq in self._price_history.items()
                },
            }

    def restore(
        self, snapshot: Optional[Dict], now: Optional[float] = None
    ) -> None:
        """Rehydrate OI + price history from a snapshot dict.

        Safety gates applied per symbol, independently:
          - Drop if the newest sample is older than MAX_RESTORE_GAP_S —
            the 1 H delta would otherwise report against a stale
            baseline (preserves the "don't lie" invariant).
          - Prune to HISTORY_WINDOW_S on load so replaying a
            pathological snapshot cannot grow the deque past its cap.
          - Merge (don't replace): any samples already collected by the
            polling thread are preserved; result is sorted by timestamp
            and deduped.
          - Lock-protected to race safely with `_run_loop.poll_once`.
        """
        if not snapshot or not isinstance(snapshot, dict):
            return
        if now is None:
            now = time.time()
        cutoff = now - self.HISTORY_WINDOW_S
        for field, target in (
            ("oi_history", self._oi_history),
            ("price_history", self._price_history),
        ):
            persisted = snapshot.get(field) or {}
            if not isinstance(persisted, dict):
                continue
            with self._lock:
                for sym, samples in persisted.items():
                    if not isinstance(samples, list) or not samples:
                        continue
                    # Per-element parse so one malformed tuple inside a
                    # long valid list doesn't invalidate the whole symbol
                    # history.
                    parsed: List[Tuple[float, float]] = []
                    for s in samples:
                        try:
                            t, v = s
                            parsed.append((float(t), float(v)))
                        except (TypeError, ValueError):
                            continue
                    if not parsed:
                        continue
                    newest = max(t for t, _ in parsed)
                    if now - newest > self.MAX_RESTORE_GAP_S:
                        continue
                    existing = target.setdefault(sym, deque())
                    merged = sorted(
                        list(existing)
                        + [(t, v) for t, v in parsed if t >= cutoff]
                    )
                    seen: set = set()
                    deduped: List[Tuple[float, float]] = []
                    for t, v in merged:
                        if t in seen:
                            continue
                        seen.add(t)
                        deduped.append((t, v))
                    target[sym] = deque(deduped)

    # ─── Public accessor ─────────────────────────────────────────

    def latest(self, pair: str) -> Optional[DerivativesSnapshot]:
        """Return a snapshot for the spot pair, or None if unknown pair.
        Staleness is recomputed at read time so consumers always see an
        accurate 'seconds since last successful fetch'."""
        with self._lock:
            snap = self._snapshots.get(pair)
            if snap is None:
                return None
            now = time.time()
            # Return a fresh copy with updated staleness
            return DerivativesSnapshot(
                **{**snap.__dict__, "staleness_s": snap.freshness_s(now)}
            )

    # ─── Polling thread ──────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                # Thread must never die — missing data is better than no data.
                # Surface the exception to stderr once per type so a silent
                # regression in parsing doesn't become an invisible outage.
                print(
                    f"  [DerivativesStream] poll_once raised {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
            self._stop.wait(self.POLL_INTERVAL_S)

    def poll_once(self) -> bool:
        """Single poll cycle. Returns True if any pair updated."""
        tickers = self._fetch_tickers()
        if not tickers:
            with self._lock:
                for snap in self._snapshots.values():
                    self._record_fetch_error(snap)
            return False

        now = time.time()
        by_symbol: Dict[str, Dict] = {
            t.get("symbol", ""): t for t in tickers if t.get("symbol")
        }

        updated = False
        with self._lock:
            for pair in self.pairs:
                snap = self._snapshots[pair]
                if snap.synthetic:
                    sol = by_symbol.get("PF_SOLUSD")
                    btc = by_symbol.get("PF_XBTUSD")
                    if sol and btc:
                        self._populate_synthetic(snap, sol, btc, now)
                        updated = True
                    else:
                        self._record_fetch_error(snap)
                    continue
                tick = by_symbol.get(snap.perp_symbol or "")
                if not tick:
                    self._record_fetch_error(snap)
                    continue
                self._populate_from_ticker(snap, tick, now)
                q_prefix = SPOT_TO_DERIVATIVES[pair].get("quarterly_prefix")
                q_symbol = self._find_quarterly(by_symbol, q_prefix, now)  # type: ignore[arg-type]
                if q_symbol:
                    self._compute_basis(snap, tick, by_symbol[q_symbol], q_symbol, now)
                updated = True
        return updated

    def _record_fetch_error(self, snap: DerivativesSnapshot) -> None:
        """Increment error counter + streak. Emit a stderr warning the
        first time streak crosses FETCH_ERROR_WARN_STREAK so the operator
        notices a dark WSL bridge instead of watching staleness silently
        grow. Caller must hold self._lock."""
        snap.fetch_error_streak += 1
        if snap.fetch_error_streak == self.FETCH_ERROR_WARN_STREAK:
            print(
                f"  [DerivativesStream] {snap.pair} has failed "
                f"{snap.fetch_error_streak} consecutive polls — check WSL/kraken CLI",
                file=sys.stderr,
            )

    # ─── CLI bridge (SPOT-ONLY: uses only public `futures tickers`) ──

    def _fetch_tickers(self) -> List[Dict]:
        """Invoke `kraken -o json futures tickers` via WSL and return
        the tickers array. Returns [] on any error — caller treats an
        empty list as 'no update this cycle' (fetch_error_streak increments,
        staleness grows). NEVER calls any authenticated subcommand.

        v2.15.2: log per failure mode so stuck WSL bridges are visible.
        Prior behavior swallowed exceptions silently; staleness would
        grow without any operator-visible cause.
        """
        cmd_str = "source ~/.cargo/env && kraken -o json futures tickers 2>/dev/null"
        cmd = ["wsl", "-d", os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu"), "--", "bash", "-c", cmd_str]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.HTTP_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            print(
                f"  [DerivativesStream] kraken CLI timeout after "
                f"{self.HTTP_TIMEOUT_S}s — WSL bridge may be stuck",
                file=sys.stderr,
            )
            return []
        except OSError as e:
            print(
                f"  [DerivativesStream] OSError invoking WSL/kraken: {e} "
                f"— check `wsl -l -v` for distro 'Ubuntu'",
                file=sys.stderr,
            )
            return []

        stdout = result.stdout.strip()
        if not stdout:
            return []
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            preview = stdout[:120].replace("\n", " ")
            print(
                f"  [DerivativesStream] JSON parse error from kraken CLI: {e} "
                f"— payload preview: {preview!r}",
                file=sys.stderr,
            )
            return []
        if not isinstance(payload, dict):
            return []
        return payload.get("tickers", []) or []

    # ─── Per-pair populate ───────────────────────────────────────

    def _populate_from_ticker(
        self, snap: DerivativesSnapshot, tick: Dict, now: float
    ) -> None:
        mark = _maybe_float(tick.get("markPrice"))
        fr = _maybe_float(tick.get("fundingRate"))
        fr_pred = _maybe_float(tick.get("fundingRatePrediction"))
        oi = _maybe_float(tick.get("openInterest"))

        if mark is not None:
            snap.mark_price = mark
        # Use the freshly extracted `mark` (not snap.mark_price which could be
        # stale from a prior tick where this tick lacks markPrice). If mark is
        # missing this round, both funding fields go None — we cannot guess.
        # Unlike the guarded fields above, funding writes are unconditional
        # (helper may return None). Reason: a stale funding bps anchored to a
        # markPrice that didn't refresh this tick would silently mislead R1/R2.
        # Nulling forces R10 to flag staleness via its missing-field count.
        snap.funding_bps_8h = _absolute_to_relative_bps(
            fr, mark, snap.pair, "fundingRate"
        )
        snap.funding_predicted_bps = _absolute_to_relative_bps(
            fr_pred, mark, snap.pair, "fundingRatePrediction"
        )
        if oi is not None:
            snap.open_interest = oi

        sym = snap.perp_symbol
        if not sym:
            return

        self._oi_history.setdefault(sym, deque())
        self._price_history.setdefault(sym, deque())

        if snap.open_interest is not None:
            self._oi_history[sym].append((now, snap.open_interest))
        if snap.mark_price is not None:
            self._price_history[sym].append((now, snap.mark_price))

        cutoff = now - self.HISTORY_WINDOW_S
        _prune_before(self._oi_history[sym], cutoff)
        _prune_before(self._price_history[sym], cutoff)

        snap.oi_delta_1h_pct = _delta_pct(
            self._oi_history[sym], now - 3600, snap.open_interest
        )
        snap.oi_delta_24h_pct = _delta_pct(
            self._oi_history[sym], now - 24 * 3600, snap.open_interest
        )
        price_delta_1h_pct = _delta_pct(
            self._price_history[sym], now - 3600, snap.mark_price
        )
        snap.oi_price_regime = self._classify_oi_price_regime(
            snap.oi_delta_1h_pct, price_delta_1h_pct
        )

        snap.last_updated_ts = now
        snap.fetch_error_streak = 0

    def _populate_synthetic(
        self,
        snap: DerivativesSnapshot,
        sol_tick: Dict,
        btc_tick: Dict,
        now: float,
    ) -> None:
        """SOL/BTC has no direct perp on Kraken Futures. We synthesize
        a "relative positioning" signal: SOL-USD funding minus BTC-USD
        funding as a proxy for which side of the SOL/BTC leg is
        crowded, and the SOL/BTC mark ratio from the two USD perps."""
        sol_fr = _maybe_float(sol_tick.get("fundingRate"))
        btc_fr = _maybe_float(btc_tick.get("fundingRate"))
        sol_mark = _maybe_float(sol_tick.get("markPrice"))
        btc_mark = _maybe_float(btc_tick.get("markPrice"))

        # Each leg's fundingRate is absolute USD/contract/period — they don't
        # share a denominator. Normalize each by its own markPrice before the
        # subtraction. If either leg lacks markPrice, the synthetic signal is
        # undefined; null it rather than emit garbage.
        sol_rel = _absolute_to_relative_bps(
            sol_fr, sol_mark, snap.pair, "synthetic.sol"
        )
        btc_rel = _absolute_to_relative_bps(
            btc_fr, btc_mark, snap.pair, "synthetic.btc"
        )
        if sol_rel is not None and btc_rel is not None:
            diff = round(sol_rel - btc_rel, 2)
            # Re-clamp the diff: per-leg clamp bounds each input to ±500, so the
            # subtraction can still reach ±1000. A diff exceeding ±500 means the
            # two legs disagree at unrealistic magnitudes — null the synthetic too.
            if abs(diff) > FUNDING_BPS_SANITY_MAX:
                print(
                    f"  [DerivativesStream] {snap.pair} synthetic funding "
                    f"{diff:+.1f} bps exceeds sanity bound; nulling.",
                    file=sys.stderr,
                )
                snap.funding_bps_8h = None
            else:
                snap.funding_bps_8h = diff
        else:
            # Same null-on-stale rationale as _populate_from_ticker — see comment there.
            snap.funding_bps_8h = None

        if sol_mark is not None and btc_mark is not None and btc_mark > 0:
            ratio = sol_mark / btc_mark
            snap.mark_price = round(ratio, 8)

        # No direct OI for synthetic; leave oi_* as None and regime unknown.
        snap.oi_price_regime = "balanced"
        snap.last_updated_ts = now
        snap.fetch_error_streak = 0

    # ─── Regime classifier ───────────────────────────────────────

    def _classify_oi_price_regime(
        self, oi_delta_pct: Optional[float], price_delta_pct: Optional[float]
    ) -> str:
        if oi_delta_pct is None or price_delta_pct is None:
            return "unknown"
        oi_dir = (
            1 if oi_delta_pct > self.OI_REGIME_OI_THRESHOLD_PCT
            else -1 if oi_delta_pct < -self.OI_REGIME_OI_THRESHOLD_PCT
            else 0
        )
        px_dir = (
            1 if price_delta_pct > self.OI_REGIME_PX_THRESHOLD_PCT
            else -1 if price_delta_pct < -self.OI_REGIME_PX_THRESHOLD_PCT
            else 0
        )
        if oi_dir == 0 or px_dir == 0:
            return "balanced"
        if oi_dir == 1 and px_dir == 1:
            return "trend_confirm_long"
        if oi_dir == 1 and px_dir == -1:
            return "trend_confirm_short"
        if oi_dir == -1 and px_dir == 1:
            return "short_squeeze"
        return "liquidation_cascade"  # oi_dir == -1 and px_dir == -1

    # ─── Basis (quarterly) ───────────────────────────────────────

    def _find_quarterly(
        self, by_symbol: Dict[str, Dict], prefix: Optional[str],
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Return earliest-dated NOT-YET-EXPIRED dated contract with the
        given prefix, or None.

        Filters out suffixes whose parsed YYMMDD is already in the past
        to prevent _compute_basis from annualizing over a clamped 1-day
        tenor (which would produce nonsense APR from residual premium
        on a lingering expired contract).

        Malformed suffixes (non-YYMMDD, bad dates) are also skipped."""
        if not prefix:
            return None
        import datetime
        if now is None:
            now = time.time()
        today = datetime.datetime.fromtimestamp(
            now, tz=datetime.timezone.utc
        ).date()
        candidates: List[str] = []
        for s in by_symbol:
            if not s.startswith(prefix + "_"):
                continue
            suffix = s.rsplit("_", 1)[-1]
            if len(suffix) != 6 or not suffix.isdigit():
                continue
            try:
                exp = datetime.date(
                    2000 + int(suffix[0:2]),
                    int(suffix[2:4]),
                    int(suffix[4:6]),
                )
            except ValueError:
                continue
            if exp < today:
                continue
            candidates.append(s)
        if not candidates:
            return None
        return sorted(candidates)[0]

    def _compute_basis(
        self,
        snap: DerivativesSnapshot,
        perp_tick: Dict,
        q_tick: Dict,
        q_symbol: str,
        now: float,
    ) -> None:
        q_mark = _maybe_float(q_tick.get("markPrice"))
        perp_mark = _maybe_float(perp_tick.get("markPrice"))
        if q_mark is None or perp_mark is None or perp_mark == 0:
            return
        suffix = q_symbol.rsplit("_", 1)[-1]
        try:
            if len(suffix) != 6 or not suffix.isdigit():
                return
            yr = 2000 + int(suffix[0:2])
            mo = int(suffix[2:4])
            dy = int(suffix[4:6])
            import datetime
            expiry = datetime.datetime(
                yr, mo, dy, tzinfo=datetime.timezone.utc
            ).timestamp()
            days = max(1.0, (expiry - now) / 86400)
            premium = (q_mark - perp_mark) / perp_mark
            snap.basis_apr_pct = round(premium * (365.0 / days) * 100.0, 2)
        except (ValueError, IndexError) as e:
            import logging; logging.warning(f"Ignored exception: {e}")


# ─── Helpers (module-private) ───────────────────────────────────

def _maybe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prune_before(history: Deque[Tuple[float, float]], cutoff: float) -> None:
    while history and history[0][0] < cutoff:
        history.popleft()


def _delta_pct(
    history: Deque[Tuple[float, float]],
    target_ts: float,
    current: Optional[float],
) -> Optional[float]:
    """Percent change of `current` vs the sample closest to (but not
    after) target_ts. Returns None when no suitable baseline exists."""
    if current is None or not history:
        return None
    closest: Optional[Tuple[float, float]] = None
    for ts, val in history:
        if ts <= target_ts:
            closest = (ts, val)
        else:
            break
    if closest is None or closest[1] == 0:
        return None
    return round(100.0 * (current - closest[1]) / closest[1], 2)
