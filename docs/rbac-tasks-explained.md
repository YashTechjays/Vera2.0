# RBAC Tasks — Detailed Explanation (Vera 2.0)

This document explains the three RBAC tickets in detail:

1. **Permission Management** — tenant-level settings to manage permissions
2. **Role Management** — tenant-level settings to manage roles and attach permissions to them
3. **Role Assignment** — give roles to users (tenant admin for their own users, platform admin across tenants)

**All the UI for these three tickets goes in the Settings page**, under a new
"Roles & Permissions" section. Only users who have the `roles:manage` permission
should see it (use the `usePermission("roles:manage")` hook).

Everything below is based on the actual code in this repo. The file paths are real —
please read those files before starting.

---

## 0. Important: most of RBAC already exists in the code

This is **not** a build-from-scratch feature. The database tables, the permission
checking, and part of the API are already built and already used by the app. These
tickets are mainly about **adding the missing API endpoints and building the Settings
page UI on top of what exists**.

### The database tables

File: `vera-backend/packages/vera_core/src/vera_core/models/rbac.py`

| Table | What it stores | Who can see/change it |
|---|---|---|
| `permission` | The master list of permission codes, like `calls:read`, `users:manage`, `roles:manage` | Global — one shared list for the whole platform. It has **no** `tenant_id` column, so a tenant can never "own" a permission. |
| `role` | Role definitions. If `tenant_id` is NULL, it is a **system role** shared by everyone (SUPER_ADMIN, TENANT_ADMIN, SUPERVISOR). If `tenant_id` is set, it is a **custom role** created by that tenant. | Tenants can *read* the system roles plus their own custom roles, but can only *write* (create/edit) their own. The database itself enforces this through RLS (row-level security). |
| `role_permission` | Which permissions each role carries (a link table) | Same rules as `role`. |
| `user_role` | Which roles each user has. Also records who granted it (`granted_by`) and when (`granted_at`). | Strictly limited to the user's own tenant. |

The default data (the full list of permission codes and the three system roles) is in
`models/rbac_defaults.py`. There are 13 tenant-level permissions (like `calls:read`,
`forms:write`, `users:manage`, `roles:manage`) and 5 platform-level permissions that
all start with `platform:` (these are only for platform operators, never for tenants).

### How permission checking works today

File: `apps/control_plane/src/control_plane/auth/rbac.py`

- Endpoints are protected by adding `require("some:permission")` as a dependency.
  For example, all the role endpoints use `require("roles:manage")`.
