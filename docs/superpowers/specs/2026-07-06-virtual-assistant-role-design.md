# `virtual_assistant` tenant role — design

**Date:** 2026-07-06
**Branch:** `feat/voice-lab-role`
**Status:** Design — pending approval

## Problem / goal

We need a new tenant-level role, `virtual_assistant`, that can do exactly one
thing: use the Voice Lab sandbox. A tenant admin must be able to pick this role
(alongside `TENANT_ADMIN`/`SUPERVISOR`/any custom role) when inviting a new user.

Two structural gaps make this non-trivial:
- Voice Lab today is gated by `calls:read` — the same permission that gates the
  real call system — so granting Voice Lab access currently also grants
  visibility into real call data. A role scoped to "only Voice Lab" needs its
  own permission.
- The invite dialog's `roleIds` is hardcoded to `[]` — there is no role picker
  in the frontend at all today, and no frontend consumer of `GET /roles`. This
  has to be built for any role to be choosable at invite time, not just this one.

## Scope (v1)

- New permission `voice_lab:sandbox`, new global system role `VIRTUAL_ASSISTANT`
  holding only that permission.
- Voice Lab's 4 endpoints re-gated onto `voice_lab:sandbox` instead of `calls:read`.
- `TENANT_ADMIN`/`SUPERVISOR` (and any tenant-custom role currently holding
  `calls:read`) backfilled with `voice_lab:sandbox` so nobody loses existing
  Voice Lab access when the gate switches.
- A role picker added to `InviteUserDialog` so a tenant admin can actually
  choose a role at invite time (today they can't choose any role).
- Frontend nav/routing locked down so a `virtual_assistant` user's sidebar only
  shows Voice Lab + Settings, and they land on Voice Lab by default instead of
  the (mock) dashboard.

**Out of scope (later, explicitly deferred):**
- A `PATCH /roles/{id}/permissions`-style endpoint to edit an existing role's
  permission set at runtime (tenant or platform level). Today no such endpoint
  exists for *any* role — permissions are set once at `POST /roles` creation
  time, and DB-level RLS blocks a tenant session from writing to a global
  (`tenant_id IS NULL`) role/role_permission row regardless. Expanding
  `virtual_assistant` (or any system role)'s permissions today requires editing
  `rbac_defaults.py` + a migration/seed run — a deploy, not an API call. This is
  a known, pre-existing gap, not something this task introduces.
- Editing/removing a user's role after invite (the existing `assign_role`/
  `revoke_role` endpoints already cover that, untouched here).
- Wiring `/`, `/call-history`, `/analytics` to real data — they're mock/
  placeholder pages today; this task only adds the permission gate they'll need
  once they're implemented.

## Forward-compatibility note (why the frontend design needs no rework later)

The nav/redirect design below is **100% permission-driven, never role-driven** —
`visibleNavFor()` filters on the user's live effective-permissions array, and
the default-landing redirect is computed from that same filtered list. Neither
hardcodes `virtual_assistant` or branches on role name anywhere. So when the
deferred roles/permissions-editing API (tenant- and platform-level) eventually
ships and someone grants `calls:read` to `virtual_assistant` (or invents a new
role entirely), every affected user's sidebar and default route update
automatically the moment their permission cache refreshes — zero frontend
changes. The only thing that stays static in `nav.ts` is the fixed mapping of
*which existing page requires which existing permission*, which is orthogonal
to how roles get edited and only changes when a new page/feature ships.

## Backend

### 1. Permission & role catalog (`vera_core/models/rbac_defaults.py`, `scripts/seed.py`)

- Add permission `voice_lab:sandbox` to `DEFAULT_PERMISSIONS`.
- Add `voice_lab:sandbox` to `TENANT_ADMIN`'s and `SUPERVISOR`'s permission sets
  in `SYSTEM_ROLES` (additive — preserves their existing Voice Lab access).
