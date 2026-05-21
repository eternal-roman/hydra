"""Hydra Kraken CLI Wrapper."""
import subprocess
import json
import time
import os
import shlex
import asyncio
import threading
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone

WSL_DISTRO = os.environ.get("HYDRA_WSL_DISTRO", "Ubuntu")

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

class KrakenCLI:
    """Wraps kraken-cli v0.3.2 running in WSL Ubuntu.

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

    @staticmethod
    def _run(args: list, timeout: int = 20) -> dict:
        """Execute a kraken CLI command via WSL and return parsed JSON.

        Every arg is passed through `shlex.quote` before being joined
        into the bash -c string — internal callers use typed numerics
        and known-good pair names today, but the companion/dashboard
        surface is growing and a single unescaped caller would grant
        RCE in the WSL environment. v2.15.0 hardens the boundary.
        """
        quoted = " ".join(shlex.quote(str(a)) for a in args)

        # Multi-tenancy: inject dynamic API keys if provided in the process environment
        cmd_str = "source ~/.cargo/env"
        api_key = os.environ.get("KRAKEN_API_KEY")
        api_secret = os.environ.get("KRAKEN_API_SECRET")
        if api_key and api_secret:
            cmd_str += f" && export KRAKEN_API_KEY={shlex.quote(api_key)} && export KRAKEN_API_SECRET={shlex.quote(api_secret)}"

        cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
        cmd = ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd_str]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            stdout = result.stdout.strip()
            rc = result.returncode
            if not stdout:
                return {"error": f"Empty response (exit code {rc})"}
            data = json.loads(stdout)
            if isinstance(data, dict) and "error" in data:
                return data
            if rc != 0:
                # Non-zero exit with parseable stdout: surface the failure so
                # callers don't treat partial output as success.
                return {"error": f"Non-zero exit code {rc}", "partial": data}
            return data
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "retryable": True}
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error: {e}", "raw": stdout[:200] if stdout else ""}
        except Exception as e:
            return {"error": str(e)}

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
    def order_buy(cls, pair: str, volume: float, price: float = None,
                  order_type: str = "limit", post_only: bool = True,
                  validate: bool = False, userref: int = None) -> dict:
        """Place a buy order. Defaults to limit post-only (maker).

        `userref` is the numeric client tag that flows back to us via
        `order_userref` on the WS executions stream — our primary
        correlation key between a local journal entry and the exchange.
        """
        p = cls._resolve_pair(pair)
        args = ["order", "buy", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", cls._format_price(pair, price)])
        if post_only and order_type == "limit":
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
        """
        p = cls._resolve_pair(pair)
        args = ["order", "sell", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", cls._format_price(pair, price)])
        if post_only and order_type == "limit":
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
    def paper_buy(cls, pair: str, volume: float, order_type: str = "limit") -> dict:
        """Paper trade buy — no API keys needed."""
        p = cls._resolve_pair(pair)
        return cls._run(["paper", "buy", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @classmethod
    def paper_sell(cls, pair: str, volume: float, order_type: str = "limit") -> dict:
        """Paper trade sell — no API keys needed."""
        p = cls._resolve_pair(pair)
        return cls._run(["paper", "sell", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @staticmethod
    def paper_balance() -> dict:
        """Get paper trading balance."""
        return KrakenCLI._run(["paper", "balance"])
