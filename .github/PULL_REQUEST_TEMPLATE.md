## Summary

<!-- What changed and why (2–5 sentences). -->

## Checklist

- [ ] Tests: `python -m pytest tests/ -q` (or targeted suite) pass
- [ ] If execution path touched: `python tests/live_harness/harness.py --mode mock`
- [ ] If dashboard touched: `cd dashboard && npm run build`
- [ ] No secrets / `.env` / journals in the diff
- [ ] CLAUDE.md invariants respected (spot-only, limit post-only, 2s REST, 15% breaker)
- [ ] Version sites updated if this is a release PR (`scripts/check_release_alignment.py`)

## Risk notes

<!-- Money-path impact? Kill switches? Rollback plan? -->
