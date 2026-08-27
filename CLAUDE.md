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

## MANDATORY: a new vocabulary word goes in the scoped `CLAUDE.md`, same commit

Adding, renaming, re-scoping or retiring a **vocabulary item** — a form-schema DSL key, an
inheritance or defaulting rule, a validator rule, an enum member a schema author hand-writes —
is not done until the scoped `CLAUDE.md` that authors actually read says so, in the SAME commit
as the code. Not the next PR, not "when it settles".

For the form-schema DSL that file is
`vera-backend/packages/vera_core/src/vera_core/forms/CLAUDE.md`, and a new key normally lands in
three of its sections:

- **the new-schema checklist** — if an author must now DO something, and above all if it is
  something **no validator enforces**;
- **validator rules** — what gets rejected, plus what deliberately does NOT and why;
- **semantics worth remembering** — what the word means, how it defaults and inherits, and what
  breaks downstream when it is wrong.

Then re-read the sections you did NOT edit and fix whatever the change just falsified. A new key
usually makes some neighbouring claim untrue.

**A dated doc under `docs/superpowers/` does not satisfy this.** Specs, plans and review records
are point-in-time evidence, are not amended in place, and load for nobody by default. The scoped
`CLAUDE.md` is the living contract. Writing an excellent design doc and stopping there is exactly
how the gap forms.

Precedent, and the reason this rule exists: `collected_per` shipped with a design doc, an
implementation plan, six review artifacts and a live-call verification record — and **zero lines
in any `CLAUDE.md`**. The authoring checklist never mentioned the one requirement no validator
enforces (the reference-number leaf must carry the marker), and two neighbouring bullets had
quietly become false, including one still describing a gate that had been deleted. Nothing in
`just check` can catch this; it surfaced only because someone asked.

## Git remote: Bitbucket, not GitHub

`origin` is a Bitbucket remote (`git remote -v`), so `gh` cannot create PRs here — push the branch
and open the PR from the URL git prints on push. Branches track `origin/dev`, so **always push with
an explicit refspec** (`git push origin HEAD:refs/heads/<branch>`) — a bare `git push` targets dev.