- Add a new `SYSTEM_ROLES` entry: `VIRTUAL_ASSISTANT` → `{"voice_lab:sandbox"}` only.
- `_seed_permissions`/`_seed_system_roles` in `scripts/seed.py` are already
  idempotent upserts, so re-running seed on an existing environment picks up
  the new permission/role/grants with no other code change.

### 2. Data migration (new Alembic revision)

Seed re-runs only touch *global* system roles — they don't reach tenant-custom
roles created via `POST /roles`. To honor "existing roles keep Voice Lab
access," the migration must also backfill those:

- Insert the `voice_lab:sandbox` permission row (if the seed hasn't already run).
- `INSERT INTO role_permission (role_id, permission_id, tenant_id) SELECT
  role_id, <voice_lab:sandbox id>, tenant_id FROM role_permission WHERE
  permission_id = <calls:read id> ON CONFLICT DO NOTHING` — copies the grant to
  every role, system or tenant-custom, currently holding `calls:read`.

### 3. Endpoint gating (`api/v1/voice_lab.py`)

Swap `require("calls:read")` for `require("voice_lab:sandbox")` on:
- `GET /voice-lab/insurance-providers`
- `POST /voice-lab/sessions`
- `DELETE /voice-lab/sessions/{room_name}`

And update the SSE transcript endpoint's inline check
(`GET /voice-lab/sessions/{room_name}/transcript`) from
`"calls:read" in permissions` to `"voice_lab:sandbox" in permissions`.

## Frontend

### 4. Invite dialog role picker (`InviteUserDialog.tsx`)

- New API client (none exists yet) wrapping `GET /roles`.
- A select/dropdown populated from it, replacing the hardcoded `roleIds: []` in
  the `inviteUser(...)` call with the chosen role's id.
- `GET /roles` already returns only what RLS permits (global system roles +
  the caller's own tenant-custom roles) and the backend already rejects
  granting a platform-tier role via invite — no new filtering logic needed
  frontend-side.

### 5. Nav lockdown (`nav.ts`)

| Nav item | Current gate | New gate |
|---|---|---|
| Voice Lab | `calls:read` | `voice_lab:sandbox` |
| Live Monitoring (`/`) | none | `calls:read` |
| Call History | none | `calls:read` |
| Analytics | none | `calls:read` |
| Data Management, Users, Settings | unchanged | unchanged |

`TENANT_ADMIN`/`SUPERVISOR` already hold `calls:read`, so their nav is
unaffected. A `virtual_assistant` user's sidebar collapses to Voice Lab +
Settings.

### 6. Routing (`App.tsx`)

Add a "redirect to the user's first visible nav item" rule (derived from
`visibleNavFor()`), used both as the post-login default route and as the
fallback when a permission-gated route is hit directly without the required
permission. For `virtual_assistant` this naturally resolves to `/voice-lab`
without any role-specific code.

## Testing

- **Backend**: add a `virtual_assistant` persona to `RBACWorld`/`rbac_world`
  (mirrors the existing `admin`/`norole` pattern in
  `tests/integration/control_plane/conftest.py`); assert it can hit all 4 Voice
  Lab endpoints and gets 403 on `/users`, `/roles`, `/calls`-family, etc. Add a
  migration-backfill test asserting a pre-existing custom role holding
  `calls:read` also holds `voice_lab:sandbox` after migration.
- **Frontend**: extend `nav.test.ts` for the new gates and `virtual_assistant`'s
  resulting visible-nav list; add a logic test for the new redirect rule; add a
  test for the invite dialog's role picker (currently untested — didn't exist).

## Verification

- Backend: `just check` (ruff + mypy + pytest) clean, including new RBAC tests.
- Frontend: `tsc` + `eslint` + tests + `vite build` clean.
- Per repo CLAUDE.md: run **"simplify code"** on the change before committing.
- Manual: invite a user as `virtual_assistant`, accept the invite, confirm the
  sidebar shows only Voice Lab + Settings, default landing is `/voice-lab`, and
  a Voice Lab session can be started/ended/transcribed; confirm an existing
  `TENANT_ADMIN`/`SUPERVISOR` user's Voice Lab access is unaffected.
