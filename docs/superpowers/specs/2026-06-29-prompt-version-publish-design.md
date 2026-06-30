# Prompt editor + version publish — design

**Date:** 2026-06-29
**Branch:** `feat/frontend-superadmin-access` (stacks on the super-admin flow, PR #28)
**Status:** Design — pending approval

## Problem / goal

The `prompt` / `prompt_version` authoring catalog exists (seeded in PR #23, with the
one-published-per-prompt index) but has **no HTTP API and no UI**. A SUPER_ADMIN
needs to **edit a prompt, save it as a draft version, and publish it** — with a
visible version history. This delivers that v1 loop.

## Scope (v1)

- Edit the current published prompt → **Save as a new draft version** → **Publish**
  (draft → published, auto-demote the previously published one).
- Version **history** list (version #, status, created_at).
- The catalog is **global and SUPER_ADMIN-curated** (no `tenant_id`), so this is a
  **platform surface** — no tenant elevation required. Lives on the existing
  super-admin **"Agent Prompt"** nav item (`/agent-prompt`).

**Out of scope (later):** rollback/diff between versions, creating new prompt
*families*, deleting drafts, mutable (edit-in-place) drafts, field-level/structural
editing of the prompt body (v1 edits it as text).

## Access model

- Endpoints are **platform-scoped**, gated by reusing **`platform:elevations:read`**
  via `platform_require(...)` (same pattern as `GET /platform/tenants`) — no new
  permission, **no re-seed**. SUPER_ADMIN holds it; tenant users are rejected (they
  must never edit the global catalog).
- DB access: `platform_scoped_session`. `prompt` / `prompt_version` / `form_schema` /
  `schema_version` carry **no `tenant_id` and have no RLS**, so the request-path
  session reads and writes them directly (verified against migration 0001:
  RLS is only on tenant-scoped, catalog (`role`/`role_permission`), `tenant_elevation`,
  and WORM tables).

## Backend — new `apps/control_plane/src/control_plane/api/v1/prompts.py`

All under the platform gate above. Response envelope `ResponseModel[T]` via `ok(...)`;
errors via `CustomAPIException` subclasses (per control_plane CLAUDE.md).

| Method + path | Purpose | Response |
|---|---|---|
| `GET /prompts` | List prompt families | `[{id, name, insurance_type, published_version}]` (`published_version` int or null) |
| `GET /prompts/{prompt_id}/versions` | Version history | `[{id, version, status, created_at}]` newest-first |
| `GET /prompts/{prompt_id}/versions/{version_id}` | One version's content | `{id, version, status, created_at, composite_json}` |
| `POST /prompts/{prompt_id}/versions` | Create a **draft** | body `{composite_json: object}` → returns the created version |
| `POST /prompts/{prompt_id}/versions/{version_id}/publish` | **Publish** a version | `{...version...}` (now `published`) |

**Create-draft semantics** (mirrors `_seed_prompts`):
- `version = max(version for prompt) + 1`, `status = "draft"`.
- `schema_version_id` bound to the prompt's schema's **current published**
  `schema_version` (via `prompt.schema_id` → `form_schema` → published `schema_version`).
  If none published → **409** ("no published schema to bind the prompt to").
- `composite_json` stored as sent. The editor preserves `name`/`format`/`source`
  and edits the `prompt` text; the client sends back the full doc.

**Publish semantics:**
- Within one transaction: demote the current published version of that prompt to
  `draft` and `flush()` (frees `uq_prompt_version_published_per_prompt`), then set the
  target version `published`. The DB index guarantees one-published-per-prompt.
- 404 if prompt/version unknown or version not in this prompt. Publishing an
  already-published version is a no-op success.

**Errors:** 401 (no session), 403 (not a platform operator / lacks the gate),
404 (unknown prompt/version), 409 (no published schema for create-draft),
422 (malformed body).

## Frontend — the `/agent-prompt` page (super-admin nav, already wired)

- `src/lib/api/prompts.ts` — typed wrappers for the 5 endpoints.
- Page:
  - **Prompt picker** (the one IBV prompt for now; a select if several).
  - **Editor**: read-only header (name + insurance_type) + a large `Textarea` bound
    to `composite_json.prompt`. Other fields pass through unchanged.
  - **Save as draft** → `POST .../versions` → refresh history, show the new draft.
  - **Version history** list: version #, status badge (published/draft), date;
    a **Publish** button on drafts; selecting a row loads its content into the editor.
  - Loading/empty/error states via `Alert`; spinners on save/publish.
- No new route/nav needed — replaces the `/agent-prompt` `Placeholder` stub. The item
  is `superAdminOnly` (always shown to a super admin; needs no elevation, since the
  catalog is global).

## Testing

- **Backend** integration tests (mirror `test_platform_elevation.py` harness):
  create draft → publish → exactly one published; publishing demotes the prior one;
  a tenant user gets 403; create-draft with no published schema → 409.
- **Frontend**: `prompts.ts` is exercised via a small logic test; extract any
  non-trivial view logic into a pure helper if it aids testing (the codebase has no
  RTL/jsdom, so prefer logic-only tests).

## Verification

- Backend: `ruff` + `mypy` clean; the new integration tests pass.
- Frontend: `tsc` + `eslint` + tests + `vite build` clean.
- Per repo CLAUDE.md: run **"simplify code"** on the change before committing.
- Manual: as the seeded super admin, open `/agent-prompt`, edit the IBV prompt,
  save a draft, publish it; confirm history shows the new published version and the
  prior one demoted to draft.
