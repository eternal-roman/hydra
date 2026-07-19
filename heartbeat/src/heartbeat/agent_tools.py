"""Agent / MCP tool surface — pure schemas + dispatch (no MCP SDK).

Hosts (Claude, Cursor, custom MCP wrappers) can:

1. Advertise ``TOOL_SCHEMAS`` (JSON-schema style dicts) as tools.
2. Route invocations through ``call_tool(name, arguments) -> dict``.

Every call returns an envelope — never raises to the host:

- Success: ``{"ok": true, "result": ...}``
- Failure: ``{"ok": false, "error": {"code", "message", "hint"}}``

Structured ``HeartbeatError`` codes (``missing_dataset``,
``invalid_dataset``) are preserved. Unknown tools and argument mistakes
use ``unknown_tool`` / ``invalid_arguments``. Unexpected exceptions map
to ``tool_error`` so agents never see a traceback as the response body.

**No order path.** Tools only expose dataset requirements, batch
indicator runs, and interpretation text. See ``AGENT.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .dataset import dataset_requirements
from .errors import HeartbeatError
from .indicator import run_dataset

# ---------------------------------------------------------------------------
# Tool schemas (MCP-compatible JSON Schema style)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "heartbeat_requirements",
        "description": (
            "Return accepted trade-tape formats and required columns for "
            "heartbeat_run_dataset. Call this before inventing CSV schemas. "
            "OHLCV-only rows without aggressor side are not supported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "heartbeat_run_dataset",
        "description": (
            "Run the heartbeat order-flow indicator on a local trade tape "
            "(path to .csv/.jsonl/.json, or in-memory rows). Returns "
            "IndicatorResult as a dict: p_up in [0,1], L, ts, tainted, "
            "series, status (ok|degraded|error), warnings. Never places "
            "orders. Missing/invalid data returns an error envelope with "
            "code missing_dataset or invalid_dataset."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Local path to trade tape (.csv, .jsonl, or .json list). "
                        "Provide path OR rows, not neither."
                    ),
                },
                "rows": {
                    "type": "array",
                    "description": (
                        "In-memory trade rows (list of objects with ts, price, "
                        "qty, side). Provide path OR rows, not neither."
                    ),
                    "items": {"type": "object"},
                },
                "symbol": {
                    "type": "string",
                    "description": (
                        "Free-form symbol label (crypto or equity ticker). "
                        "Default UNKNOWN."
                    ),
                    "default": "UNKNOWN",
                },
                "tf": {
                    "type": "string",
                    "description": "Candle timeframe (e.g. 1h). Default 1h.",
                    "default": "1h",
                },
                "weights_path": {
                    "type": "string",
                    "description": (
                        "Optional path to calibrated weights JSON. When omitted, "
                        "auto-find by symbol/tf or run uncalibrated (status=degraded)."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "heartbeat_explain",
        "description": (
            "Short agent-oriented text on how to interpret p_up, tainted, "
            "uncalibrated weights, and the no-order-path rule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

_KNOWN_NAMES = frozenset(s["name"] for s in TOOL_SCHEMAS)

_EXPLAIN_TEXT = """\
heartbeat indicator — agent contract (summary)

WHAT IT IS
  Order-flow confirmer: recursive Bayesian P(up) from trade tape
  (aggressor side + price/qty). Asset-agnostic (crypto or equity) when
  rows carry side. Confirmation classifier, not a standalone signal
  generator and NEVER an order path.

OUTPUT FIELDS (IndicatorResult / heartbeat_run_dataset result)
  p_up     Posterior P(price up) in [0, 1] at last candle close.
  L        Log-odds companion to p_up (L = logit(p_up) in the model).
  ts       Exchange timestamp of the last series point.
  tainted  If true: feed gap / clock skew / sequence issue overlapped
           this window — treat as NO OPINION. Never substitute 0.5.
  status   "ok" | "degraded" | "error"
  warnings List of human strings (see uncalibrated below).
  series   Per closed-candle rows including p_up (batch history).

HOW TO USE p_up
  - Prefer status=="ok" and tainted is not true before acting on level.
  - High p_up ≈ order-flow leaning up; low ≈ down. Near 0.5 is weak.
  - Pair with your own entry logic; heartbeat is confirmer-only.

TAINTED
  Consumers MUST treat tainted: true (or a stale ts) as "no opinion".
  Do not invent 0.5, do not force a trade.