- When a request comes in, this looks up the user's permissions from the database
  (user → their roles → those roles' permissions). The result is cached in Redis so
  it's fast.
- Every check is written to the audit log — both when access is **allowed** and when it
  is **denied**.
- Because of the cache, there is one rule you must never forget: **whenever you change
  anything that affects someone's permissions** (assign a role, remove a role, change a
  role's permissions, delete a role), you must call
  `resolver.invalidate(tenant_id, user_id)` for that user. Otherwise they keep their
  old permissions until the cache expires.
- There is also `platform_require("platform:...")` for platform-only routes (no tenant).

### The API endpoints that already exist

File: `apps/control_plane/src/control_plane/api/v1/roles.py` — all protected by
`require("roles:manage")`:

- `GET /roles` — lists the system roles plus this tenant's custom roles.
- `POST /roles` — creates a custom role with a list of `permission_ids`. It already
  blocks any `platform:*` permission from being put into a tenant role.
- `POST /users/{user_id}/roles` — gives a role to a user.
- `DELETE /users/{user_id}/roles/{role_id}` — takes a role away from a user.

Also useful: the invite endpoint (`POST /users/invitations` in `users.py`) already
accepts a `role_ids` list, so a new user can be invited *with* roles from day one.

### The frontend plumbing that already exists

File: `vera-frontend/src/lib/auth/permissions.ts`

- After login, `/auth/me` returns the user's `roles` (names) and `permissions` (codes).
  The server computes these — the frontend never decides permissions on its own.
- `usePermission("code")` hook and the `RequirePermission` component are used to
  show/hide UI. Good examples to copy: `src/pages/Users.tsx` and
  `src/pages/Settings.tsx`.

**Summary: the work is (a) add the missing read/update/delete endpoints, (b) build the
Settings page UI, (c) make sure the platform-admin path works.**

---

## 1. Ticket: Permission Management

> "A tenant level settings to manage permission CRUD management"

### DECISION: read-only catalog — do NOT build create/update/delete

In this system, **permissions are defined in code, not by users**. Every permission
code is written directly into the route protection (for example,
`require("calls:read")` is a literal string in the code), and the list is seeded from
`rbac_defaults.py`. This means:

- If a tenant admin **creates** a new permission from a settings screen, nothing will
  ever check it — it does nothing (dead data).
- If they **delete** one, the route guards that check it break, and it gets ripped out
  of every role that carried it.
- The `permission` table has no `tenant_id` on purpose — tenants cannot own
  permissions.

So this ticket is scoped as: **the tenant sees a read-only permission catalog, and
"managing" permissions means wiring them into roles (Ticket 2).** When a genuinely new
permission is needed, that is a code change plus a seed migration — which is correct,
because a new permission is meaningless without new code that enforces it. We are also
**not** building a platform-admin "edit descriptions" endpoint — it's scope with no
user; add it later only if someone actually asks.

### What to build

- [ ] `GET /permissions` — a new endpoint (either a new `api/v1/permissions.py` file or
      inside `roles.py`). Protect it with `require("roles:manage")`. Return `id`,
      `code`, `description` for each permission. **Filter out every `platform:*` code**
      — use the `is_platform_permission()` helper in `api/v1/common.py`. A tenant admin
      should never even see platform permissions as options.
- [ ] Frontend: a read-only "Permissions" table inside the Settings page section —
      just code + description, nothing editable.
- [ ] Tests: a tenant caller never sees `platform:*` rows; a caller without
      `roles:manage` gets 403.

---

## 2. Ticket: Role Management

> "A tenant level settings to manage Role CRUD and assign permissions to role"

### What is missing (this is the actual work)

`GET /roles` and `POST /roles` already exist. What does not exist:

1. **`GET /roles/{role_id}`** — role detail **including the permissions it carries**.
   The current list endpoint only returns id/name/description/is_system, so the edit
   screen has no way to show which permissions a role has.
2. **`PATCH /roles/{role_id}`** — rename a role, change its description, and **replace
   its set of permissions**. Note: `POST /roles` currently has no description field at
   all (roles are created with an empty description) — add that field in the same pass.
3. **`DELETE /roles/{role_id}`** — delete a custom role.

### Guards and edge cases — each of these should become a test

- **System roles cannot be edited or deleted by a tenant.** If `role.tenant_id` is
  NULL, PATCH and DELETE must return a clean 403/404. The database RLS would block the
  write anyway, but do not rely on a silent "0 rows updated" — check ownership
  explicitly and return a proper error (follow the `_role_visible` pattern already in
  `roles.py`).
- **A `platform:*` permission must never get into a tenant role.** `create_role`
  already enforces this (`roles.py:114`) — PATCH must enforce exactly the same rule.
- **Changing a role's permission set changes live users' access — handle the cache.**
  When PATCH changes which permissions a role carries, collect the ids of every user
  holding the role and call `resolver.invalidate(tenant_id, user_id)` for **each** of
  them — otherwise those users keep their cached permissions until the cache expires.
- **DECISION — delete is blocked while users still hold the role: return 409 with the
  holder count.** No silent cascade. A silent cascade would strip access from N users
  with no warning — the wrong shape in a HIPAA product where every access change is
  audited. The UI shows "N users still have this role — remove it from them first",
  and the admin revokes per user through the existing (already audited,
  cache-invalidating) revoke endpoint. This also keeps DELETE simple: by the time it
  runs, the role has no holders, so there is no bulk cache invalidation to get right.
- **Duplicate role name** → return 409. There is a unique constraint on
  `(tenant_id, name)` and the existing `_conflict_or_raise` helper handles it. Note the
  constraint also means a tenant cannot reuse a system role's name.
- **Unknown permission id in the request** → return 400 (copy the `create_role`
  pattern).

### Audit — required, not optional

Every mutation must write an auth event using `emit_auth_event` (see how
`create_role` does it at `roles.py:130`). The `AuthEvent` enum
(`vera_core/models/enums.py`) already has `ROLE_CREATED`, but **`ROLE_UPDATED` and
`ROLE_DELETED` do not exist — add them**. Put the role id and what changed in the
`meta` field, using ids and names only (never anything that could be PHI).

### Frontend (Settings page)

Inside the "Roles & Permissions" section:

- A roles list. System roles get a badge and are read-only (no edit/delete buttons).
- A create/edit dialog: name, description, and permission checkboxes. Group the
  checkboxes by prefix (`calls:*`, `forms:*`, `users:*`, …) so the list is readable.
  The checkboxes are fed by the new `GET /permissions` endpoint.
- Delete with a confirmation dialog (if we go the 409 route, show the holder count).

---

## 3. Ticket: Role Assignment

> "Tenant admin assigns roles to their own users; platform admin can do it across all tenants"

### The tenant-admin part — mostly done on the backend

`POST /users/{user_id}/roles` and `DELETE /users/{user_id}/roles/{role_id}` already
exist, and the important safety checks are already in them (read `roles.py:152-242`):

- **Cross-tenant assignment is impossible.** If someone passes a user id or role id
  from another tenant, RLS makes it resolve to "no such row" and the request is
  rejected — it can never silently link across tenants.
- **A tenant can never assign a platform-privileged role** (like SUPER_ADMIN). The
  check is based on whether the role carries any `platform:*` permission — not on the
  role's name — so any future platform role is automatically covered too.
- Every assign/revoke invalidates the permission cache and writes an audit event.

What is missing:

1. **A way to read a user's roles.** Build `GET /users/{user_id}/roles`, or add a
   `roles` field to the `GET /users` list response. Today the user list only returns
   id/email/name/status/last_login_at, so the UI has nothing to display.
2. **The UI** — in the Settings page section: a user-role assignment view where the
   admin picks a user, sees their current roles, and adds or removes roles. (The
   invite dialog already sends `role_ids`, so invited users can start with roles.)

### The platform-admin part — understand the "elevation" model first

A platform operator (a user with `account_type = "platform"`, i.e. SUPER_ADMIN) does
**not** get a free-for-all cross-tenant API. Vera's security model (see
`auth/elevation.py` and `api/v1/platform.py`) is **break-glass elevation**:

