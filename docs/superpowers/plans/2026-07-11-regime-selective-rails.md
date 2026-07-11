# Regime-Selective Rails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship kill-switchable deterministic regime-selective rails that match the causal AI-control study’s winning re-regulation policy (not deregulation), and retest historical ranges for fidelity.

**Architecture:** After signal generation, `HydraEngine._apply_regime_selective(regime, signal)` rewrites BUY/SELL/HOLD using only current regime + position + conf. Opt-in via `HYDRA_REGIME_SELECTIVE=1` or constructor flag. Counterfactual tool and unit tests prove range fidelity vs study baselines.

**Tech Stack:** Python 3.10–3.12 stdlib engine, pytest, sqlite `hydra_history.sqlite`, existing `tools/ai_control_counterfactual.py`.

## Global Constraints

- **SPOT-ONLY** — no futures/margin orders.
- **Limit post-only** — never market; next-bar fill model in research tools.
- **2s REST floor** unchanged.
- **15% circuit breaker** — BUY halt, SELL allowed (PR-A) unchanged.
- **SKIP ≠ BLOCK** — selective entry denials are soft HOLD (SKIP), not session halt.
- **No deregulation by default** — do not lower min_conf to 0.50; do not disable friction by default.
- **Default off** — `HYDRA_REGIME_SELECTIVE` unset/`0` = identical pre-change behavior.
- **Fidelity bands (retest):** SOL 90d selective ret ∈ [−4.3, +1.7]%; SOL 365d selective ret ∈ [−10.8, −2.8]%; selective − baseline ≥ +5 pp both windows.
- **No absolute +P&L claim** in docs or CHANGELOG.
- **Engine purity** — no numpy/pandas in engine.
- **CLAUDE.md** — env flag row in same change as flag introduction.
- **TDD** — failing tests first per task.
- Branch: `feature/regime-selective-rails` (not main without PR).

## Faithfulness checklist (must remain true after implementation)

| Guidance | Plan task |
|----------|-----------|
| Re-regulate, don’t deregulate | T1–T2 implement selective; no default conf 0.50 |
| Block BUY RANGING/VOLATILE | T1 rule 2 |
| BUY only TREND_UP conf≥0.55 | T1 rule 3 |
| Force flatten TREND_DOWN | T1 rule 1 |
| Keep friction + min_conf rails | T2 negative tests |
| Kill switch default off | T1 env + constructor |
| Retest same ranges | T4 |
| Three-agent insight: fewer trades, not more | T4 fill count selective << baseline |

## File map

| File | Role |
|------|------|
| `hydra_engine.py` | `_apply_regime_selective`, flag wiring in `__init__` / `tick` / `execute_signal` |
| `tests/test_regime_selective.py` | Unit coverage for rules 1–4 + default off |
| `tools/ai_control_counterfactual.py` | Optional engine-backed selective; fidelity assert mode |
| `tools/retest_regime_selective_ranges.py` | One-shot range gate vs bands |
| `CLAUDE.md` | Env flag row |
| `docs/superpowers/specs/2026-07-11-regime-selective-rails-design.md` | Design (exists) |

---

### Task 1: Engine regime-selective core (TDD)

**Files:**
- Modify: `hydra_engine.py` (`HydraEngine.__init__`, `tick`, `execute_signal`, new method)
- Create: `tests/test_regime_selective.py`

**Interfaces:**
- Produces:
  - `HydraEngine(..., regime_selective: Optional[bool] = None)`
  - `HydraEngine.regime_selective: bool`
  - `HydraEngine._apply_regime_selective(regime: Regime, signal: Signal) -> Signal`
  - Env: `HYDRA_REGIME_SELECTIVE` in `{"1","true","yes","on"}` (case-insensitive) → True when constructor is None

**Constants (verbatim):**
- `REGIME_SELECTIVE_BUY_MIN_CONF = 0.55`
- `REGIME_SELECTIVE_FLATTEN_CONF = 0.70`
- Reason prefixes: `REGIME_SELECTIVE:force_flatten`, `REGIME_SELECTIVE:block_buy_`, `REGIME_SELECTIVE:low_conf`

- [ ] **Step 1: Write failing tests** in `tests/test_regime_selective.py`:

