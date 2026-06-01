#!/usr/bin/env bash
# Path-scoped post-edit hook for Hydra. Runs the narrowest possible
# verification step based on which file was edited, so the agent gets fast
# feedback without paying the full pytest cost on every edit.
#
# Disable for rapid iteration: export HYDRA_POSTEDIT_HOOK_DISABLED=1
#
# Runtime requirement: bash (Git Bash on Windows is fine), python on PATH.

set -u
[[ "${HYDRA_POSTEDIT_HOOK_DISABLED:-0}" == "1" ]] && exit 0

# Claude Code passes hook input as JSON on stdin; the touched path lives at
# .tool_input.file_path (Edit) or .tool_input.path (Write). Fall back to env.
INPUT=$(cat 2>/dev/null || true)
FILE=$(printf '%s' "$INPUT" | python -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {}) or {}
    print(ti.get('file_path') or ti.get('path') or '')
except Exception:
    print('')
" 2>/dev/null)
FILE="${FILE:-${CLAUDE_FILE_PATH:-}}"

[[ -z "$FILE" ]] && exit 0

# Patterns use leading wildcards so they match absolute, workspace-relative,
# or basename-only paths.
case "$FILE" in
  *hydra_engine.py|*hydra_tuner.py)
    echo "[post-edit] running tests/test_engine.py + tests/test_tuner.py"
    python tests/test_engine.py 2>&1 | tail -20
    python tests/test_tuner.py 2>&1 | tail -20
    ;;
  *hydra_agent.py)
    # hydra_agent.py contains BaseStream and all Stream subclasses as
    # nested classes — there are no separate Stream.py files today.
    echo "[post-edit] running execution-path harness (smoke)"
    python tests/live_harness/harness.py --mode smoke 2>&1 | tail -20
    ;;
  *hydra_companions/*.py)
    echo "[post-edit] running companion test subset"
    python -m pytest tests/test_companion_*.py -x --tb=short 2>&1 | tail -30
    ;;
  *hydra_backtest*.py|*hydra_experiments.py)
    echo "[post-edit] running backtest test subset"
    python -m pytest tests/test_backtest_*.py tests/test_experiments.py -x --tb=short 2>&1 | tail -30
    ;;
  *.py)
    # Default for any other Python file: syntax check via py_compile.
    # py_compile validates the file without import side-effects and works
    # on any path (no module-resolution needed for files in subpackages).
    echo "[post-edit] py_compile: $FILE"
    python -m py_compile "$FILE" 2>&1 | tail -10 || true
    ;;
  *dashboard/src/*.jsx|*dashboard/src/*.js|*dashboard/src/*.css)
    # ESLint is sub-second per file; full `npm run build` is too heavy for
    # a per-edit hook (10-30s). Hydra has eslint.config.js — use it.
    echo "[post-edit] eslint: $FILE"
    (cd dashboard && npx --no-install eslint --no-warn-ignored "$FILE" 2>&1 | tail -20) || true
    ;;
  *)
    # Non-code file (md, json, txt, yml) — no-op
    ;;
esac

exit 0
