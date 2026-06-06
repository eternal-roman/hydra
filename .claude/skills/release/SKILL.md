---
name: release
description: Run a Hydra release end-to-end. Use when the user says /release, asks to cut a release, bump version, ship a release, or tag a new version. Walks audit -> tests -> version bump -> PR -> CI green -> merge -> signed tag verification.
---

# Release

You are running a release. Steps:

1. Run full test suite + typecheck + lint
2. Grep for current version string across repo, bump everywhere
3. Create PR with summary of changes
4. Wait for CI green (poll gh pr checks)
5. Merge PR
6. Create signed git tag with changelog
7. Verify tag with `git tag -v`
8. **Publish GitHub Release** (`gh release create`) and verify it becomes Latest
9. Run `scripts/check_release_alignment.py --check-tag --check-gh-release` — exit 0 required

Ask user for version bump type (patch/minor/major) before starting.

## Hydra-specific contract

### Step 1 expansion (full test suite)

The CI gate is `.github/workflows/ci.yml`. Mirror it locally:

- All individual `python tests/test_*.py` invocations listed under the `engine-tests` job in `.github/workflows/ci.yml` (the workflow is the authoritative list)
- `python tests/live_harness/harness.py --mode smoke`
- `python tests/live_harness/harness.py --mode mock`  ← **mandatory for execution-path changes**
- `python hydra_engine.py` (synthetic demo smoke)
- `cd dashboard && npm install && npm run build` (dashboard-build job)

There is no separate typecheck/lint gate today; if `ruff` is installed locally,
run `ruff check .` as a courtesy.

### Step 2 expansion (version sites — 7 lockstep)

**Rule 5 (Operating Rules) is binding here.** Before bumping, run:

```bash
git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'
python scripts/check_release_alignment.py
```

`check_release_alignment.py` is the authoritative enumerator — it prints
every canonical site with its current version. If it fails, fix
alignment before proceeding. If `git grep` surfaces a semver path that
the script does NOT cover, STOP and ask the user whether the new site
should be added to §Version Management in CLAUDE.md as a permanent
lockstep entry (and added to the script).
Past failure: v2.6.0 had to ship a follow-up correction commit because
this grep was skipped; v2.11.0→v2.14.2 shipped without `gh release
create`, leaving v2.10.11 as GitHub's "Latest" — detected by
`--check-gh-release`.

Canonical sites:

1. `CHANGELOG.md` — new `## [X.Y.Z]` section header
2. `dashboard/package.json` — `"version"` field
3. `dashboard/package-lock.json` — both `"version"` fields (root + `""` package)
4. `dashboard/src/App.jsx` — footer string `HYDRA vX.Y.Z`
5. `hydra_agent.py` — `_export_competition_results()` `"version"` field
6. `hydra_backtest.py` — `HYDRA_VERSION = "X.Y.Z"` (stamps every BacktestResult)
7. Git tag — `git tag -s vX.Y.Z` after merge

### Step 3 expansion (PR)

Use a HEREDOC for the PR body so newlines render correctly. Summary should
reference: changes per CHANGELOG, any safety-invariant impact (I1-I12),
and any version-stamped artifacts (BacktestResult, dashboard footer).

### Step 4 expansion (CI poll)

`gh pr checks <pr-number> --watch` until both `engine-tests` and
`dashboard-build` are green. Never merge with red or pending CI.

### Step 6 expansion (signed tag)

`git tag -s vX.Y.Z -m "vX.Y.Z"`. The changelog entry from Step 2 is the
tag message body. Push with `git push origin vX.Y.Z`.

### Step 7 expansion (verification)

**Rule 3 (Operating Rules) is binding here.** Run `git tag -v vX.Y.Z` and
paste the output. It must show `Good signature`. If GPG is not configured,
fall back to annotated tag (`git tag -a vX.Y.Z`) and document the gap.
Do not declare the release "verified" without showing the command output.

### Step 8 expansion (publish GitHub Release)

A pushed signed tag alone does NOT produce a GitHub Release — the
`/tag/vX.Y.Z` URL renders because the tag exists, but the Releases page
still shows the last *published* release as "Latest". Publish explicitly:

```bash
gh release create vX.Y.Z --verify-tag --notes-from-tag --title "vX.Y.Z — <summary>"
```

Then verify with `gh release view vX.Y.Z` (must not error) and
`gh release list --limit 1` (must show `vX.Y.Z` with the `Latest` badge).

Past failure: v2.11.0 → v2.14.2 all shipped without this step, so
GitHub's Latest stayed pinned at v2.10.11 for weeks.

### Step 9 expansion (alignment gate)

Final gate — must exit 0:

```bash
python scripts/check_release_alignment.py --check-tag --check-gh-release
```

This re-enumerates all 7 code sites + the tag at HEAD + the latest
published GitHub Release, and fails loudly on any drift. Run it as the
last action of the release; if it fails, fix before declaring done.

## Pre-flight ask

Before starting, confirm with the user:
- Bump type: **patch** / **minor** / **major** (per CLAUDE.md guidance:
  patch for fixes/docs; minor for material upgrades; major reserved)
- Whether the live agent is running — if so, stop it per Operating Rule 2
  before any `hydra_session_snapshot.json` or `hydra_order_journal.json` edits

## Operating Rules invoked

This skill invokes §Operating Rules in CLAUDE.md:
- Rule 2 (stop processes before editing state) — pre-flight check
- Rule 3 (verify claims with actual commands) — Steps 4 and 7
- Rule 5 (enumerate version sites upfront) — Step 2