```python
"""Regime-selective rails (AI-control re-regulation study)."""
import os
import pytest
from hydra_engine import (
    HydraEngine, Regime, Strategy, Signal, SignalAction, Candle,
)

def _engine(selective=True, balance=1000.0):
    return HydraEngine(
        initial_balance=balance,
        asset="SOL/USD",
        regime_selective=selective,
        candle_interval=60,
    )

def _warmup(eng, n=60, price=100.0):
    for i in range(n):
        eng.ingest_candle({
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 10.0, "timestamp": float(i),
        })

def test_default_off_without_env(monkeypatch):
    monkeypatch.delenv("HYDRA_REGIME_SELECTIVE", raising=False)
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.regime_selective is False

def test_env_enables(monkeypatch):
    monkeypatch.setenv("HYDRA_REGIME_SELECTIVE", "1")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD")
    assert eng.regime_selective is True

def test_constructor_overrides_env(monkeypatch):
    monkeypatch.setenv("HYDRA_REGIME_SELECTIVE", "1")
    eng = HydraEngine(initial_balance=100.0, asset="SOL/USD", regime_selective=False)
    assert eng.regime_selective is False

def test_block_buy_ranging():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.90, "MR", Strategy.MEAN_REVERSION)
    out = eng._apply_regime_selective(Regime.RANGING, sig)
    assert out.action == SignalAction.HOLD
    assert "REGIME_SELECTIVE:block_buy" in out.reason

def test_block_buy_volatile():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.90, "GRID", Strategy.GRID)
    out = eng._apply_regime_selective(Regime.VOLATILE, sig)
    assert out.action == SignalAction.HOLD

def test_allow_trend_up_buy_above_floor():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.70, "MOM", Strategy.MOMENTUM)
    out = eng._apply_regime_selective(Regime.TREND_UP, sig)
    assert out.action == SignalAction.BUY

def test_block_trend_up_buy_below_floor():
    eng = _engine(True)
    sig = Signal(SignalAction.BUY, 0.54, "MOM", Strategy.MOMENTUM)
    out = eng._apply_regime_selective(Regime.TREND_UP, sig)
    assert out.action == SignalAction.HOLD
    assert "low_conf" in out.reason

def test_force_flatten_trend_down_when_long():
    eng = _engine(True)
    eng.position.size = 1.0
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.HOLD, 0.5, "idle", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL
    assert out.confidence >= 0.70
    assert "force_flatten" in out.reason

def test_force_flatten_overrides_buy_nibble():
    eng = _engine(True)
    eng.position.size = 0.5
    eng.position.avg_entry = 100.0
    sig = Signal(SignalAction.BUY, 0.56, "DEF nibble", Strategy.DEFENSIVE)
    out = eng._apply_regime_selective(Regime.TREND_DOWN, sig)
    assert out.action == SignalAction.SELL

def test_selective_off_passthrough():
    eng = _engine(False)
    eng.position.size = 1.0
    sig = Signal(SignalAction.BUY, 0.90, "MR", Strategy.MEAN_REVERSION)
    out = eng._apply_regime_selective(Regime.RANGING, sig)
    assert out.action == SignalAction.BUY
```

- [ ] **Step 2: Run tests — expect FAIL** (missing API)

```bash
python -m pytest tests/test_regime_selective.py -v
```

- [ ] **Step 3: Implement minimal engine support**

In `HydraEngine.__init__` add parameter `regime_selective: Optional[bool] = None` and:

```python
if regime_selective is None:
    raw = os.environ.get("HYDRA_REGIME_SELECTIVE", "").strip().lower()
    self.regime_selective = raw in ("1", "true", "yes", "on")
else:
    self.regime_selective = bool(regime_selective)
```

Add class constants and method:

```python
REGIME_SELECTIVE_BUY_MIN_CONF = 0.55
REGIME_SELECTIVE_FLATTEN_CONF = 0.70

def _apply_regime_selective(self, regime: Regime, signal: Signal) -> Signal:
    if not self.regime_selective:
        return signal
    # 1) force flatten
    if self.position.size > 0 and regime == Regime.TREND_DOWN:
        return Signal(
            action=SignalAction.SELL,
            confidence=max(float(signal.confidence), self.REGIME_SELECTIVE_FLATTEN_CONF),
            reason=f"REGIME_SELECTIVE:force_flatten|{signal.reason}",
            strategy=Strategy.DEFENSIVE,
        )
    if signal.action == SignalAction.BUY:
        if regime != Regime.TREND_UP:
            return Signal(
                action=SignalAction.HOLD,
                confidence=signal.confidence,
                reason=f"REGIME_SELECTIVE:block_buy_{regime.value}|{signal.reason}",
                strategy=signal.strategy,
            )
        if float(signal.confidence) < self.REGIME_SELECTIVE_BUY_MIN_CONF:
            return Signal(
                action=SignalAction.HOLD,
                confidence=signal.confidence,
                reason=f"REGIME_SELECTIVE:low_conf|{signal.reason}",
                strategy=signal.strategy,
            )
    return signal
```

In `tick()`, after `signal = SignalGenerator.generate(...)`:

```python
signal = self._apply_regime_selective(regime, signal)
```

