# Contributing

Thanks for interest in HYDRA. This project is small and safety-sensitive (live order paths).

## Before you change code

1. Read [`CLAUDE.md`](CLAUDE.md) invariants (spot-only, limit post-only, 2s REST, 15% breaker).
2. Prefer paper mode for experiments: `python hydra_agent.py --paper`.
3. Never commit `.env`, keys, journals, or snapshots.

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
python tests/live_harness/harness.py --mode mock   # if you touch execution
cd dashboard && npm install && npm run build
```

## Pull requests

- Branch off `main`; one logical change per PR.
- Keep CI green (`engine-tests` + `dashboard-build`).
- Execution-path changes **must** pass `harness.py --mode mock`.
- Version bumps: update every site listed in `CLAUDE.md` (or run `python scripts/check_release_alignment.py`).
- Do not open public issues for security bugs — use [Security Advisories](https://github.com/eternal-roman/hydra/security/advisories/new).

## Style

- Engine / flywheel: **stdlib only** (no numpy/pandas).
- Prefer SKIP over BLOCK for soft gates; reserve BLOCK for hard safety stops.
