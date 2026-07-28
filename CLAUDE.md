# Vera 2.0 — repo-wide workflow rules

This repository holds the backend (`vera-backend/`) and frontend (`vera-frontend/`),
each with its own `CLAUDE.md` of domain/security rules that load when you touch that
code. The rules below are **repo-wide** and apply to every change in Vera 2.0.

## MANDATORY: code comments — only when truly needed

Default to no comments: well-named, readable code is the documentation. Add a
comment only when it explains something the code cannot — a non-obvious constraint,
a real race or lock order, a compliance rule, or a deliberate trade-off — and keep
it to one line. Never narrate what the code already says, and keep docstrings to a
single sentence.

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
