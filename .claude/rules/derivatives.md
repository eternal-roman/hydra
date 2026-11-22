---
paths:
  - "hydra_derivatives_stream.py"
  - "hydra_quant_rules.py"
  - "hydra_agent.py"
  - "hydra_pair_registry.py"
  - "hydra_config.py"
  - "tests/test_derivatives_stream.py"
  - "tests/test_quant_rules.py"
---

# Derivatives coverage and funding invariants (HIGH severity if violated)

Moved verbatim from `CLAUDE.md` §Guardrails and the LLM. Loads when the
files above are touched.

- **Pairs declare their derivatives coverage to R10 structurally** (from `SPOT_TO_DERIVATIVES`, never from data presence), or R10 force-holds them every tick: synthetic pairs set `DerivativesSnapshot.synthetic=True` → `quant_indicators["synthetic_pair"]` (tracks funding/cvd/regime only); perp-only pairs (ZEC `PF_ZECUSD`; `SPOT_TO_DERIVATIVES.quarterly_prefix is None`) get `DerivativesSnapshot.basis_available=False` → `quant_indicators["basis_available"]` (4 fields, no `basis_apr_pct`); unmapped pairs (satellites, e.g. NIGHT/USD) get `quant_indicators["derivatives_covered"]=False` from `_build_quant_indicators` (CVD only). A covered pair with a warming/stale stream must still hit the R10 blackout. A new pair without a direct perp needs the matching flag.
- **Funding is markPrice-relative and hourly** — `PF_*` `fundingRate` is absolute USD per contract per period: bps = `(fundingRate / markPrice) * 10000` via `_absolute_to_relative_bps` (±500 bps clamp), never `fundingRate * 10000`. PF funding settles hourly, so `_funding_bps_8h` scales by `FUNDING_PERIODS_PER_8H` (8) and applies the ±500 bound to the 8h value (unscaled, R1/R2's 80 bps/8h extreme never fired). Pre-v2.15.2 fires are not authoritative. Hourly settlement is unverified against the live API — confirm on the first live run.
