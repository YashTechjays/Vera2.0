#!/usr/bin/env bash
# Stop hook (non-blocking): if there are uncommitted backend/frontend CODE changes,
# remind to run the code-simplifier (per the repo-root CLAUDE.md rule) and re-run the
# checks before committing. Prints only a systemMessage; never blocks the stop, so it
# cannot cause a stop-loop. Silent when there are no pending code changes.
set -euo pipefail

if git status --porcelain -- vera-backend vera-frontend 2>/dev/null | grep -qE '\.(py|ts|tsx)$'; then
  printf '%s\n' '{"systemMessage":"Per CLAUDE.md: uncommitted code changes detected - run \"simplify code\" (code-simplifier) and re-run checks (backend: just check; frontend: tsc/eslint/tests/build) before claiming done or committing."}'
fi
exit 0
