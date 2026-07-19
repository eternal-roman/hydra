"""High-level indicator API — dataset in, P(up) out.

``run_dataset`` is the primary batch entry for agents and CLI. It loads
trades, applies calibrated weights when available, runs the same
``run_tape`` pipeline used by eval/replay, and returns a structured
``IndicatorResult``. Missing/invalid data re-raises structured errors
from ``heartbeat.dataset`` (never swallowed).

Uncalibrated weights produce ``status="degraded"`` and an explicit
warning — never a silent coin-flip without disclosure (H3 / HONEST_FINDINGS).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .config import load_config
from .dataset import PathOrRows, load_trades
from .engine.pipeline import HeartbeatPipeline, run_tape
from .engine.posterior import HeartbeatOutput
from .feed.tape import Trade
from .weights_io import (
    apply_weights_to_config,
    find_weights,
    load_weights_file,
)

_UNCALIBRATED_WARNING = (
    "uncalibrated_weights: p_up uses default_weight (near coin-flip)"
)

ConfigLike = Union[None, str, Path, Mapping[str, Any]]
WeightsLike = Union[None, str, Path, Mapping[str, float]]


@dataclass
class IndicatorResult:
    """Structured P(up) indicator output for batch or streaming use."""

    symbol: str
    tf: str
    n_trades: int
    n_candles: int
    n_heartbeats: int
    p_up: Optional[float]
    L: Optional[float]
    ts: Optional[float]
    tainted: Optional[bool]
    series: List[dict] = field(default_factory=list)
    status: str = "ok"  # ok | degraded | error
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "n_trades": self.n_trades,
            "n_candles": self.n_candles,
            "n_heartbeats": self.n_heartbeats,
            "p_up": self.p_up,
            "L": self.L,
            "ts": self.ts,
            "tainted": self.tainted,
            "series": list(self.series),
            "status": self.status,
            "warnings": list(self.warnings),
        }


def _resolve_config(config: ConfigLike) -> dict:
    if config is None:
        return load_config()
    if isinstance(config, Mapping):
        return copy.deepcopy(dict(config))
    # path to yaml
    return load_config(str(config))


def _apply_weights(
    cfg: dict,
    *,
    symbol: str,
    tf: str,
    weights: WeightsLike,
) -> bool:
    """Mutate cfg with weights. Returns True if calibrated weights applied."""
    if weights is not None:
        if isinstance(weights, Mapping):
            apply_weights_to_config(cfg, {str(k): float(v) for k, v in weights.items()})
            return True
        path = Path(weights)
        loaded = load_weights_file(path)
        if not loaded:
            return False
        apply_weights_to_config(cfg, loaded)
        return True

    store_root = cfg.get("store", {}).get("root", "data")
    found = find_weights(symbol, tf, store_root=store_root)
    if found:
        apply_weights_to_config(cfg, found[0])
        return True
    return False


def _status_for(calibrated: bool, series: Sequence[dict],
                warnings: List[str]) -> str:
    if not series:
        if not calibrated:
            warnings.append(_UNCALIBRATED_WARNING)
        return "error"
    if not calibrated:
        warnings.append(_UNCALIBRATED_WARNING)
        return "degraded"
    return "ok"


def _last_from_series(series: Sequence[dict]) -> tuple[
    Optional[float], Optional[float], Optional[float], Optional[bool]
]:
    if not series:
        return None, None, None, None
    last = series[-1]
    return (
        last.get("p_up"),
        last.get("L"),
        last.get("ts"),
        last.get("tainted"),
    )


def run_dataset(
    data: PathOrRows,
    *,
    symbol: str = "UNKNOWN",
    tf: str = "1h",
    config: ConfigLike = None,
    weights: WeightsLike = None,
) -> IndicatorResult:
    """Run the heartbeat indicator over a trade dataset.

    Parameters
    ----------
    data
        Path to CSV/JSONL/JSON, iterable of row dicts, or ``list[Trade]``.
    symbol
        Free-form symbol label (crypto or equity ticker).
    tf
        Candle timeframe string understood by the candle builder (e.g. ``1h``).
    config
        ``None`` (package default.yaml), a merged config dict, or path to YAML.
    weights
        ``None`` (auto ``find_weights``), feature→weight dict, or path to
        weights JSON.

    Returns
    -------
    IndicatorResult
        Last candle-close posterior plus full series.

    Raises
    ------
    MissingDatasetError
        Empty path or missing file (propagated from ``load_trades``).
    InvalidDatasetError
        Schema/parse/empty failures (propagated from ``load_trades``).
    """
    trades = load_trades(data, symbol=symbol)
    cfg = _resolve_config(config)
    calibrated = _apply_weights(cfg, symbol=symbol, tf=tf, weights=weights)

    n_heartbeats = 0

    def _on_hb(_out: HeartbeatOutput, _progress: float) -> None:
        nonlocal n_heartbeats
        n_heartbeats += 1

    series = run_tape(cfg, symbol, tf, trades, on_heartbeat=_on_hb)
    warnings: List[str] = []
    status = _status_for(calibrated, series, warnings)
    p_up, L, ts, tainted = _last_from_series(series)

    return IndicatorResult(
        symbol=symbol,
        tf=tf,
        n_trades=len(trades),
        n_candles=len(series),
        n_heartbeats=n_heartbeats,
        p_up=p_up,
        L=L,
        ts=ts,
        tainted=tainted,
        series=series,
        status=status,
        warnings=warnings,
    )


class HeartbeatSession:
    """Streaming wrapper: feed trades one-by-one, read ``.latest``.

    Same weight/config semantics as ``run_dataset``. Does not place orders.
    """

    def __init__(
        self,
        *,
        symbol: str = "UNKNOWN",
        tf: str = "1h",
        config: ConfigLike = None,
        weights: WeightsLike = None,
    ) -> None:
        self.symbol = symbol
        self.tf = tf
        self._cfg = _resolve_config(config)
        self._calibrated = _apply_weights(
            self._cfg, symbol=symbol, tf=tf, weights=weights
        )
        self._n_trades = 0
        self._n_heartbeats = 0
        self._series: List[dict] = []
        self._pipe = HeartbeatPipeline(
            self._cfg,
            symbol,
            tf,
            on_heartbeat=self._on_heartbeat,
            on_candle=self._on_candle,
        )

    def _on_heartbeat(self, out: HeartbeatOutput, _progress: float) -> None:
        self._n_heartbeats += 1

    def _on_candle(self, row: dict) -> None:
        self._series.append(row)

    def feed_trade(
        self,
        trade: Trade,
        local_ts: Optional[float] = None,
    ) -> Optional[HeartbeatOutput]:
        """Push one trade; return heartbeat output when a heartbeat fires."""
        self._n_trades += 1
        return self._pipe.feed_trade(trade, local_ts=local_ts)

    def flush(self) -> Optional[dict]:
        """Force-close the forming candle (end of stream)."""
        return self._pipe.flush()

    @property
    def latest(self) -> IndicatorResult:
        """Best-effort indicator snapshot from closed candles + last heartbeat.

        If no candle has closed yet, falls back to ``pipe.last_output`` for
        p_up/L/ts/tainted (still may be None during cold start).
        """
        series = list(self._series)
        warnings: List[str] = []
        # Prefer closed-candle series; if empty, treat last heartbeat as soft state
        if series:
            p_up, L, ts, tainted = _last_from_series(series)
            status = _status_for(self._calibrated, series, warnings)
        else:
            out = self._pipe.last_output
            if out is not None:
                p_up, L, ts, tainted = out.p_up, out.L, out.ts, out.tainted
                # no closed candles yet — degraded if uncalibrated else ok with empty series
                if not self._calibrated:
                    warnings.append(_UNCALIBRATED_WARNING)
                    status = "degraded"
                else:
                    status = "ok"
            else:
                p_up = L = ts = tainted = None
                status = _status_for(self._calibrated, series, warnings)

        return IndicatorResult(
            symbol=self.symbol,
            tf=self.tf,
            n_trades=self._n_trades,
            n_candles=len(series),
            n_heartbeats=self._n_heartbeats,
            p_up=p_up,
            L=L,
            ts=ts,
            tainted=tainted,
            series=series,
            status=status,
            warnings=warnings,
        )