UNCALIBRATED WEIGHTS
  Without calibrated weights for symbol/tf, status becomes "degraded"
  and warnings include:
    "uncalibrated_weights: p_up uses default_weight (near coin-flip)"
  That is intentional disclosure — never a silent coin-flip.
  Pass weights_path when you have a weights_{SYMBOL}_{tf}.json file.

DATA REQUIREMENTS
  Call heartbeat_requirements for formats/columns. Required: ts, price,
  qty, side (buy/sell/b/s/1/-1). OHLCV-only without aggressor side is
  NOT supported.

ERRORS
  call_tool never raises. Failures return:
    {"ok": false, "error": {"code", "message", "hint"}}
  Codes: missing_dataset, invalid_dataset, invalid_arguments,
  unknown_tool, tool_error.

NO ORDER PATH
  These tools do not place, cancel, or size orders. Indicator JSON only.
"""


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def _ok(result: Any) -> dict:
    return {"ok": True, "result": result}


def _err(code: str, message: str, hint: Optional[str] = None) -> dict:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "hint": hint,
        },
    }


def _from_heartbeat_error(exc: HeartbeatError) -> dict:
    return _err(exc.code, str(exc), hint=exc.hint)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_requirements(_arguments: Mapping[str, Any]) -> dict:
    return _ok(dataset_requirements())


def _tool_explain(_arguments: Mapping[str, Any]) -> dict:
    return _ok(_EXPLAIN_TEXT.strip() + "\n")


def _tool_run_dataset(arguments: Mapping[str, Any]) -> dict:
    path = arguments.get("path")
    rows = arguments.get("rows")
    symbol = arguments.get("symbol") or "UNKNOWN"
    tf = arguments.get("tf") or "1h"
    weights_path = arguments.get("weights_path")

    has_path = path is not None and str(path).strip() != ""
    has_rows = rows is not None

    if has_path and has_rows:
        return _err(
            "invalid_arguments",
            "provide path OR rows, not both",
            hint="omit rows when path is set (or omit path when rows is set)",
        )
    if not has_path and not has_rows:
        return _err(
            "invalid_arguments",
            "heartbeat_run_dataset requires path or rows",
            hint=(
                "pass path to a local .csv/.jsonl/.json file, or rows as a "
                "list of {ts, price, qty, side} objects; "
                "call heartbeat_requirements for column aliases"
            ),
        )

    data: Any = str(path) if has_path else rows
    weights: Any = None
    if weights_path is not None and str(weights_path).strip() != "":
        weights = str(weights_path)

    try:
        result = run_dataset(
            data,
            symbol=str(symbol),
            tf=str(tf),
            weights=weights,
        )
    except HeartbeatError as exc:
        return _from_heartbeat_error(exc)

    return _ok(result.to_dict())


_DISPATCH = {
    "heartbeat_requirements": _tool_requirements,
    "heartbeat_run_dataset": _tool_run_dataset,
    "heartbeat_explain": _tool_explain,
}


def call_tool(name: str, arguments: Optional[Mapping[str, Any]] = None) -> dict:
    """Dispatch a named tool and return an ok/error envelope.

    Parameters
    ----------
    name
        One of the names in ``TOOL_SCHEMAS``.
    arguments
        Tool arguments dict (or None / empty for no-arg tools).

    Returns
    -------
    dict
        ``{"ok": true, "result": ...}`` or
        ``{"ok": false, "error": {"code", "message", "hint"}}``.
        Never raises.
    """
    try:
        if name not in _DISPATCH:
            known = ", ".join(sorted(_KNOWN_NAMES))
            return _err(
                "unknown_tool",
                f"unknown tool {name!r}",
                hint=f"known tools: {known}",
            )
        args: Mapping[str, Any] = arguments if arguments is not None else {}
        if not isinstance(args, Mapping):
            return _err(
                "invalid_arguments",
                f"arguments must be a mapping, got {type(args).__name__}",
                hint="pass a JSON object as arguments",
            )
        return _DISPATCH[name](args)
    except HeartbeatError as exc:
        # Safety net if a tool forgets to catch
        return _from_heartbeat_error(exc)
    except Exception as exc:  # noqa: BLE001 — envelope contract for agents
        return _err(
            "tool_error",
            f"{type(exc).__name__}: {exc}",
            hint="see heartbeat AGENT.md; check path/rows/symbol/tf types",
        )
