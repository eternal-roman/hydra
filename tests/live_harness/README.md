# Hydra Live-Execution Test Harness

Drives `HydraAgent._place_order` across every code path — happy, failure,
edge, schema, rollback, WS lifecycle, historical regression, and real-Kraken — to catch
wiring bugs that unit tests miss. **This harness surfaced HF-001 through
HF-004 on its first run.** Any PR touching the execution path should use it.

## Mandatory for PRs touching

`hydra_agent.py:_place_order`, `_place_paper_order`, `ExecutionStream`,
the tick-loop wrapper at lines 2155-2193, any `order_journal.append` site, or
`hydra_engine.py:execute_signal`/`_maybe_execute`/`snapshot_position`/
`restore_position`/`PositionSizer.calculate`.

## Run modes

| Mode | Duration | Cost | What it runs |
|---|---|---|---|
| `smoke` | ~1.5s | $0 | Import + agent construction only |
| `mock` *(default)* | ~1.5s | $0 | **35** scenarios via monkey-patched Kraken (H/F/E/S/R/Hp/W) — CI gate |
| `validate` | ~10s | $0 | 3 scenarios hitting real Kraken read-only + `--validate` |
| `live` | ~3min | <$0.01 | 7 scenarios with real post-only orders + immediate cancel |

Fast mock mode is achieved by monkey-patching `time.sleep` to a no-op during
mock runs — Hydra's rate-limit sleeps are only meaningful for real API calls.

Live mode requires the `--i-understand-this-places-real-orders` flag as an
explicit opt-in. Orders are placed at non-crossing prices (cannot fill) and
cancelled within 5 seconds. An exception handler calls `kraken order
cancel-all` as a final safety net.

## Usage

```bash
python tests/live_harness/harness.py --mode smoke            # default pre-flight
python tests/live_harness/harness.py --mode mock             # default full suite
python tests/live_harness/harness.py --mode mock --scenario H3
python tests/live_harness/harness.py --mode mock --json report.json
python tests/live_harness/harness.py --mode validate         # real Kraken, no real orders
python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
```

Exit codes: `0` all passed, `1` scenario failure, `2` harness setup error.

## Scenario catalog

Source of truth is `scenarios.py` → `ALL_SCENARIOS`. Mock CI runs the registered
subset that currently reports **35/35**; full registry includes live-only (`L*`)
scenarios not executed in `mock` mode. Categories:

| Prefix | Category | Count | What it tests |
|---|---|---|---|
| `H*` | Happy path | 6 | Paper/live buy/sell, mocked and real |
| `F*` | Failure path | 7 | Each `_place_order` failure branch + 13-field rollback check |
| `E*` | Edge case | 7 | Txid shapes, halted engine, ordermin, unparseable JSON |
| `S*` | Schema meta | 1 | Validator sanity check |
| `R*` | Rollback meta | 1 | Comparator sanity check |
| `Hp*` | Historical regression | 6 | Named for the commit that fixed the original bug |
| `L*` | Live only | 6 | Real Kraken — ticker, validate, post-only + cancel per pair |
| `W*` | WS lifecycle | 7 | ExecutionStream lifecycle transitions via FakeExecutionStream |

Every `H/F/E` scenario calls `validate_entry(entry, expected_state=...)`,
so schema compliance is enforced implicitly for all production entries.
Every `F` scenario runs through `_run_with_rollback_check`, which asserts
all 13 engine fields restore exactly to pre-trade state.

## Architecture

```
tests/live_harness/
├── __init__.py          Package marker
├── harness.py           Harness class, CLI entry, harness_execute wrapper
├── scenarios.py         All 41+ scenarios + ALL_SCENARIOS registry
├── schemas.py           Per-state order journal schemas + validate_entry()
├── state_comparator.py  13-field rollback comparator
├── stubs.py             StubRun + Kraken response builders
└── README.md            This file
```

**Isolation guarantees** (all in `harness.py`):
1. **No `run()` call, ever.** Harness uses `_place_order` directly; the
   rolling log file is written only by `run()`, so harness never touches it.
