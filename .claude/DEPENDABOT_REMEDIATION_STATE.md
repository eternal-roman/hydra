# Dependabot remediation — running state

Working file for a multi-session operation. Delete once all PRs are merged.
Updated as each step completes so a fresh session can resume from here.

**Session started:** 2026-08-05
**Repo:** eternal-roman/hydra · **Target base:** `main`
**Audit branch (separate work, already pushed):** `claude/kraken-cli-audit-updates-n2p6d5`

## Resume instructions

1. Read this file top to bottom.
2. `gh`/MCP: re-list open PRs by `dependabot[bot]` to confirm what is still open.
3. Continue at the first PR whose status is not `MERGED`.
4. Re-run the verification commands in "Verification recipe" before any merge.

## Verification recipe

```bash
cd /home/user/hydra
pip install -r requirements.txt          # after any pip bump
python -m pytest tests/ -q               # baseline: 1404 passed, 29 env-failed
python tests/live_harness/harness.py --mode mock    # must be 36/36, exit 0
cd dashboard && npm ci && npm run build  # after any npm bump
```

Baseline note: 29 pytest failures are PRE-EXISTING and environmental — a
self-defined `SkipTest` in `tests/test_engine.py` that pytest cannot recognize,
raised when `anthropic`/`openai` are absent. With those SDKs installed the
suite is fully green. Do NOT treat 29 failures as a regression; compare against
a stashed baseline if unsure.

## PR inventory (10 open, all dependabot[bot], all base=main)

| PR | Bump | Ecosystem | Risk | Status |
|---|---|---|---|---|
| 177 | bcrypt `<4.0` → `<5.0` | pip | **HIGH** — requirements.txt pin comment says passlib 1.7.4 reads `bcrypt.__about__`, removed in bcrypt 4.x | PENDING |
| 179 | anthropic `>=0.116.0` → `>=0.120.2` | pip | MED — brain client surface | PENDING |
| 181 | openai `>=2.46.0` → `>=2.49.0` | pip | MED — Grok strategist path | PENDING |
| 176 | websockets `>=16.1` → `>=16.1.1` | pip | LOW | PENDING |
| 171 | react 19.2.7 → 19.2.8 | npm | LOW | PENDING |
| 174 | react-dom 19.2.7 → 19.2.8 | npm | LOW — must land with 171 | PENDING |
| 173 | @vitejs/plugin-react 6.0.3 → 6.0.4 | npm | LOW | PENDING |
| 180 | eslint 10.7.0 → 10.8.0 | npm dev | MED — new rules can fail lint | PENDING |
| 182 | globals 17.7.0 → 17.8.0 | npm dev | LOW | PENDING |
| 172 | actions/setup-python 6 → 7 | gh actions | MED — CI-wide | PENDING |

## Known hazards

- **Sequential merge invalidates siblings.** Every pip PR edits `requirements.txt`;
  every npm PR edits `dashboard/package.json` + `package-lock.json`. After each
  merge the remaining same-ecosystem PRs go stale/conflicted. Expect to trigger
  a dependabot rebase (`@dependabot rebase`) or resolve manually.
- **react + react-dom must move together** (171 + 174) — mismatched versions are
  a runtime hazard.
- **My audit branch also edits `requirements.txt`** (adds PyYAML). It will need a
  merge from main after these land.

## Progress log

- [x] Re-requested kraken-cli repo access — granted (read via git proxy), cloned to
      `/workspace/krakenfx/kraken-cli` at `aa32814`, v0.3.2.
- [x] **RESOLVED open question from prior session:** `--yes` is `global = true`
      in `src/lib.rs:96-97` (alias `force`), so it IS valid on `order buy`/`sell`/
      `cancel`. Hydra's usage is correct — no change needed. The tool-catalog.json
      simply does not enumerate global flags per command.
- [x] Confirmed from source (`src/commands/paper.rs:52-63`) that `paper buy` takes
      `pair` and `volume` POSITIONALLY, `--type` defaults to `market`, `--price`
      required for limit. The fix already shipped on the audit branch is correct.
- [x] **Python side verified empirically — ALL GREEN.** Installed the exact target
      versions of every pip PR simultaneously and ran the full suite:
      - anthropic **0.120.2** (PR 179 target)
      - openai **2.53.0** (satisfies PR 181 `>=2.49.0`)
      - bcrypt **4.3.0** (what PR 177's `<5.0` resolves to)
      - websockets **17.0.1** (satisfies PR 176 `>=16.1.1`)
      - passlib 1.7.4
      Result: **`1435 passed, 2 skipped, 0 failed`**. Mock harness **36/36, exit 0**.
      `tests/test_auth.py` 10/10 (real passlib hash/verify exercised).
      This also PROVES the 29 failures seen earlier were purely SDK-absence.
- [x] **bcrypt investigated in isolated venvs (task #7 resolved — NOT a blocker).**
      - `bcrypt<5.0` → resolves 4.3.0. passlib TRAPS the `__about__` AttributeError
        ("(trapped) error reading bcrypt version") and hash/verify both work.
      - `bcrypt>=5.0` → **FATAL**: `ValueError: password cannot be longer than
        72 bytes` raised inside passlib's `detect_wrap_bug` probe at backend
        init, i.e. on the very first CryptContext use. Auth would be dead.
      ⇒ PR 177 is SAFE. The `<5.0` ceiling is LOAD-BEARING. The requirements.txt
        comment must be rewritten when 177 lands — it currently blames
        `__about__`, which is only a trapped warning, and would mislead a
        reviewer into waving through a future `<6.0` bump.
- [x] **npm side verified.** Installed react 19.2.8, react-dom 19.2.8,
      eslint 10.8.0, globals 17.8.0, @vitejs/plugin-react 6.0.4 together:
      `npm run build` exit 0; `npm run lint` exit 0 after the fix below.
      All five are `^` caret ranges in package.json, so these bumps move the
      LOCKFILE primarily — react/react-dom cannot land mismatched.
- [x] **Fixed 2 pre-existing lint errors** (`App.jsx` optional catch binding,
      `catch (_)` → `catch`). Present at eslint 10.7.0 too, so not caused by
      PR 180; lint is not in CI, but it is now green either way.
- [ ] Await parallel per-dependency assessment workflow (changelog/breaking-change review)
- [ ] Merge in sequence

## Local env setup needed to reproduce (fresh container)

```bash
pip install pytest cffi PyYAML "passlib[bcrypt]>=1.7.4" PyJWT websockets \
            "anthropic>=0.120.2,<1.0" "openai>=2.49.0,<3.0" "bcrypt>=4.0,<5.0"
```
Without `cffi` the `cryptography` import panics (pyo3) and 15 test files fail to
collect. Without `anthropic`/`openai`, 29 brain tests "fail" via an unrecognized
self-defined `SkipTest`.
