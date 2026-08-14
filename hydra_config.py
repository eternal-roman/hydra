"""Role-bound trading triangle + quote CLI.

`TradingTriangle` names pairs by role (`stable_sol`, `stable_btc`,
`bridge`) so coordinator/agent code never hardcodes `SOL/USD`.
`HydraConfig.from_quote` builds a triangle. `add_config_args` registers
`--quote` (env `HYDRA_QUOTE`, default USD) for `--pairs auto`.

Invariants: both stable legs share `quote ∈ STABLE_QUOTES`; bridge is
always SOL/BTC.
"""

from __future__ import annotations

import os
import argparse
from dataclasses import dataclass
from typing import Optional

from hydra_pair_registry import (
    Pair,
    PairRegistry,
    STABLE_QUOTES,
    default_registry,
)


# ═══════════════════════════════════════════════════════════════════
# Defaults
# ═══════════════════════════════════════════════════════════════════

# Default stable quote for v2.19+. Pre-v2.19 default was USDC.
DEFAULT_QUOTE = "USD"

# Bridge asset spec. Hydra's strategy hinges on SOL/BTC as the cross-
# asset bridge. If you ever want a different cross asset, that is a
# strategy change, not a config flip — change here intentionally.
_BRIDGE_BASE = "SOL"
_BRIDGE_QUOTE = "BTC"

# The two stable-quoted base assets in the triangle.
_STABLE_BASES = ("SOL", "BTC")


# ═══════════════════════════════════════════════════════════════════
# Triangle
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TradingTriangle:
    """Role-bound trio of Pairs that defines Hydra's trading universe.

    Roles:
      stable_sol — the SOL pair quoted in the active stable currency
                   (e.g. SOL/USD when quote=USD; SOL/USDC when quote=USDC).
      stable_btc — the BTC pair quoted in the active stable currency.
      bridge     — the SOL/BTC cross. Quote-independent.

    Strategy code references these by role, never by Kraken pair name.
    """
    stable_sol: Pair
    stable_btc: Pair
    bridge: Pair
    quote: str

    def __post_init__(self):
        # Defensive: triangle integrity must hold or downstream logic
        # silently misroutes. These were previously implicit because
        # the code hardcoded the right pair names.
        if self.stable_sol.quote != self.quote:
            raise ValueError(
                f"stable_sol quote {self.stable_sol.quote!r} ≠ triangle quote {self.quote!r}"
            )
        if self.stable_btc.quote != self.quote:
            raise ValueError(
                f"stable_btc quote {self.stable_btc.quote!r} ≠ triangle quote {self.quote!r}"
            )
        if self.bridge.base != _BRIDGE_BASE or self.bridge.quote != _BRIDGE_QUOTE:
            raise ValueError(
                f"bridge {self.bridge.cli_format} ≠ {_BRIDGE_BASE}/{_BRIDGE_QUOTE}"
            )

    def as_tuple(self) -> tuple[Pair, Pair, Pair]:
        """(stable_sol, stable_btc, bridge) — useful for iteration."""
        return (self.stable_sol, self.stable_btc, self.bridge)


# ═══════════════════════════════════════════════════════════════════
# Top-level config
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class HydraConfig:
    """Boot-time configuration, threaded through every subsystem."""
    quote: str
    registry: PairRegistry
    triangle: TradingTriangle

    @property
    def primary_quote(self) -> str:
        """Alias for `quote`. Some consumers read `cfg.primary_quote` to
        emphasize that this is the engine's quote of record (vs an
        ad-hoc per-pair quote)."""
        return self.quote

    @property
    def pairs(self) -> tuple[Pair, Pair, Pair]:
        """The three Pair objects in the active triangle."""
        return self.triangle.as_tuple()

    def pair_symbols(self) -> tuple[str, str, str]:
        """`cli_format` strings for the active triangle (e.g.
        ('SOL/USD', 'BTC/USD', 'SOL/BTC')). Use when a downstream
        surface still wants strings (CLI args, JSON, log lines)."""
        return tuple(p.cli_format for p in self.pairs)

    # ─── Constructors ───

    @classmethod
    def from_quote(
        cls,
        quote: str,
        registry: Optional[PairRegistry] = None,
    ) -> "HydraConfig":
        """Build a HydraConfig for a given stable quote.

        Raises ValueError if the quote is not in STABLE_QUOTES, or if
        the registry doesn't contain the required SOL/QUOTE, BTC/QUOTE,
        and SOL/BTC pairs.
        """
        q = (quote or "").strip().upper()
        if q not in STABLE_QUOTES:
            raise ValueError(
                f"Unsupported quote {q!r}; supported stable quotes: "
                f"{sorted(STABLE_QUOTES)}"
            )
        reg = registry if registry is not None else default_registry()

        stable_sol = reg.get(f"SOL/{q}")
        if stable_sol is None:
            raise ValueError(
                f"Registry missing SOL/{q} — required for triangle"
            )
        stable_btc = reg.get(f"BTC/{q}")
        if stable_btc is None:
            raise ValueError(
                f"Registry missing BTC/{q} — required for triangle"
            )
        bridge = reg.get(f"{_BRIDGE_BASE}/{_BRIDGE_QUOTE}")
        if bridge is None:
            raise ValueError(
                f"Registry missing {_BRIDGE_BASE}/{_BRIDGE_QUOTE} — "
                f"required as triangle bridge"
            )

        triangle = TradingTriangle(
            stable_sol=stable_sol,
            stable_btc=stable_btc,
            bridge=bridge,
            quote=q,
        )
        return cls(quote=q, registry=reg, triangle=triangle)

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        registry: Optional[PairRegistry] = None,
    ) -> "HydraConfig":
        """Build from argparse Namespace populated by `add_config_args`.

        Resolution order for quote: explicit --quote > HYDRA_QUOTE env >
        DEFAULT_QUOTE.
        """
        # `args.quote` already encodes the resolution (CLI > env > default)
        # because add_config_args wires it that way.
        return cls.from_quote(args.quote, registry=registry)


# ═══════════════════════════════════════════════════════════════════
# CLI integration
# ═══════════════════════════════════════════════════════════════════

def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Register quote-selection args on an existing parser.

    Adds `--quote` with default resolved from `HYDRA_QUOTE` env or
    `DEFAULT_QUOTE`. CLI explicit value wins over env (argparse behavior:
    user-provided value overrides default).
    """
    env_quote = os.environ.get("HYDRA_QUOTE", "").strip().upper()
    default = env_quote if env_quote in STABLE_QUOTES else DEFAULT_QUOTE
    parser.add_argument(
        "--quote",
        default=default,
        type=lambda s: s.strip().upper(),
        choices=sorted(STABLE_QUOTES),
        help=(
            f"Fallback quote for --pairs auto (default: {default}; "
            f"env: HYDRA_QUOTE; choices: {sorted(STABLE_QUOTES)}). "
            f"Explicit --pairs is unchanged."
        ),
    )