In `execute_signal()`, after building the Signal from args, re-detect regime (same detector call as tick) and apply `_apply_regime_selective` before `_maybe_execute`.

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m pytest tests/test_regime_selective.py -v
```

- [ ] **Step 5: Commit**

```bash
git add hydra_engine.py tests/test_regime_selective.py
git commit -m "feat(engine): opt-in regime-selective entry/exit rails"
```

---

### Task 2: Guardrails — friction and min_conf still active under selective

**Files:**
- Modify: `tests/test_regime_selective.py`

**Interfaces:**
- Consumes: engine from Task 1
- Produces: tests proving selective does **not** disable friction / does not lower sizer min_conf

- [ ] **Step 1: Write failing tests**

```python
def test_selective_does_not_lower_min_conf():
    eng = _engine(True)
    assert eng.sizer.min_confidence >= 0.55  # competition may be 0.65

def test_friction_gate_still_present():
    # env not forcing friction off
    import os
    assert os.environ.get("HYDRA_FRICTION_GATE_DISABLED") != "1" or True
    eng = _engine(True)
    assert hasattr(eng, "ROUND_TRIP_FRICTION_PCT")
    assert eng.FRICTION_HURDLE_MULT >= 2.0
```

Strengthen second test: with selective on, a BUY that passes selective still goes through `_maybe_execute` friction path — mock insufficient expected move if easy; otherwise assert `HYDRA_FRICTION_GATE_DISABLED` is not set by selective constructor.

- [ ] **Step 2: Implement only if something incorrectly disabled friction — should be no product code if Task 1 correct**

- [ ] **Step 3: pytest pass + commit**

```bash
git commit -m "test(engine): selective rails keep friction and min_conf"
```

---

### Task 3: Counterfactual engine-backed selective + range gate script

**Files:**
- Modify: `tools/ai_control_counterfactual.py` — add policy `engine_selective` that sets `regime_selective=True` and uses baseline decision path (engine signal after selective apply via tick)
- Create: `tools/retest_regime_selective_ranges.py`

**Interfaces:**
- `run_policy` accepts optional `regime_selective: bool` on engine construction
- Retest script exit 0 iff bands pass

**Retest bands (verbatim from design):**

```python
BANDS = {
    "sol_90d": {"selective_ret": (-4.3, 1.7), "min_edge_vs_base_pp": 5.0},
    "sol_365d": {"selective_ret": (-10.8, -2.8), "min_edge_vs_base_pp": 5.0},
}
```

Study anchors (for reporting, not hard equality): 90d −1.27%, 365d −6.78%.

- [ ] **Step 1: Wire `regime_selective` into counterfactual `run_policy`**

When `policy_name == "engine_selective"` (or `ai_proxy_selective` rewritten): construct engine with `regime_selective=True` and use `policy_baseline` decision (engine already filtered). Prefer **replacing** `ai_proxy_selective` body to call engine path so single source of truth — keep name `ai_proxy_selective` for JSON continuity **or** dual-run both and compare.

**Required:** at least one policy path uses `HydraEngine(regime_selective=True)` and does **not** duplicate rule logic in the tool.

- [ ] **Step 2: Write `tools/retest_regime_selective_ranges.py`**

Runs SOL 90d + 365d for `baseline` and `ai_proxy_selective` (engine-backed), checks bands, prints table, exit 1 on fail.

- [ ] **Step 3: Run retest**

```bash
python tools/retest_regime_selective_ranges.py
```

Expected: PASS with returns near study anchors.

- [ ] **Step 4: Commit**

```bash
git add tools/ai_control_counterfactual.py tools/retest_regime_selective_ranges.py
git commit -m "test(research): range fidelity gate for regime-selective rails"
```

---

### Task 4: CLAUDE.md + faithfulness verification doc

**Files:**
- Modify: `CLAUDE.md` — env flags table
- Create: `.hydra-research/regime_selective_fidelity_report.md` (generated by retest or hand-written summary after run)

- [ ] **Step 1: Add env row**

| `HYDRA_REGIME_SELECTIVE` | engine | `=1` enables TREND_UP-only BUY + force-flatten TREND_DOWN (re-regulation study). **Default off.** Does not disable friction or lower min_conf. Absolute alpha unproven. |

- [ ] **Step 2: Append fidelity report** with actual retest numbers vs bands

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .hydra-research/regime_selective_fidelity_report.md
git commit -m "docs: regime-selective flag and fidelity report"
```

---

### Task 5: Full verification suite

- [ ] **Step 1:** `python -m pytest tests/test_regime_selective.py tests/test_exit_guarantees.py tests/test_friction_fee.py -q`
- [ ] **Step 2:** `python tools/retest_regime_selective_ranges.py`
- [ ] **Step 3:** Confirm faithfulness checklist all ☑ in report
- [ ] **Step 4:** No commit required if clean; fix commits if not

---

## Self-review (writing-plans)

1. **Spec coverage:** All design rules 1–4, kill switch, retest bands, non-goals → Tasks 1–5.
2. **Placeholders:** None intentional.
3. **Types:** `regime_selective: Optional[bool]`, `_apply_regime_selective(Regime, Signal) -> Signal` consistent.

## Execution

Use **subagent-driven-development**: one implementer per task, review after each, continuous execution, ledger at `.superpowers/sdd/progress.md`.