- The operator opens an elevation into **one specific tenant**, with a reason, and it
  is time-boxed (maximum 8 hours) and fully audited.
- While elevated, they act *inside* that tenant, under that tenant's RLS rules — the
  same as a tenant admin would.
- They can end the elevation early, and only one active elevation is allowed at a time.

**DECISION: reuse elevation — no parallel API.**
The platform admin's flow for "assign a role to a user in tenant X" is: elevate into
tenant X → use the **same** `/roles` and `/users/{id}/roles` endpoints a tenant admin
uses → end the elevation. This satisfies "across all tenant users" with zero new
authorization code and a complete audit trail.

**Do NOT build separate `/platform/tenants/{id}/users/{id}/roles` endpoints.** A
parallel path would need its own authorization checks and audit path, would bypass the
RLS design, and would fail security review. The work here is only: verify the
elevate → manage roles → end-elevation flow end to end (phase 5), and add a small UI
affordance if an elevated admin can't currently reach the Settings section.

Two rules that must hold in any new code:

- **SUPER_ADMIN (or any role carrying a `platform:*` permission) must never be
  assignable through tenant-facing endpoints.** This is already enforced — keep it that
  way in everything new.
- **`account_type` is the only correct way to tell platform from tenant users — never
  check `tenant_id IS NULL`.** This is a hard rule from
  `apps/control_plane/src/control_plane/CLAUDE.md`; getting it wrong creates a
  privilege-escalation bug.

