"""Hydra Kraken CLI Wrapper."""
import subprocess
import json
import os
import shlex
import threading
import time
from typing import Optional

WSL_DISTRO = os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu")
# Process-wide floor between authenticated REST invocations (CLAUDE 2s invariant).
# Agent also sleeps at call sites; this is the hard gate for concurrent callers
# (companion ladder, multi-thread). Public market data may share the same path —
# 2s is still correct under Kraken throttle policy.
#
# Upstream confirms this floor is load-bearing and cannot be delegated:
# kraken-cli AGENTS.md states "The CLI does not pre-throttle or retry
# rate-limited requests" — the server enforces limits and the CLI surfaces the
# rejection immediately. Hydra is the only throttle in the chain.
KRAKEN_REST_FLOOR_S = 2.0

# ── kraken-cli error envelope (upstream AGENTS.md) ──────────────────────────
# Every failure returns {"error": "<category>", "message": "<human text>"}
# with optional "suggestion" / "docs_url" / "retryable". NOTE: `error` is a
# CATEGORY SLUG, not a sentence — upstream CONTEXT.md is explicit: "Route
# handling decisions based on the `error` field rather than message text."
# Hydra historically wrote its own human strings into the same `error` key,
# which collides with the slug namespace; `_normalize_error` keeps `error`
# byte-compatible for existing callers while adding unambiguous
# `error_category` / `error_message` / `retryable` fields alongside it.
CLI_ERROR_RETRYABLE = {
    "api": False,
    "auth": False,
    "config": False,
    "io": False,
    "network": True,
    "parse": False,
    "rate_limit": True,
    "validation": False,
    "websocket": True,
}

# Hydra-local transport failures (the CLI never ran, or its output was
# unusable). Kept distinct from the upstream categories so callers can tell
# "Kraken rejected this" from "the WSL bridge is dark".
_TRANSPORT_RETRYABLE = {
    "transport_timeout": True,
    "transport_empty": True,
    "transport_exit": False,
    "transport_parse": False,
    "transport_spawn": True,
}

# Read-only commands that are safe to retry on a retryable failure. Order
# placement, cancellation and paper trades are deliberately EXCLUDED: a retry
# after an ambiguous timeout can double-place, and no idempotency key on the
# spot order path protects against it.
_RETRY_SAFE_HEADS = frozenset({
    "status", "ticker", "ohlc", "orderbook", "trades", "spreads", "assets",
    "pairs", "balance", "extended-balance", "open-orders", "closed-orders",
    "trades-history", "query-orders", "volume", "ledgers", "server-time",
    "futures",
})

from hydra_pair_registry import (
    PairRegistry,
    default_registry,
    normalize_asset as _registry_normalize_asset,
    STAKED_SUFFIXES as _REGISTRY_STAKED_SUFFIXES,
    ASSET_ALIASES as _REGISTRY_ASSET_ALIASES,
)

# ═══════════════════════════════════════════════════════════════
# KRAKEN CLI WRAPPER (via WSL)
# ═══════════════════════════════════════════════════════════════

def _normalize_error(payload: dict) -> dict:
    """Annotate a kraken-cli error envelope with unambiguous fields.

    Adds (never replaces) `error_category`, `error_message` and `retryable`.
    The original `error` key is left byte-identical so the many existing
    `if "error" in result` / `result["error"]` call sites keep working.

    Upstream sends `{"error": "<category>", "message": "..."}`. Hydra's own
    transport failures use an `error_category` of `transport_*`.
    """
    if not isinstance(payload, dict) or "error" not in payload:
        return payload
    raw = payload.get("error")
    category = raw if isinstance(raw, str) and raw in CLI_ERROR_RETRYABLE else None
    if category is not None:
        payload.setdefault("error_category", category)
        payload.setdefault("error_message", payload.get("message") or category)
        # Upstream may state `retryable` explicitly; its value wins over the
        # category default so a future CLI can override per-error.
        if "retryable" not in payload:
            payload["retryable"] = CLI_ERROR_RETRYABLE[category]
    else:
        payload.setdefault("error_category", "unknown")
        payload.setdefault("error_message", str(raw))
        payload.setdefault("retryable", False)
    return payload


