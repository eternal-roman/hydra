---
name: audit
description: Run a comprehensive Hydra codebase audit. Use when the user says /audit, asks for a code audit, requests a zero-skip review, or wants parallel agents to find bugs. Spawns 7 parallel exploration agents across natural file-group partitions, triages findings HIGH/MED/LOW, and runs a self-audit before declaring done.
---

# Audit

You are running a Hydra codebase audit. The pattern below has previously
caught 7+ bugs per session that single-pass review missed.

## Partitions (spawn 7 parallel Task agents, subagent_type: explore)

1. Engine + tuner: `hydra_engine.py`, `hydra_tuner.py`
2. Agent + streams: `hydra_agent.py` + the `BaseStream` subclasses
   (`ExecutionStream`, `CandleStream`, `TickerStream`, `BalanceStream`, `BookStream`)
3. AI layer: `hydra_brain.py`
4. Backtest platform: `hydra_backtest*.py`, `hydra_experiments.py`
5. Companion subsystem: `hydra_companions/` package + soul JSONs
6. Dashboard: `dashboard/src/App.jsx` (single-file React)
7. Tests: `tests/` + `tests/live_harness/`

Each agent should return a structured findings list with: file:line, severity,
category, description, suggested fix.

## Severity rubric

**HIGH** — fix immediately:
- Violation of safety invariants I1-I12 (see CLAUDE.md §Backtesting)
- Limit-post-only rule violated (any market-order path)
- 2 s rate-limit floor violated
- 15 % circuit-breaker bypassed
- Wilder-EMA RSI/ATR replaced with simpler formula
- `HYDRA_COMPANION_LIVE_EXECUTION` default-off contract violated
- Secret/credential leak (.env, API keys logged)

**MEDIUM** — fix before next release:
- Naming inconsistencies vs CLAUDE.md §Naming
- Stale doc references
- Test coverage gaps in changed code paths
- Sub-optimal but non-buggy patterns

**LOW** — backlog:
- Style nits, minor refactor opportunities, dead code

## Two-phase self-audit (mandatory — Operating Rule 4)

Past failure: the journal-maintenance-tool session caught 7 bugs across
two self-audit rounds that single-pass review missed.

**Phase 1** — after writing fixes, audit your own diff for:
- Unused imports, dead code
- Unhandled exceptions
- Null/empty crashes
- Deprecated API usage
- Misleading error messages
- False-positive checks
- Re-run the 7-partition sweep against your diff
- Run the full §Testing block from CLAUDE.md
- Run `python tests/live_harness/harness.py --mode mock`

Fix everything found, then run **Phase 2** — repeat the same sweep
against the post-Phase-1 diff. If Phase 2 surfaces new HIGH items, loop
back through both phases. Only declare done when Phase 2 is clean.

## Output

Produce an `AUDIT_<date>.md` file at the repo root summarizing:
- Findings count by severity
- Files touched in fixes
- Self-audit result (Phase 1 and Phase 2 separately)
- Any deferred LOW items added to backlog

## Operating Rules invoked

This skill invokes §Operating Rules in CLAUDE.md:
- Rule 1 (parallel Task agents) — partition sweep
- Rule 3 (verify claims with actual commands) — every "fixed" claim
  must be backed by a re-run of the relevant test or grep
- Rule 4 (two-phase self-audit) — mandatory before declaring done
