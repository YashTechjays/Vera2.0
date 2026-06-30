# Vera 2.0 — repo-wide workflow rules

This repository holds the backend (`vera-backend/`) and frontend (`vera-frontend/`),
each with its own `CLAUDE.md` of domain/security rules that load when you touch that
code. The rule below is **repo-wide** and applies to every change in Vera 2.0.

## MANDATORY: simplify code after every implementation

After completing any implementation — a feature, a bug fix, or any non-trivial edit —
and **BEFORE** claiming the work done or committing, run the **code-simplifier** plugin
on the change. Trigger it the same way every time: **"simplify code"** (this launches the
`code-simplifier` agent from `code-simplifier@claude-plugins-official`).

- It reconciles the recently modified code to clear, consistent, **maintainable** code
  that follows project standards, **without changing behavior**.
- It MUST run in the **same session** as the implementation (so it sees the change).
- By default it targets recently modified code; pass specific files if needed.
- After it applies refinements, **re-run the checks** before committing:
  - backend → `just check` (ruff + mypy + pytest);
  - frontend → `tsc` + `eslint` + tests + build.
- Skip only for truly trivial edits (a typo or a one-line rename).

This is not optional for Vera 2.0 — treat it as part of "done."

> Note: this is a model-followed workflow rule (a hook can't launch an agent), so it
> lives here in `CLAUDE.md` rather than as a `settings.json` hook.