def describe_error(payload: dict) -> str:
    """Render a CLI error for logs/journal as '<category>: <message>'.

    Prevents the "PLACEMENT_FAILED: validation" class of log line, where the
    bare category slug was printed and the actionable message discarded.
    """
    if not isinstance(payload, dict) or "error" not in payload:
        return ""
    cat = payload.get("error_category") or payload.get("error")
    msg = payload.get("error_message") or payload.get("message") or ""
    out = f"{cat}: {msg}" if msg and msg != cat else str(cat)
    sugg = payload.get("suggestion")
    if sugg:
        out += f" ({sugg})"
    return out


class KrakenCLI:
    """Wraps kraken-cli v0.3.2 running in WSL Ubuntu.

    Upstream re-verified 2026-08-05 against krakenfx/kraken-cli: v0.3.2
    (2026-04-20) is still the latest tagged release and `aa32814` is still
    branch HEAD, so the pin below is current — there is no newer CLI to
    adopt. What changed on Hydra's side is alignment with the CLI's
    *documented* contract (tool-catalog.json / AGENTS.md / CONTEXT.md):
      - `paper buy|sell` take VOLUME positionally, not via `--volume`.
      - errors are category slugs with a `retryable` hint (see
        `_normalize_error`), not human sentences.
      - the CLI does not pre-throttle or retry; Hydra owns the 2s floor.

    Verified compatible with kraken-cli v0.3.2 (commit aa32814+):
      - `--asset-class` flag is canonical (`--aclass` is hidden alias);
        Hydra never passed `--aclass`, so no callsite change required.
      - `relativeFundingRate` rename in commit 910a4d6 was internal to
        kraken-cli's paper-trading futures engine. Hydra calls
        `kraken futures tickers` (read-only public endpoint), which still
        emits `fundingRate` (absolute, USD/contract/period) — that field
        is converted to relative bps via `_absolute_to_relative_bps` in
        `hydra_derivatives_stream.py`.
      - Spot endpoints (ticker/balance/orderbook/ohlc/orders/pairs) have
        no breaking schema changes from v0.2.3 → v0.3.2.

    Pair metadata (precision, ordermin, costmin, alias resolution) is
    delegated to `hydra_pair_registry.PairRegistry`. The class-level
    `registry` attribute is the shared registry instance; live agent
    boot calls `KrakenCLI.apply_pair_constants(load_pair_constants(...))`
    to overlay authoritative metadata from `kraken pairs`.
    """

    # Single source of truth for pair metadata. Class-level so the
    # static-method API (_resolve_pair, _format_price, ...) can delegate
    # without threading an instance through every callsite. Tests that
    # need isolation can call `set_registry(default_registry())` to
    # reset between cases.
    registry: PairRegistry = default_registry()

    # Global REST spacing (monotonic + lock). Shared across all KrakenCLI uses.
    _rest_lock = threading.Lock()
    _last_rest_mono: float = 0.0

    # Suffixes Kraken uses for non-tradable (staked/bonded/locked/earn) assets.
    # Re-exposed from hydra_pair_registry so external callers
    # (KrakenCLI.STAKED_SUFFIXES) continue to resolve.
    STAKED_SUFFIXES = _REGISTRY_STAKED_SUFFIXES

    # Re-export for external callers that previously read this dict.
    # Prefer `hydra_pair_registry.normalize_asset` for new code.
    ASSET_NORMALIZE = _REGISTRY_ASSET_ALIASES

    # Conservative fallback for any pair not in the registry — preserves
    # the legacy `_format_price` passthrough behavior for unknown pairs.
    PRICE_DECIMALS_DEFAULT = 8

    @classmethod
    def set_registry(cls, registry: PairRegistry) -> None:
        """Replace the class-level registry (test/boot use only)."""
        cls.registry = registry

    @staticmethod
    def _is_staked(asset: str) -> bool:
        """Check if an asset name represents a staked/bonded/locked position."""
        if not asset:
            return False
        return any(asset.endswith(s) for s in _REGISTRY_STAKED_SUFFIXES)

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        """Normalize Kraken asset name to canonical form (e.g. XXBT → BTC).

        Strips staked suffixes (.B/.S/.M/.F) first, then applies
        Z/X-prefix aliases (ZUSD→USD, XXBT→BTC).
        """
        return _registry_normalize_asset(asset)

    @staticmethod
    def version() -> str:
        """Return the installed kraken-cli version from WSL, or 'unknown' on failure."""
        try:
            result = subprocess.run(
                ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                 "source ~/.cargo/env && kraken --version 2>/dev/null"],
                capture_output=True, text=True, timeout=5,
            )
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                return parts[1]
        except Exception:
            pass
        return "unknown"

    @classmethod
    def _throttle_rest(cls) -> None:
        """Enforce ≥KRAKEN_REST_FLOOR_S between any KrakenCLI._run calls."""
        with cls._rest_lock:
            now = time.monotonic()
            wait = KRAKEN_REST_FLOOR_S - (now - cls._last_rest_mono)
            if wait > 0:
                time.sleep(wait)
            cls._last_rest_mono = time.monotonic()

    @classmethod
    def _build_invocation(cls, args: list) -> tuple:
        """Return (argv, env) for a kraken CLI call.

        Every arg is passed through `shlex.quote` before being joined
        into the bash -c string — internal callers use typed numerics
        and known-good pair names today, but the companion/dashboard
        surface is growing and a single unescaped caller would grant
        RCE in the WSL environment. v2.15.0 hardens the boundary.

        Multi-tenancy credentials are handed to the child process through
        its ENVIRONMENT (forwarded into the distro via WSLENV), never
        interpolated into the bash -c string. The old `export KRAKEN_API_
        SECRET=...` form put the live trading secret in the `wsl` process
        argv, where any local user could read it out of `ps` / procfs for
        the duration of the call. shlex.quote stopped injection but never
        disclosure. Set HYDRA_CLI_LEGACY_SECRET_EXPORT=1 to revert if a
        WSLENV-less environment cannot forward the variables.
        """
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        env = os.environ.copy()
        cmd_str = "source ~/.cargo/env"

        api_key = os.environ.get("KRAKEN_API_KEY")
        api_secret = os.environ.get("KRAKEN_API_SECRET")
        if api_key and api_secret:
            if os.environ.get("HYDRA_CLI_LEGACY_SECRET_EXPORT") == "1":
                cmd_str += (
                    f" && export KRAKEN_API_KEY={shlex.quote(api_key)}"
                    f" && export KRAKEN_API_SECRET={shlex.quote(api_secret)}"
                )
            else:
                # WSLENV is a colon-separated list of variable NAMES to
                # forward across the Windows→WSL boundary. The values stay
                # in the environment block, out of argv.
                names = ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"]
                existing = env.get("WSLENV", "")
                parts = [p for p in existing.split(":") if p]
                for n in names:
                    if n not in parts:
                        parts.append(n)
                env["WSLENV"] = ":".join(parts)

        cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
        return ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd_str], env

    @classmethod
    def _run_once(cls, args: list, timeout: int = 20) -> dict:
        """Single CLI invocation. Enforces the REST floor, returns parsed JSON.

        v2.27.6: process-wide 2s REST floor (lock + monotonic) so concurrent
        callers cannot stack under the Kraken throttle.
        """
        cls._throttle_rest()
        cmd, env = cls._build_invocation(args)
        stdout = ""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env,
            )
            stdout = result.stdout.strip()
            rc = result.returncode
            if not stdout:
                return _normalize_error({
                    "error": f"Empty response (exit code {rc})",
                    "error_category": "transport_empty",
                    "error_message": f"kraken CLI produced no stdout (exit code {rc})",
                    "retryable": _TRANSPORT_RETRYABLE["transport_empty"],
                })
            data = json.loads(stdout)
            if isinstance(data, dict) and "error" in data:
                return _normalize_error(data)
            if rc != 0:
                # Non-zero exit with parseable stdout: surface the failure so
                # callers don't treat partial output as success.
                return _normalize_error({
                    "error": f"Non-zero exit code {rc}",
                    "error_category": "transport_exit",
                    "error_message": f"kraken CLI exited {rc} with parseable output",
                    "retryable": _TRANSPORT_RETRYABLE["transport_exit"],
                    "partial": data,
                })
            return data
        except subprocess.TimeoutExpired:
            return _normalize_error({
                "error": "Command timed out",
                "error_category": "transport_timeout",
                "error_message": f"kraken CLI exceeded {timeout}s",
                "retryable": _TRANSPORT_RETRYABLE["transport_timeout"],
            })
        except json.JSONDecodeError as e:
            return _normalize_error({
                "error": f"JSON parse error: {e}",
                "error_category": "transport_parse",
                "error_message": f"unparseable CLI stdout: {e}",
                "retryable": _TRANSPORT_RETRYABLE["transport_parse"],
                "raw": stdout[:200] if stdout else "",
            })
        except Exception as e:
            return _normalize_error({
                "error": str(e),
                "error_category": "transport_spawn",
                "error_message": f"{type(e).__name__}: {e}",
                "retryable": _TRANSPORT_RETRYABLE["transport_spawn"],
            })

    @classmethod
    def _is_retry_safe(cls, args: list) -> bool:
        """True only for read-only commands (never order/cancel/paper).

        A retry after an ambiguous timeout on a placement call can double-
        place: the first invocation may have reached Kraken even though we
        never saw the response, and the spot order path carries no
        idempotency key that would collapse the duplicate.
        """
        if not args:
            return False
        head = str(args[0])
        if head not in _RETRY_SAFE_HEADS:
            return False
        # `futures` is retry-safe only for its public read subcommands.
        if head == "futures":
            sub = str(args[1]) if len(args) > 1 else ""
            return sub in ("instruments", "tickers", "ticker", "orderbook",
                           "history", "feeschedules", "historical-funding-rates")
        return True

    @classmethod
    def _run(cls, args: list, timeout: int = 20, retries: int = None) -> dict:
        """Execute a kraken CLI command via WSL and return parsed JSON.

        Retries are attempted ONLY when the command is read-only and the CLI
        reported a retryable failure (`network`, `rate_limit`, or a Hydra
        transport timeout). Upstream AGENTS.md: the CLI never retries on its
        own, so an unretried transient blanks an indicator for a whole tick.
        The 2s REST floor still gates every attempt.
        """
        if retries is None:
            retries = 2 if cls._is_retry_safe(args) else 0
        attempt = 0
        while True:
            data = cls._run_once(args, timeout=timeout)
            if not (isinstance(data, dict) and "error" in data):
                return data
            if attempt >= retries or not data.get("retryable"):
                return data
            attempt += 1
            # _throttle_rest already spaces attempts by KRAKEN_REST_FLOOR_S;
            # add escalating backoff on top for rate_limit/network.
            time.sleep(min(2.0 ** attempt, 8.0))

    @classmethod
    def _resolve_pair(cls, pair: str) -> str:
        """Resolve to CLI pair format (e.g. SOL/BTC, BTC/USD).

        Unknown pairs are returned unchanged (passthrough), matching
        the pre-v2.19 behavior.
        """
        p = cls.registry.get(pair)
        return p.cli_format if p else pair

    @classmethod
    def _resolve_ws_pair(cls, pair: str) -> str:
        """Resolve to WS v2 pair format (e.g. SOL/BTC, BTC/USD).

        Unknown pairs returned unchanged. With BTC as canonical, WS v2
        format matches CLI format directly for all known pairs.
        """
        p = cls.registry.get(pair)
        return p.ws_format if p else pair

    @classmethod
    def _format_price(cls, pair: str, price: float) -> str:
        """Format a price at the pair's native precision.

        Looks up the pair in the registry, rounds the price to the
        allowed number of decimals, and formats with trailing zeros to
        8dp (Kraken accepts trailing zeros as insignificant but rejects
        meaningful decimals beyond the pair's precision). Unknown pairs
        fall back to PRICE_DECIMALS_DEFAULT (8).
        """
        p = cls.registry.get(pair)
        if p is not None:
            return p.format_price(price)
        rounded = round(float(price), cls.PRICE_DECIMALS_DEFAULT)
        return f"{rounded:.8f}"

    # ─── System Status ───

    @staticmethod
    def system_status() -> dict:
        """Get Kraken system status.

        Returns {"status": "online"|"cancel_only"|"post_only"|"maintenance",
                 "timestamp": "..."} or {"error": "..."} on failure.
        """
        return KrakenCLI._run(["status"])

    # ─── Asset Pair Info ───

    @classmethod
    def asset_pairs(cls, pairs: list = None) -> dict:
        """Get tradable asset pair info.

        Returns {pair_name: {pair_decimals, ordermin, costmin, base, quote, ...}}
        or {"error": "..."} on failure.
        """
        args = ["pairs"]
        if pairs:
            resolved = ",".join(cls._resolve_pair(p) for p in pairs)
            args.extend(["--pair", resolved])
        return cls._run(args)

    @classmethod
    def load_pair_constants(cls, pairs: list) -> dict:
        """Fetch pair info from Kraken and return normalized constants.

        Returns {friendly_pair: {price_decimals, ordermin, costmin, base, quote,
        lot_decimals, tick_size}} for each requested pair that Kraken knows about.
        Returns {} on API failure (caller should use registry fallback values).
        """
        data = cls.asset_pairs(pairs)
        if not isinstance(data, dict) or "error" in data:
            return {}

        # Build a friendly-name lookup using the registry for input forms.
        # Kraken returns wsname like "SOL/USD" or "XBT/USDC"; altname like
        # "SOLUSD" or "XBTUSDC"; and the top-level dict key uses Kraken's
        # internal name. The registry resolves all these forms.
        result = {}
        for kraken_name, info in data.items():
            if not isinstance(info, dict):
                continue
            friendly_pair = (
                cls.registry.get(info.get("wsname"))
                or cls.registry.get(info.get("altname"))
                or cls.registry.get(kraken_name)
            )
            # If we asked for a pair not in the registry, fall back to the
            # original friendly form the caller passed.
            friendly = friendly_pair.cli_format if friendly_pair else None
            if not friendly:
                # Best-effort: if Kraken's altname matches one of our
                # requested pairs in a slashless form, use the original.
                slashless = (info.get("altname") or kraken_name).upper()
                for requested in pairs or []:
                    if requested.replace("/", "").upper() == slashless:
                        friendly = requested
                        break
            if not friendly:
                continue
            base = cls._normalize_asset(info.get("base", ""))
            quote = cls._normalize_asset(info.get("quote", ""))
            result[friendly] = {
                "price_decimals": int(info.get("pair_decimals", cls.PRICE_DECIMALS_DEFAULT)),
                "ordermin": float(info.get("ordermin", 0.02)),
                "costmin": float(info.get("costmin", 0.5)),
                "base": base,
                "quote": quote,
                "lot_decimals": int(info.get("lot_decimals", 8)),
                "tick_size": info.get("tick_size"),
            }
        return result

    @classmethod
    def apply_pair_constants(cls, loaded: dict):
        """Merge dynamically loaded pair constants into the shared registry.

        Calls `PairRegistry.bootstrap_from_kraken(loaded)` — overlays
        precision/ordermin/costmin from live Kraken data. Idempotent.
        """
        cls.registry.bootstrap_from_kraken(loaded)

    # ─── Public Market Data ───

    @classmethod
    def ticker(cls, pair: str) -> dict:
        """Get current ticker data."""
        p = cls._resolve_pair(pair)
        data = cls._run(["ticker", p])
        if "error" in data:
            return data
        for key, val in data.items():
            if isinstance(val, dict) and "c" in val:
                return {
                    "pair": pair,
                    "price": float(val["c"][0]) if val.get("c") else 0,
                    "ask": float(val["a"][0]) if val.get("a") else 0,
                    "bid": float(val["b"][0]) if val.get("b") else 0,
                    "high_24h": float(val["h"][1]) if len(val.get("h", [])) > 1 else 0,
                    "low_24h": float(val["l"][1]) if len(val.get("l", [])) > 1 else 0,
                    "volume_24h": float(val["v"][1]) if len(val.get("v", [])) > 1 else 0,
                    "open": float(val.get("o", 0)),
                }
        return data

    @classmethod
    def ohlc(cls, pair: str, interval: int = 1) -> list:
        """Fetch OHLC candles. Returns list of candle dicts."""
        return cls.ohlc_paged(pair, interval=interval, since=0)[0]

    @classmethod
    def ohlc_paged(cls, pair: str, interval: int = 1, since: int = 0) -> tuple:
        """Like ohlc() but exposes the `last` cursor for pagination.

        Returns (candles: list, last_cursor: int). last_cursor is the timestamp
        of the most recent candle returned; pass it back as `since` for the
        next page. Returns (candles, 0) if no more data.
        """
        p = cls._resolve_pair(pair)
        args = ["ohlc", p, "--interval", str(interval)]
        if since > 0:
            args += ["--since", str(int(since))]
        data = cls._run(args)
        if isinstance(data, dict) and "error" in data:
            print(f"  [WARN] OHLC fetch error for {pair}: {data['error']}")
            return [], 0
        candles = []
        last_cursor = 0
        if isinstance(data, dict):
            if "last" in data:
                try:
                    last_cursor = int(data["last"])
                except (TypeError, ValueError):
                    last_cursor = 0
            for key, values in data.items():
                if key in ("error", "last"):
                    continue
                if isinstance(values, list):
                    for row in values:
                        if isinstance(row, list) and len(row) >= 7:
                            candles.append({
                                "timestamp": float(row[0]),
                                "open": float(row[1]),
                                "high": float(row[2]),
                                "low": float(row[3]),
                                "close": float(row[4]),
                                "volume": float(row[6]),
                            })
        return candles, last_cursor

    # ─── Private Account ───

    @staticmethod
    def balance() -> dict:
        """Get account balance. Returns {asset: amount} for non-zero balances."""
        data = KrakenCLI._run(["balance"])
        if isinstance(data, dict) and "error" not in data:
            return {k: float(v) for k, v in data.items() if float(v) > 0}
        return data

    # Field spellings Kraken/kraken-cli have used for "amount locked in open
    # orders". Probed in order; the first numeric hit wins. Unknown shapes
    # fall through to "nothing held", which reproduces the pre-v2.32 gross
    # behavior exactly — this path fails OPEN by construction.
    _HELD_KEYS = ("hold_trade", "holds", "held", "hold")

    @staticmethod
    def extended_balance() -> dict:
        """Get extended balances (`kraken extended-balance`).

        Returns Kraken's per-asset dict — typically
        {asset: {"balance": "...", "hold_trade": "...", "credit": ...}}.
        """
        return KrakenCLI._run(["extended-balance"])

    @classmethod
    def _extract_free(cls, info) -> Optional[float]:
        """Return spendable balance from one extended-balance entry.

        `balance` is the GROSS holding and includes funds locked behind
        resting orders; only `balance - held` is actually spendable. Returns
        None when the entry shape is unrecognized so callers can fail open.
        """
        if isinstance(info, (int, float)):
            return float(info)
        if isinstance(info, str):
            try:
                return float(info)
            except ValueError:
                return None
        if not isinstance(info, dict):
            return None
        try:
            total = float(info.get("balance"))
        except (TypeError, ValueError):
            return None
        held = 0.0
        for key in cls._HELD_KEYS:
            if key in info:
                try:
                    held = float(info[key])
                    break
                except (TypeError, ValueError):
                    continue
        return max(0.0, total - max(0.0, held))

    @classmethod
    def free_balance(cls) -> dict:
        """Return {asset: spendable_amount}, net of funds held in open orders.

        Motivation: `kraken balance` reports the GROSS holding. With a
        post-only BUY resting on the book its quote currency is committed but
        still counted, so the sizer re-spends it on the next tick and the
        exchange rejects the order — the `PLACEMENT_FAILED:
        insufficient_<quote>_balance` loop. Netting out holds removes the
        cause rather than absorbing the rejection.

        Fails open: on any CLI error or unrecognized payload shape this
        returns {} and callers keep using the gross balance.
        """
        data = cls.extended_balance()
        if not isinstance(data, dict) or "error" in data:
            return {}
        out = {}
        for asset, info in data.items():
            free = cls._extract_free(info)
            if free is None:
                continue
            if free > 0:
                out[asset] = free
        return out

    @staticmethod
    def trades_history(start: float = None, end: float = None) -> dict:
        """Get trade history, optionally filtered by time range.

        start/end: Unix timestamps. Returns {"count": N, "trades": {trade_id: {...}}}.
        """
        args = ["trades-history"]
        if start is not None:
            args.extend(["--start", str(start)])
        if end is not None:
            args.extend(["--end", str(end)])
        return KrakenCLI._run(args)

    @classmethod
    def volume(cls, pairs=None) -> dict:
        """Get 30-day trade volume and current fee tier.

        pairs: optional list of friendly pair symbols (e.g. ["SOL/USD","BTC/USD"])
        or a pre-formatted comma-separated string. Returns raw Kraken response dict,
        or {"error": ...} on failure.
        """
        args = ["volume"]
        if pairs:
            if isinstance(pairs, (list, tuple)):
                resolved = ",".join(cls._resolve_pair(p) for p in pairs)
            else:
                resolved = pairs
            args.extend(["--pair", resolved])
        return cls._run(args)

    # ─── Order Execution ───

    @classmethod
    def _assert_spot_limit_post_only(cls, order_type: str, post_only: bool) -> Optional[dict]:
        """Hard gate: Hydra places only limit post-only spot orders.

        Returns an error dict if rejected, else None. Opt-in market is
        intentionally unsupported (CLAUDE limit-post-only invariant).
        """
        ot = (order_type or "").strip().lower()
        if ot != "limit":
            return {
                "error": (
                    f"Hydra refuses order_type={order_type!r}; "
                    "only limit post-only is allowed"
                ),
            }
        if not post_only:
            return {
                "error": "Hydra refuses post_only=False; only limit post-only is allowed",
            }
        return None

    @classmethod
    def order_buy(cls, pair: str, volume: float, price: float = None,
                  order_type: str = "limit", post_only: bool = True,
                  validate: bool = False, userref: int = None) -> dict:
        """Place a buy order. Defaults to limit post-only (maker).

        `userref` is the numeric client tag that flows back to us via
        `order_userref` on the WS executions stream — our primary
        correlation key between a local journal entry and the exchange.

        v2.27.6: hard-rejects non-limit / non-post-only (defense in depth).
        """
        denied = cls._assert_spot_limit_post_only(order_type, post_only)
        if denied is not None:
            return denied
        p = cls._resolve_pair(pair)
        args = ["order", "buy", p, f"{volume:.8f}", "--type", "limit", "--yes"]
        if price is not None:
            args.extend(["--price", cls._format_price(pair, price)])
        args.extend(["--oflags", "post"])
        if userref is not None:
            args.extend(["--userref", str(int(userref))])
        if validate:
            args.append("--validate")
        return cls._run(args)

    @classmethod
    def order_sell(cls, pair: str, volume: float, price: float = None,
                   order_type: str = "limit", post_only: bool = True,
                   validate: bool = False, userref: int = None) -> dict:
        """Place a sell order. Defaults to limit post-only (maker).

        `userref` is the numeric client tag that flows back to us via
        `order_userref` on the WS executions stream — our primary
        correlation key between a local journal entry and the exchange.

        v2.27.6: hard-rejects non-limit / non-post-only (defense in depth).
        """
        denied = cls._assert_spot_limit_post_only(order_type, post_only)
        if denied is not None:
            return denied
        p = cls._resolve_pair(pair)
        args = ["order", "sell", p, f"{volume:.8f}", "--type", "limit", "--yes"]
        if price is not None:
            args.extend(["--price", cls._format_price(pair, price)])
        args.extend(["--oflags", "post"])
        if userref is not None:
            args.extend(["--userref", str(int(userref))])
        if validate:
            args.append("--validate")
        return cls._run(args)

    @staticmethod
    def query_orders(*txids, userref: int = None, trades: bool = False) -> dict:
        """Query specific orders by txid or userref.

        Returns {txid: {status, vol_exec, price, fee, ...}} for each order,
        or {"error": "..."} on failure.
        """
        args = ["query-orders"]
        if txids:
            args.extend([str(t) for t in txids])
        if userref is not None:
            args.extend(["--userref", str(userref)])
        if trades:
            args.append("--trades")
        return KrakenCLI._run(args)

    @staticmethod
    def cancel_order(*txids) -> dict:
        """Cancel specific order(s) by txid.

        Returns Kraken response (typically {"count": N}) or {"error": "..."}.
        """
        args = ["order", "cancel"]
        args.extend([str(t) for t in txids])
        args.append("--yes")
        return KrakenCLI._run(args)

    @staticmethod
    def cancel_after(seconds: int = 60) -> dict:
        """Dead man's switch — cancel all orders after timeout."""
        return KrakenCLI._run(["order", "cancel-after", str(seconds)])

    @staticmethod
    def cancel_all() -> dict:
        """Cancel all open orders."""
        return KrakenCLI._run(["order", "cancel-all", "--yes"])

    # ─── Paper Trading ───

    @classmethod
    def _paper_args(cls, side: str, pair: str, volume: float,
                    order_type: str = "limit", price: float = None) -> list:
        """Build argv for `kraken paper buy|sell`.

        Upstream contract (agents/tool-catalog.json, AGENTS.md, CONTEXT.md all
        agree): `kraken paper buy <PAIR> <VOLUME> [--type T] [--price P]`.
        VOLUME is POSITIONAL. Hydra previously passed it as `--volume`, which
        the CLI rejects as an unexpected argument with the required positional
        missing — every paper order failed at the CLI boundary. The whole test
        suite stubs `_run`, so no test ever saw the malformed argv.

        A limit order additionally requires `--price`; sending `--type limit`
        with no price is a validation error upstream. Callers that cannot
        supply a price fall back to `market` so paper mode still transacts.
        Note the spot paper engine models neither post-only nor partial fills
        (upstream "Limitations"), so paper fills remain an approximation of
        the live maker path — that gap is unchanged by this fix.
        """
        p = cls._resolve_pair(pair)
        ot = (order_type or "limit").strip().lower()
        if ot == "limit" and (price is None or float(price) <= 0):
            ot = "market"
        args = ["paper", side, p, f"{volume:.8f}", "--type", ot]
        if ot == "limit":
            args.extend(["--price", cls._format_price(pair, price)])
        return args

    @classmethod
    def paper_buy(cls, pair: str, volume: float, order_type: str = "limit",
                  price: float = None) -> dict:
        """Paper trade buy — no API keys needed."""
        return cls._run(cls._paper_args("buy", pair, volume, order_type, price))

    @classmethod
    def paper_sell(cls, pair: str, volume: float, order_type: str = "limit",
                   price: float = None) -> dict:
        """Paper trade sell — no API keys needed."""
        return cls._run(cls._paper_args("sell", pair, volume, order_type, price))

    @staticmethod
    def paper_balance() -> dict:
        """Get paper trading balance."""
        return KrakenCLI._run(["paper", "balance"])