One more edge case — **DECISION: add the self-lockout guard.** The scenario: the only
admin with `roles:manage` revokes that role from themselves (nobody in the tenant can
manage roles anymore; the only recovery is a platform operator doing break-glass
elevation, i.e. a support ticket). The guard is one check in `revoke_role`: if the
target user **is the caller** AND removing this role would leave them **without the
`roles:manage` permission**, return 409 with a clear message ("you cannot remove your
own last role-management role"). Two implementation notes:

- The condition is "would lose the **permission**", not "is removing a specific role" —
  the caller might hold `roles:manage` through two different roles, and removing one of
  them is fine.
- Keep it self-only. Do **not** build a cross-user "last admin in the tenant" check —
  one admin removing another admin's role is an explicit, audited act, and elevation
  exists as the recovery path.

---

## 4. Rules that apply to all three tickets

These come from the repo's `CLAUDE.md` files and existing code. Deviating from them
will fail code review:

1. **Response format:** every endpoint returns `ResponseModel[T]` via `ok(...)`, and
   errors are raised as `CustomAPIException` subclasses — never a bare dict, never
   `HTTPException`.
2. **RLS is the tenant isolation layer:** run every query through the tenant-scoped
   session (`TenantSession`). Never write raw SQL that skips the tenant context.
3. **Cache invalidation:** every change that affects anyone's effective permissions
   must call `resolver.invalidate(...)` for **every affected user**.
4. **Audit everything:** `emit_auth_event` on every mutation; extend the `AuthEvent`
   enum where needed; meta carries ids/names only.
5. **Idempotency:** mutating endpoints should follow the existing idempotency pattern —
   see how the invite endpoint in `users.py` does it.
6. **Migrations:** only needed if you add columns or seed rows. Use
   `just makemigration` (it generates a random hex revision id with a date-prefixed
   filename). **Never number migrations by hand** (`0023`, `0024`…) — two branches will
   grab the same number and break `alembic upgrade head`. Also remember RLS policies
   live in `migrations/`, not in the model files.
7. **Definition of done:** backend — `just check` passes (ruff + mypy --strict +
   pytest; integration tests need `just up && just migrate` first). Frontend — tsc +
   eslint + tests + build all pass.

---

## 5. Suggested order of work

| Phase | What to build |
|---|---|
| 1 | `GET /permissions` and `GET /roles/{id}` — the read endpoints. These unblock all the UI work. |
| 2 | `PATCH /roles/{id}` and `DELETE /roles/{id}` (409 while held), the new `AuthEvent` values, holder cache invalidation on PATCH, the self-lockout guard in `revoke_role`, and tests for every guard listed above. |
| 3 | `GET /users/{id}/roles` (or roles inside the user list response). |
| 4 | Settings page UI: read-only Permissions table, Roles list + create/edit dialog, user-role assignment view. |
| 5 | Verify the platform-admin flow end to end (elevate → manage roles → end elevation). |

## Decisions (settled — build to these)

These were open questions; they are now decided. Details and reasoning are inline in
the sections above.

1. **Permission "CRUD" = read-only catalog** (`GET /permissions` only). No
   create/update/delete anywhere — new permissions are a code change + seed migration.
   (Section 1.)
2. **Deleting a role that users still hold → 409 with the holder count.** No silent
   cascade; revoke per user first. (Section 2.)
3. **Platform-admin cross-tenant assignment uses the elevation flow.** No parallel
   `/platform/...` assignment API. (Section 3.)
4. **Self-lockout guard is in:** a caller cannot revoke their own last source of the
   `roles:manage` permission → 409. Self-only; no cross-user "last admin" check.
   (Section 3.)