2. **Tuner save neutralized** — `ParameterTracker._save` patched to no-op.
3. **Brain disabled** — LLM API env vars unset; `HydraBrain` is always `None`.
4. **Broadcaster** — `.start()` patched to no-op (defensive; `__init__`
   doesn't call it anyway).

**The execute wrapper** `harness_execute()` reproduces the tick-loop wrapper
at `hydra_agent.py:2155-2193`: snapshot → `execute_signal` → `_place_order` →
rollback on failure. Returns a report dict with `outcome`, `pre_snap`,
`trade`, `trade_dict`, `last_journal_entry` for post-scenario assertions.

## Findings tracker

Every bug discovered by the harness is tracked with a stable `HF-###` ID.
Severity scale: **S1** critical (blocks any PR), **S2** latent (blocks any
PR that would trigger it), **S3** defensive (fix opportunistically), **S4**
cosmetic. Every finding must have regression coverage before closing.

| ID | Title | Sev | Status | Fix commit | Regression test |
|---|---|---|---|---|---|
| HF-001 | `KrakenCLI` hardcoded `.8f` price precision | S2 | **Closed** | PR #36 | `TestPriceFormat` (14 tests) + L2 live-mode |
| HF-002 | `execute_signal` bypasses halt check | S3 | **Closed** | PR #36 | `TestHaltedEngineExecuteSignal` (3 tests) |
| HF-003 | Silent `except Exception: pass` in rolling log writer | S3 | **Closed** | PR #36 | Replaced with logged warning; visual inspection |
| HF-004 | Trade persistence silently failing (tick crash + snapshot missing `trades`) | **S1** | **Closed** | PR #36 | `TestSnapshotTradesRoundTrip` (7 tests); tick-body try/except surfaces future crashes to `hydra_errors.log` |

See `git show 0621e8a` for the PR #36 merge commit with full fix descriptions.

## Scenario authoring guide

Every scenario is a single function that takes a `Harness` and raises on
failure. Register it in `ALL_SCENARIOS` at the bottom of `scenarios.py`.

### Happy-path template

```python
def scenario_H9_your_name(h: Harness):
    """One-sentence description of what this verifies."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "paper_buy": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "H9 description")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    validate_entry(report["last_journal_entry"], expected_state="FILLED")
```

### Failure-path template (with rollback check)

```python
def scenario_F8_your_failure(h: Harness):
    """One-sentence description of the failure branch."""
    _run_with_rollback_check(
        h, "F8",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_error("EOrder:Your specific error"),
        })),
        action="BUY", confidence=0.75,
        expected_state="PLACEMENT_FAILED",
    )
```

`_run_with_rollback_check` captures pre-trade state, runs the scenario,
asserts the status, and verifies all 13 rollback fields match the snapshot.

### Registration

```python
Scenario("H9", "Your description", "H", MOCK, scenario_H9_your_name),
```

Fields: stable `code` (never reuse), `name`, `category` (one letter), `modes`
(`MOCK` / `LIVE` / `LIVE_ONLY` / `VALIDATE_ONLY`), `fn`.

## Continuity protocol

**When to run:**

| Change touches | Required modes |
|---|---|
| `_place_order`, `_place_paper_order`, order journal write sites | `mock` + `validate` + `live` for high-risk |
| `ExecutionStream` | `mock` + `live` recommended |
| `execute_signal`, `_maybe_execute`, `snapshot_*`, `restore_*`, `PositionSizer.calculate` | `mock` |
| `KrakenCLI.order_buy`/`order_sell`/`order_amend`/`ticker` | `mock` + `validate` |
| Any order_journal entry schema | `mock` |
| Signal/regime/indicators | (not on execution path — `test_engine.py` covers this) |
| Any field added to `snapshot_position`/`snapshot_runtime` | `mock` + update `state_comparator.py` **in the same PR** |

**How to respond to a finding:**
1. Never weaken an assertion to silence a failure. Fix the bug or document why.
2. Every bug gets an `HF-###` entry with severity, status, regression test.
3. **S1** blocks the PR. **S2** blocks any PR that triggers the latent path.
   **S3** opportunistic. **S4** logged but not blocking.
4. A fix closing a finding must reference a regression test.

**CI gate:** the `smoke` + `mock` modes run in GitHub Actions on every PR
(~3 seconds added to CI). See `.github/workflows/ci.yml`.

## Field-sync checklist — READ BEFORE MODIFYING `HydraEngine`

The rollback comparator has a hardcoded list of engine fields. If you add a
field to `HydraEngine` that's serialized by `snapshot_position()` or
`snapshot_runtime()`, you **MUST** add it to `capture_engine_state()` in
`state_comparator.py` in the **same PR**. Missing fields silently pass
rollback tests while rollback is actually incomplete — exactly the bug class
that commit `4effbea` fixed.

Current 15 fields (snapshot_position/runtime must agree; comparator covers rollback-relevant subset):

| Field | `snapshot_position` | `snapshot_runtime` | comparator |
|---|---|---|---|
| `balance` | ✓ | ✓ | ✓ |
| `position.size` | ✓ | ✓ | ✓ |
| `position.avg_entry` | ✓ | ✓ | ✓ |
| `position.realized_pnl` | ✓ | ✓ | ✓ |
| `position.params_at_entry` | ✓ | ✓ | ✓ |
| `total_trades` | ✓ | ✓ | ✓ |
| `win_count` | ✓ | ✓ | ✓ |
| `loss_count` | ✓ | ✓ | ✓ |
| `len(trades)` | ✓ | ✓ *(HF-004: full list now persisted)* | ✓ |
| `len(equity_history)` | ✓ | ✓ | ✓ |
| `peak_equity` | ✓ | ✓ | ✓ |
| `max_drawdown` | ✓ | ✓ | ✓ |
| `gross_profit` | ✓ | ✓ | — |
| `gross_loss` | ✓ | ✓ | — |
| `halted` | — | ✓ | ✓ |

When adding a field, add a row to this table in the same PR. That's how the
table stays truthful.
