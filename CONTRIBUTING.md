# Contributing

Thanks for interest in HYDRA. This project is small and safety-sensitive (live order paths).

## Before you change code

1. Read [`CLAUDE.md`](CLAUDE.md) invariants (spot-only, limit post-only, 2s REST, 15% BUY-halt breaker).
2. Offline smoke (no keys / no WSL): `python hydra_agent.py --demo --duration 20`.
3. Prefer paper for exchange-shaped experiments: `python hydra_agent.py --paper` (needs kraken-cli).
4. Never commit `.env`, keys, journals, or snapshots.
5. Do not claim strategy “alpha” in docs without a causal historical retest and OOS note. Default engine path has **not** proven positive absolute return on the published SOL windows.

## Development

```bash
pip install -r requirements.txt
python hydra_agent.py --demo --duration 15          # offline agent loop
python hydra_engine.py && python hydra_backtest.py   # pure synthetic demos
python -m pytest tests/ -q
python tests/live_harness/harness.py --mode mock    # if you touch execution
python -m pytest tests/test_hold_through.py -q   # hold-through rails
cd dashboard && npm install && npm run build
```

## Pull requests

- Branch off `main`; one logical change per PR.
- Keep CI green (`engine-tests` + `dashboard-build`).
- Execution-path changes **must** pass `harness.py --mode mock`.
- Version bumps: update every site listed in `CLAUDE.md` (or run `python scripts/check_release_alignment.py`).
- Kill switches that default **off** stay off unless the PR explicitly changes the default and documents why.
- Do not open public issues for security bugs — use [Security Advisories](https://github.com/eternal-roman/hydra/security/advisories/new).

## Style

- Engine / flywheel: **stdlib only** (no numpy/pandas).
- Prefer SKIP over BLOCK for soft gates; reserve BLOCK for hard safety stops (e.g. 15% breaker).
- Docs must match code: default-off flags, BUY-only CB halt, no absolute-profit claims without evidence.
