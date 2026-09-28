"""Monkey-patching helpers and canned Kraken response builders.

The harness replaces KrakenCLI._run (and adjacent methods) with stubs that
return deterministic responses. Every install() must be paired with a
restore() via try/finally so a failing scenario doesn't corrupt siblings.

All response builders mirror the exact shape KrakenCLI._run returns on
live Kraken — callers check `"error" in result` to detect failures, and
expect specific sub-keys on success. If Kraken ever changes one of these
shapes, update the builder and the harness scenarios will catch the
downstream drift.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from hydra_agent import KrakenCLI  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# StubRun — context-style monkey-patch for KrakenCLI._run
# ─────────────────────────────────────────────────────────────────

class StubRun:
    """Replaces KrakenCLI._run with a dispatcher that returns canned responses.

    Supports three response modes:

    1. Static dict/list — every call returns the same response:
       StubRun({"volume": "1234"})

    2. List of responses — returns them in sequence (IndexError if exhausted):
       StubRun([resp1, resp2, resp3])

    3. Callable dispatcher — receives args and returns the response. Use this
       to inspect call args and route to different responses:
       StubRun(lambda args: resp_a if "order" in args else resp_b)

    All calls are recorded in .calls for post-scenario assertions.
    Install/restore via try/finally or use the with_stub() context helper.
    """

    def __init__(self, response):
        self._response = response
        self._response_index = 0
        self.calls: list[list] = []
        self._original = None

    def _resolve(self, args: list) -> Any:
        if callable(self._response):
            return self._response(args)
        if isinstance(self._response, list):
            if self._response_index >= len(self._response):
                raise IndexError(
                    f"StubRun response list exhausted after {self._response_index} "
                    f"calls; scenario made more KrakenCLI._run calls than mocked. "
                    f"Last call args: {args}"
                )
            resp = self._response[self._response_index]
            self._response_index += 1
            return resp
        return self._response

    def install(self) -> "StubRun":
        self._original = KrakenCLI._run
        outer = self

        def fake(args, timeout: int = 20):
            outer.calls.append(list(args))
            return outer._resolve(list(args))

        KrakenCLI._run = staticmethod(fake)
        return self

    def restore(self) -> None:
        if self._original is not None:
            KrakenCLI._run = staticmethod(self._original)
            self._original = None


class with_stub:
    """Context manager that auto-installs and restores a StubRun.

    Usage:
        with with_stub({"txid": ["ABC"]}) as stub:
            result = KrakenCLI.order_buy("SOL/USDC", 0.02, price=100.0)
            assert stub.calls[0][0] == "order"
    """

    def __init__(self, response):
        self.stub = StubRun(response)

    def __enter__(self) -> StubRun:
        return self.stub.install()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stub.restore()


# ─────────────────────────────────────────────────────────────────
# Kraken response builders
# ─────────────────────────────────────────────────────────────────
#
# These produce the exact shape that KrakenCLI._run returns on each
# endpoint. Builders live here so every scenario uses identical shapes
# and any Kraken schema drift is caught in one place.


def kraken_ticker(pair: str, bid: float, ask: float, price: float = None) -> dict:
    """Return a ticker response matching KrakenCLI.ticker() parser expectations.

    KrakenCLI.ticker() reshapes the raw response into a flat dict with
    `pair`, `price`, `ask`, `bid`, `high_24h`, `low_24h`, `volume_24h`, `open`.
    But we stub at the _run level (below reshape), so we must produce the
    raw Kraken shape: a top-level dict keyed by the resolved pair name,
    containing a sub-dict with keys 'a' (ask), 'b' (bid), 'c' (last close),
    'h', 'l', 'v', 'o'. The real KrakenCLI.ticker iterates top-level keys
    looking for a 'last_price' (kraken-cli v0.4.1) or 'c' (legacy) sub-key;
    this builder emits the legacy shape, which ticker() still accepts.
    """
    if price is None:
        price = (bid + ask) / 2
    return {
        pair: {
            "a": [f"{ask:.8f}", "1", "1.000"],
            "b": [f"{bid:.8f}", "1", "1.000"],
            "c": [f"{price:.8f}", "0.1"],
            "h": [f"{ask * 1.01:.8f}", f"{ask * 1.02:.8f}"],
            "l": [f"{bid * 0.99:.8f}", f"{bid * 0.98:.8f}"],
            "v": ["100", "1000"],
            "o": f"{price:.8f}",
            "p": [f"{price:.8f}", f"{price:.8f}"],
            "t": [10, 100],
        }
    }


def kraken_ticker_error(msg: str = "EAPI:Rate limit") -> dict:
    return {"error": msg}


def kraken_ticker_missing_fields() -> dict:
    """Response that parses as a dict but lacks 'bid'/'ask' keys after reshape.

    This is what KrakenCLI.ticker() returns when the raw response has no
    sub-dict with a 'last_price' or 'c' key — it passes the raw data through. The
    _execute_trade check `"bid" not in ticker` triggers on this.
    """
    return {"WEIRD_PAIR": {"unexpected": "shape"}}


def kraken_order_success_scalar(txid: str) -> dict:
    """Kraken order response with a scalar txid (rare — most return a list)."""
    return {
        "descr": {"order": f"buy 0.02 PAIR @ limit 100"},
        "txid": txid,
    }


def kraken_order_success_list(txid: str) -> dict:
    """Kraken order response with a list-wrapped txid (the common case).

    This is the shape that commit 9e652d5 was about. _execute_trade at
    hydra_agent.py:1192-1193 must unwrap it.
    """
    return {
        "descr": {"order": f"buy 0.02 PAIR @ limit 100"},
        "txid": [txid],
    }


def kraken_order_success_nested(txid: str) -> dict:
    """Kraken order response with txid nested under 'result'.

    Covered by _execute_trade's fallback at hydra_agent.py:1190
    (`result.get("result", {}).get("txid", "unknown")`).
    """
    return {
        "result": {
            "descr": {"order": f"buy 0.02 PAIR @ limit 100"},
            "txid": txid,
        }
    }


def kraken_order_success_missing_txid() -> dict:
    """Response that succeeded but has no txid field anywhere.

    _execute_trade's chain resolves to 'unknown' and reconciler.register
    skips the entry (known_orders is not populated).
    """
    return {
        "descr": {"order": "buy 0.02 PAIR @ limit 100"},
    }


def kraken_order_success_empty_list() -> dict:
    """Response with `txid: []` — edge case covered by txid[0] if txid else 'unknown'."""
    return {
        "descr": {"order": "buy 0.02 PAIR @ limit 100"},
        "txid": [],
    }


def kraken_order_error(msg: str) -> dict:
    return {"error": msg}


def kraken_order_timeout() -> dict:
    """Matches KrakenCLI._run timeout return shape at hydra_agent.py:111."""
    return {"error": "Command timed out", "retryable": True}


def kraken_order_json_error() -> dict:
    """Matches KrakenCLI._run JSONDecodeError return shape at hydra_agent.py:113."""
    return {"error": "JSON parse error: unexpected token", "raw": "garbage"}


def kraken_paper_success() -> dict:
    """Kraken paper_buy/paper_sell success response."""
    return {
        "status": "success",
        "message": "Paper trade executed",
    }


def kraken_paper_error(msg: str) -> dict:
    return {"error": msg}


def kraken_validate_success() -> dict:
    """Response from kraken order ... --validate when the order is valid.

    Kraken returns a descr but no txid because nothing was placed.
    """
    return {
        "descr": {"order": "buy 0.02 PAIR @ limit 100"},
    }


def kraken_validate_error(msg: str) -> dict:
    return {"error": msg}


def kraken_volume_success() -> dict:
    """Minimal volume endpoint response — a well-formed dict."""
    return {
        "volume": "100.0",
        "fees": {"SOLUSDC": {"fee": "0.35"}},
        "fees_maker": {"SOLUSDC": {"fee": "0.25"}},
    }


def kraken_balance_success(balances: dict[str, float] = None) -> dict:
    """Kraken balance response — flat dict of asset -> string amount."""
    if balances is None:
        balances = {"USDC": "500.00", "XXBT": "0.005", "SOL": "2.0"}
    return {k: str(v) for k, v in balances.items()}


# ─────────────────────────────────────────────────────────────────
# Response dispatcher builder — route by command keyword
# ─────────────────────────────────────────────────────────────────

def build_dispatcher(responses: dict[str, Any]) -> Callable[[list], Any]:
    """Builds a dispatcher function for StubRun that routes by command keyword.

    Usage:
        dispatcher = build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", 100, 101),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_success_list("ABC123"),
        })

    Routes args to responses based on these rules (first match wins):
      - args contains '--validate' -> 'order_validate'
      - args[0] == 'order'         -> 'order'
      - args[0] == 'ticker'        -> 'ticker'
      - args[0] == 'balance'       -> 'balance'
      - args[0:2] == ['paper','buy'] -> 'paper_buy'
      - args[0:2] == ['paper','sell'] -> 'paper_sell'
      - args[0] == 'volume'        -> 'volume'
    """

    def dispatch(args: list) -> Any:
        if not args:
            return {"error": "empty args"}
        head = args[0]
        if head == "order" and "--validate" in args:
            key = "order_validate"
        elif head == "order" and len(args) >= 2 and args[1] in ("buy", "sell"):
            key = "order"
        elif head == "paper" and len(args) >= 2:
            key = f"paper_{args[1]}"
        elif head in ("ticker", "balance", "volume",
                       "trades-history", "status"):
            key = head.replace("-", "_")
        else:
            key = head
        if key in responses:
            return responses[key]
        return {"error": f"StubRun dispatcher has no response for key '{key}' (args: {args})"}

    return dispatch
