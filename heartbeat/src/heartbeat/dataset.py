"""Local dataset IO for trade tapes — no network I/O.

Accepts CSV / JSONL / JSON list files, iterables of row dicts, or an
existing ``list[Trade]``. All paths resolve to ``list[Trade]`` using the
canonical ``heartbeat.feed.tape.Trade`` / ``Side`` types.

Missing inputs raise ``MissingDatasetError``; schema/parse/empty-after-
parse failures raise ``InvalidDatasetError``. Never silently invents
sides or coin-flip fills.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from .errors import InvalidDatasetError, MissingDatasetError
from .feed.tape import Side, Trade

# ---------------------------------------------------------------------------
# Column aliases (case-insensitive)
# ---------------------------------------------------------------------------

_TS_ALIASES = ("ts", "timestamp", "time")
_PRICE_ALIASES = ("price",)
_QTY_ALIASES = ("qty", "quantity", "size", "volume")
_SIDE_ALIASES = ("side", "aggressor")
_TRADE_ID_ALIASES = ("trade_id", "tradeid", "id")
_ORD_TYPE_ALIASES = ("ord_type", "ordtype", "order_type")

_REQUIRED_HINT = (
    "required columns: ts|timestamp|time, price, "
    "qty|quantity|size|volume, side|aggressor "
    "(buy/sell/b/s/1/-1); optional: trade_id, ord_type"
)

# Explicit OHLCV rejection (YAGNI: no synthetic mid-side policy).
_OHLCV_HINT = (
    "OHLCV-only is not supported — provide aggressor side "
    "(side|aggressor = buy/sell/b/s/1/-1). "
    "Synthetic mid-side policy is not implemented (YAGNI)."
)
_OHLCV_COL_HINTS = frozenset({
    "open", "high", "low", "close", "ohlc", "ohlcv", "bar", "candle",
})

PathOrRows = Union[str, Path, Iterable[Any]]


def dataset_requirements() -> dict:
    """Describe accepted formats and columns for agents / MCP hosts."""
    return {
        "formats": [
            "csv",
            "jsonl",
            "json",  # list of row objects
            "list[dict]",
            "list[Trade]",
        ],
        "accepted_formats": [
            ".csv",
            ".jsonl",
            ".json",
            "iterable of dicts",
            "list of heartbeat.feed.tape.Trade",
        ],
        "required_columns": {
            "ts": list(_TS_ALIASES),
            "price": list(_PRICE_ALIASES),
            "qty": list(_QTY_ALIASES),
            "side": list(_SIDE_ALIASES),
        },
        "columns": {
            "ts": {"aliases": list(_TS_ALIASES), "required": True},
            "price": {"aliases": list(_PRICE_ALIASES), "required": True},
            "qty": {"aliases": list(_QTY_ALIASES), "required": True},
            "side": {
                "aliases": list(_SIDE_ALIASES),
                "required": True,
                "values": ["buy", "sell", "b", "s", "1", "-1"],
            },
            "trade_id": {"aliases": list(_TRADE_ID_ALIASES), "required": False},
            "ord_type": {"aliases": list(_ORD_TYPE_ALIASES), "required": False},
        },
        "side_values": {
            "buy": ["buy", "b", "1"],
            "sell": ["sell", "s", "-1"],
        },
        "unsupported": {
            "ohlcv_only": True,
            "reason": (
                "OHLCV-only rows without aggressor side are not supported. "
                "Synthetic mid-side / invent-side-from-OHLC policy is not "
                "implemented (YAGNI)."
            ),
            "hint": _OHLCV_HINT,
        },
        "notes": [
            "OHLCV-only rows without aggressor side are not supported.",
            "Synthetic mid-side policy is not implemented (YAGNI).",
            "No network I/O; path must be a local file or in-memory rows.",
            "symbol kwarg is free-form metadata (stock tickers OK; not used for parsing).",
        ],
        "hint": _REQUIRED_HINT,
    }


def load_trades(
    path_or_rows: PathOrRows,
    *,
    symbol: Optional[str] = None,  # noqa: ARG001 — reserved for callers/metadata
) -> list[Trade]:
    """Load and normalize trades from a file path or in-memory rows.

    Parameters
    ----------
    path_or_rows
        ``.csv`` / ``.jsonl`` / ``.json`` path, iterable of dicts, or
        ``list[Trade]``.
    symbol
        Optional free-form symbol label (stock or crypto). Not used for
        parsing; reserved for higher-level APIs.

    Returns
    -------
    list[Trade]
        Non-empty list of normalized trades.

    Raises
    ------
    MissingDatasetError
        Empty path string or missing file.
    InvalidDatasetError
        Schema/parse failures or zero rows after parse.
    """
    del symbol  # metadata only; parsing is column-driven

    if path_or_rows is None:
        raise MissingDatasetError(
            "dataset path or rows is None",
            hint=_REQUIRED_HINT,
        )

    # Empty string path
    if isinstance(path_or_rows, str) and path_or_rows.strip() == "":
        raise MissingDatasetError(
            "dataset path is empty",
            hint=_REQUIRED_HINT,
        )

    # Existing list[Trade] (or any sequence of Trade)
    if isinstance(path_or_rows, list) and path_or_rows and all(
        isinstance(x, Trade) for x in path_or_rows
    ):
        return path_or_rows

    # Path-like
    if isinstance(path_or_rows, (str, Path)):
        path = Path(path_or_rows)
        if not path.exists() or not path.is_file():
            raise MissingDatasetError(
                f"dataset not found: {path}",
                hint=_REQUIRED_HINT,
            )
        rows = _read_file_rows(path)
        trades = _rows_to_trades(rows)
        if not trades:
            raise InvalidDatasetError(
                f"dataset has zero rows after parse: {path}",
                hint=_REQUIRED_HINT,
            )
        return trades

    # Iterable of dicts / trades (consume once)
    try:
        items = list(path_or_rows)  # type: ignore[arg-type]
    except TypeError as exc:
        raise InvalidDatasetError(
            f"unsupported dataset input type: {type(path_or_rows).__name__}",
            hint=_REQUIRED_HINT,
        ) from exc

    if not items:
        raise InvalidDatasetError(
            "dataset has zero rows",
            hint=_REQUIRED_HINT,
        )

    if all(isinstance(x, Trade) for x in items):
        return list(items)

    if all(isinstance(x, Mapping) for x in items):
        trades = _rows_to_trades(items)
        if not trades:
            raise InvalidDatasetError(
                "dataset has zero rows after parse",
                hint=_REQUIRED_HINT,
            )
        return trades

    raise InvalidDatasetError(
        "dataset rows must be dicts or Trade instances",
        hint=_REQUIRED_HINT,
    )


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_file_rows(path: Path) -> list[Mapping[str, Any]]:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingDatasetError(
            f"dataset not readable: {path}: {exc}",
            hint=_REQUIRED_HINT,
        ) from exc

    if suffix == ".csv":
        return _parse_csv(text, source=str(path))
    if suffix == ".jsonl":
        return _parse_jsonl(text, source=str(path))
    if suffix == ".json":
        return _parse_json(text, source=str(path))
    raise InvalidDatasetError(
        f"unsupported dataset format {suffix!r} for {path}; "
        f"use .csv, .jsonl, or .json",
        hint=_REQUIRED_HINT,
    )


def _parse_csv(text: str, *, source: str) -> list[Mapping[str, Any]]:
    # Strip BOM if present
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    if not text.strip():
        return []
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise InvalidDatasetError(
            f"CSV has no header row: {source}",
            hint=_REQUIRED_HINT,
        )
    rows: list[Mapping[str, Any]] = []
    for raw in reader:
        # DictReader yields empty-ish rows for blank lines; skip pure empties
        if raw is None:
            continue
        if all(v is None or str(v).strip() == "" for v in raw.values()):
            continue
        rows.append(raw)
    return rows


def _parse_jsonl(text: str, *, source: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InvalidDatasetError(
                f"invalid JSONL at {source}:{lineno}: {exc}",
                hint=_REQUIRED_HINT,
            ) from exc
        if not isinstance(obj, Mapping):
            raise InvalidDatasetError(
                f"JSONL row must be an object at {source}:{lineno}",
                hint=_REQUIRED_HINT,
            )
        rows.append(obj)
    return rows


def _parse_json(text: str, *, source: str) -> list[Mapping[str, Any]]:
    try:
        obj = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as exc:
        raise InvalidDatasetError(
            f"invalid JSON in {source}: {exc}",
            hint=_REQUIRED_HINT,
        ) from exc
    if not isinstance(obj, list):
        raise InvalidDatasetError(
            f"JSON root must be a list of trade objects: {source}",
            hint=_REQUIRED_HINT,
        )
    rows: list[Mapping[str, Any]] = []
    for i, item in enumerate(obj):
        if not isinstance(item, Mapping):
            raise InvalidDatasetError(
                f"JSON item {i} must be an object: {source}",
                hint=_REQUIRED_HINT,
            )
        rows.append(item)
    return rows


# ---------------------------------------------------------------------------
# Row → Trade
# ---------------------------------------------------------------------------

def _rows_to_trades(rows: Sequence[Mapping[str, Any]]) -> list[Trade]:
    trades: list[Trade] = []
    for i, row in enumerate(rows):
        trades.append(_row_to_trade(row, index=i))
    return trades


def _norm_keys(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k).strip().lower(): v for k, v in row.items() if k is not None}


def _pick(norm: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for a in aliases:
        if a in norm and norm[a] is not None and str(norm[a]).strip() != "":
            return norm[a]
    return None


def _looks_like_ohlcv(norm: Mapping[str, Any]) -> bool:
    """True when the row has OHLCV-ish columns and no aggressor side."""
    keys = set(norm.keys())
    has_ohlc = bool(keys & _OHLCV_COL_HINTS) or (
        "open" in keys and "close" in keys
    )
    # Common candle CSVs: open,high,low,close,volume without side.
    candle_core = {"open", "high", "low", "close"}
    if candle_core.issubset(keys):
        has_ohlc = True
    return has_ohlc and _pick(norm, _SIDE_ALIASES) is None


def _row_to_trade(row: Mapping[str, Any], *, index: int) -> Trade:
    norm = _norm_keys(row)

    ts_raw = _pick(norm, _TS_ALIASES)
    price_raw = _pick(norm, _PRICE_ALIASES)
    qty_raw = _pick(norm, _QTY_ALIASES)
    side_raw = _pick(norm, _SIDE_ALIASES)

    # Fail loud on OHLCV-only (no synthetic mid-side).
    if side_raw is None and _looks_like_ohlcv(norm):
        raise InvalidDatasetError(
            f"row {index}: OHLCV-only input without aggressor side",
            hint=_OHLCV_HINT,
        )

    missing = []
    if ts_raw is None:
        missing.append("ts|timestamp|time")
    if price_raw is None:
        missing.append("price")
    if qty_raw is None:
        missing.append("qty|quantity|size|volume")
    if side_raw is None:
        missing.append("side|aggressor")
    if missing:
        # Prefer OHLCV-specific hint when side is the only/main gap and
        # volume/close-style keys suggest candle data.
        hint = _REQUIRED_HINT
        if side_raw is None and any(
            k in norm for k in ("close", "open", "high", "low", "volume")
        ):
            hint = _OHLCV_HINT
        raise InvalidDatasetError(
            f"row {index}: missing required column(s): {', '.join(missing)}",
            hint=hint,
        )

    try:
        ts = float(ts_raw)
        price = float(price_raw)
        qty = float(qty_raw)
    except (TypeError, ValueError) as exc:
        raise InvalidDatasetError(
            f"row {index}: non-numeric ts/price/qty ({exc})",
            hint=_REQUIRED_HINT,
        ) from exc

    side = _parse_side(side_raw, index=index)

    ord_type_raw = _pick(norm, _ORD_TYPE_ALIASES)
    ord_type = str(ord_type_raw).strip() if ord_type_raw is not None else "limit"

    trade_id_raw = _pick(norm, _TRADE_ID_ALIASES)
    trade_id = 0
    if trade_id_raw is not None:
        try:
            trade_id = int(float(trade_id_raw))
        except (TypeError, ValueError) as exc:
            raise InvalidDatasetError(
                f"row {index}: invalid trade_id {trade_id_raw!r}",
                hint=_REQUIRED_HINT,
            ) from exc

    return Trade(
        ts=ts,
        price=price,
        qty=qty,
        side=side,
        ord_type=ord_type,
        trade_id=trade_id,
    )


def _parse_side(raw: Any, *, index: int) -> Side:
    if isinstance(raw, Side):
        return raw
    s = str(raw).strip().lower()
    if s in ("buy", "b", "1", "1.0"):
        return Side.BUY
    if s in ("sell", "s", "-1", "-1.0"):
        return Side.SELL
    raise InvalidDatasetError(
        f"row {index}: bad side value {raw!r}; expected buy/sell/b/s/1/-1",
        hint="use buy/sell/b/s/1/-1 for side|aggressor",
    )
