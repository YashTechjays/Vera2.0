# RBAC Roles & Permissions (Settings) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the three RBAC tickets — a read-only permission catalog, full role management (detail/edit/delete), and user-role assignment — as new backend endpoints plus a "Roles & Permissions" section in the frontend Settings page, gated by `roles:manage`.

**Architecture:** Most of RBAC already exists (tables, `require()` permission checks, role create/list, assign/revoke). This plan only adds the missing endpoints to the existing `roles.py` router (same patterns: `ResponseModel[T]`/`ok()`, `TenantSession` RLS, `emit_auth_event` audit, `resolver.invalidate` cache), one migration to widen the `auth_audit_log` event CHECK, and a new self-contained Settings section on the frontend built from the `ApiKeysSection` template.

**Tech Stack:** Backend — FastAPI + SQLAlchemy async + Alembic + pytest (`vera-backend`, run via `just`/`uv`). Frontend — React + Vite + TypeScript + shadcn/Radix + Vitest (`vera-frontend`, run via `npm`).

## Global Constraints

These come from the task spec (scratchpad `rbac-task-doc.txt`) and the repo `CLAUDE.md` files. Every task implicitly includes them:

1. **Response format:** every endpoint returns `ResponseModel[T]` via `ok(...)`; errors are raised as `CustomAPIException` subclasses — never a bare dict, never `HTTPException`.
2. **RLS:** every query runs through the tenant-scoped session (`TenantSession`). Never raw SQL that skips tenant context.
3. **Cache invalidation:** every change that affects anyone's effective permissions calls `resolver.invalidate(tenant_id, user_id)` for **every** affected user.
4. **Audit:** `emit_auth_event` on every mutation; `meta` carries ids/names only (never anything that could be PHI).
5. **Platform permissions never reach tenants:** filter `platform:*` from tenant-visible catalogs; block them from tenant roles (use `is_platform_permission()` / `roles_grant_platform_permission()` from `api/v1/common.py`).
6. **`account_type` is the only signal for platform-vs-tenant** — never branch on `tenant_id IS NULL`.
7. **Migrations:** `just makemigration` only (random hex revision id, date-prefixed filename) — never hand-numbered.
8. **Definition of done:** backend `just check` (ruff + mypy --strict + pytest; integration tests need `just up && just migrate` first); frontend `npm run lint` + `npm run test` + `npm run build` (tsc runs inside build). Per repo-root `CLAUDE.md`, run the **code-simplifier** agent ("simplify code") on the change before claiming done, then re-run the checks.
9. **Settled decisions (do not reopen):** permissions are a **read-only** catalog; role delete while users hold it → **409 with holder count** (no cascade); platform admins use the **elevation flow** (no `/platform/...` assignment API); **self-lockout guard**: a caller cannot revoke their own last source of `roles:manage` → 409 (self-only; no cross-user "last admin" check).

---

## Part 0 — Background: how RBAC works in this repo today

*(Read this before any task. New endpoints must look like they were always here.)*

**Tables** (`vera-backend/packages/vera_core/src/vera_core/models/rbac.py`):

| Table | What it stores | Key facts |
|---|---|---|
| `permission` | Global catalog of codes (`calls:read`, `roles:manage`, …) | No `tenant_id`, no RLS — tenants can never own a permission. Seeded from `rbac_defaults.py` (13 tenant codes + 5 `platform:*` codes). |
| `role` | Role definitions | `tenant_id NULL` ⇒ global **system role** (SUPER_ADMIN, TENANT_ADMIN, SUPERVISOR); set ⇒ tenant's custom role. UNIQUE `(tenant_id, name)` with `postgresql_nulls_not_distinct`. Catalog RLS: read own + NULL-tenant rows, write only own. |
| `role_permission` | Role ↔ permission links | FK cascade on role delete. UNIQUE `(role_id, permission_id)`. |
| `user_role` | User ↔ role grants (`granted_by`, `granted_at`) | Strict RLS (own tenant only). UNIQUE `(app_user_id, role_id)`. |

**Permission checking** (`apps/control_plane/src/control_plane/auth/rbac.py`): routes declare `_caller: VerifiedIdentity = require("roles:manage")`. The resolver caches each user's effective permission set (Redis in prod, 30s in-memory in tests) — which is why every permission-affecting write must call `resolver.invalidate(tenant_id, user_id)`.

**Existing endpoints** (`apps/control_plane/src/control_plane/api/v1/roles.py`, all gated `roles:manage`): `GET /roles`, `POST /roles`, `POST /users/{user_id}/roles`, `DELETE /users/{user_id}/roles/{role_id}`. Missing (this plan): `GET /permissions`, `GET /roles/{id}`, `PATCH /roles/{id}`, `DELETE /roles/{id}`, `GET /users/{id}/roles`, the self-lockout guard, and a `description` field on create.

**Audit:** mutations call `emit_auth_event(audit, tenant_id=..., event=AuthEvent.X, ip=client_ip(request), user_id=_caller.user_id, meta={ids/names only})`. ⚠️ `auth_audit_log.event_type` has a DB CHECK constraint built from the `AuthEvent` enum (`ck_auth_audit_log_event_type_valid`), so **adding enum members requires a migration** — exact precedent: `migrations/versions/0017_persona_tweak_event.py`. (The task doc said "migrations only for columns/seeds" — this CHECK widening is the one exception, discovered in code.)

**Platform admins:** never get parallel APIs. They break-glass **elevate** into one tenant (`POST /platform/elevations`, ≤8h, audited) and then use the same tenant endpoints under that tenant's RLS. SUPER_ADMIN carries every permission, so once elevated, `require("roles:manage")` passes.

**Frontend:** Settings (`vera-frontend/src/pages/Settings.tsx`) is a single page of sections, each `{usePermission("x") && <Section />}`. API modules live in `src/lib/*.ts` over `apiRequest<T>()` (unwraps the envelope, throws typed `ApiError`). Template to copy: `src/components/settings/ApiKeysSection.tsx` + `src/lib/api-keys.ts`. Tests are pure-logic Vitest (`*.test.ts` colocated) — there is **no component-render test harness**, so put derivable logic in plain functions and test those.

## File map

**Backend (modify):**
- `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py` — all new endpoints + schemas (kept in-file, matching the existing style)
- `vera-backend/packages/vera_core/src/vera_core/models/enums.py` — `AuthEvent.ROLE_UPDATED`, `AuthEvent.ROLE_DELETED`
- `vera-backend/migrations/versions/<generated>_role_update_delete_events.py` — widen the event CHECK (create via `just makemigration`)
- `vera-backend/tests/integration/control_plane/test_admin.py` — new tests in the `# --- roles ---` section
- `vera-backend/tests/integration/control_plane/test_platform_elevation.py` — one elevation-flow test

**Frontend (create):**
- `vera-frontend/src/lib/roles.ts` — typed API module + `groupPermissionsByPrefix` helper
- `vera-frontend/src/lib/roles.test.ts` — unit tests for the helper
- `vera-frontend/src/components/settings/PermissionsTable.tsx` — read-only catalog
- `vera-frontend/src/components/settings/RoleDialog.tsx` — create/edit dialog with grouped permission checkboxes
- `vera-frontend/src/components/settings/UserRolesCard.tsx` — user-role assignment view
- `vera-frontend/src/components/settings/RolesSection.tsx` — the section shell (roles list, delete, mounts the above)

**Frontend (modify):**
- `vera-frontend/src/pages/Settings.tsx` — mount `RolesSection` behind `usePermission("roles:manage")`

**Working setup before Task 1:** in `vera-backend/`: `just up && just migrate` (Docker must be running — if `just up` fails with port 5432 unreachable, see the Docker Desktop WSL note in project memory: reboot Windows first). Run tests with `uv run pytest ...`.

---

### Task 1: `GET /permissions` — read-only catalog endpoint

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: existing `Permission` model, `is_platform_permission()` (`common.py`), `require`, `ok`, `ResponseModel`.
- Produces: `PermissionResponse(id: UUID, code: str, description: str)` — reused by Task 2's `RoleDetailResponse` and consumed by the frontend as `{ id, code, description }`.

- [ ] **Step 1: Write the failing tests** — append to the `# --- roles ---` section of `test_admin.py`:

```python
async def test_list_permissions_hides_platform_tier(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    codes = {p["code"] for p in rows}
    assert "roles:manage" in codes  # tenant-tier codes are present
    assert not any(c.startswith("platform:") for c in codes)  # platform tier hidden
    assert all(set(p) == {"id", "code", "description"} for p in rows)


async def test_list_permissions_requires_roles_manage(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify they fail**

Run (in `vera-backend/`): `uv run pytest tests/integration/control_plane/test_admin.py -k "list_permissions" -v`
Expected: both FAIL with `assert 404 == 200` / `assert 404 == 403` (route doesn't exist).

- [ ] **Step 3: Implement** — in `roles.py`, add `PermissionResponse` next to the other schemas and the route after `list_roles`:

```python
class PermissionResponse(BaseModel):
    id: UUID
    code: str
    description: str
```

```python
@router.get(
    "/permissions",
    response_model=ResponseModel[list[PermissionResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_permissions(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[list[PermissionResponse]]:
    # The permission catalog is global (no tenant_id, no RLS) and code-defined —
    # tenants get a read-only view. Platform-tier codes are never shown to a
    # tenant: they can't be granted here, so they must not appear as options.
    rows = (await session.execute(select(Permission).order_by(Permission.code))).scalars().all()
    return ok(
        [
            PermissionResponse(id=p.id, code=p.code, description=p.description)
            for p in rows
            if not is_platform_permission(p.code)
        ]
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "list_permissions" -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): read-only GET /permissions catalog (platform tier hidden)"
```

---

### Task 2: `GET /roles/{role_id}` — role detail with permissions

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: `PermissionResponse` from Task 1; existing `RoleResponse`, `Role`, `RolePermission`, `NotFoundError`.
- Produces: `RoleDetailResponse(RoleResponse)` with `permissions: list[PermissionResponse]`; helper `async _role_permissions(session, role_id) -> list[Permission]` (reused by Task 4's PATCH response).

- [ ] **Step 1: Write the failing tests:**

```python
async def test_get_role_detail_includes_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    perms = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    users_read = next(p for p in perms.json()["data"] if p["code"] == "users:read")
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "DETAIL_ROLE", "permission_ids": [users_read["id"]]},
    )
    role_id = created.json()["data"]["id"]

    detail = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["name"] == "DETAIL_ROLE"
    assert data["is_system"] is False
    assert [p["code"] for p in data["permissions"]] == ["users:read"]


async def test_get_role_detail_unknown_id_is_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(f"/api/v1/roles/{uuid4()}", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 404


async def test_get_system_role_detail_hides_platform_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    super_admin_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPER_ADMIN")
    detail = await client.get(
        f"/api/v1/roles/{super_admin_id}", headers=_auth(rbac_world.admin_token)
    )
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["is_system"] is True
    # A tenant admin may see the system role exists, but never its platform codes.
    assert not any(p["code"].startswith("platform:") for p in data["permissions"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "role_detail or system_role_detail" -v`
Expected: FAIL with 404/405 mismatches (route missing).

- [ ] **Step 3: Implement** — schema, helper, and route in `roles.py`:

```python
class RoleDetailResponse(RoleResponse):
    permissions: list[PermissionResponse]
```

```python
async def _role_permissions(session: AsyncSession, role_id: UUID) -> list[Permission]:
    return list(
        (
            await session.execute(
                select(Permission)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role_id)
                .order_by(Permission.code)
            )
        )
        .scalars()
        .all()
    )


def _to_detail(role: Role, permissions: list[Permission]) -> RoleDetailResponse:
    return RoleDetailResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        is_system=role.tenant_id is None,
        # Same rule as GET /permissions: platform-tier codes never render for a tenant.
        permissions=[
            PermissionResponse(id=p.id, code=p.code, description=p.description)
            for p in permissions
            if not is_platform_permission(p.code)
        ],
    )
```

```python
@router.get(
    "/roles/{role_id}",
    response_model=ResponseModel[RoleDetailResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_role(
    role_id: UUID,
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[RoleDetailResponse]:
    role = (
        await session.execute(select(Role).where(Role.id == role_id))
    ).scalar_one_or_none()
    if role is None:  # unknown id, or another tenant's role hidden by RLS
        raise NotFoundError(message="no such role")
    return ok(_to_detail(role, await _role_permissions(session, role_id)))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "role_detail or system_role_detail" -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): GET /roles/{id} detail with permission list"
```

---

### Task 3: `description` on `POST /roles`

The task doc calls this out: create currently writes `description=""` always; the edit dialog needs it.

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py` (`CreateRoleRequest`, `create_role`)
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Produces: `CreateRoleRequest.description: str` (default `""`, max 2000) — frontend sends it in Task 8's `createRole`.

- [ ] **Step 1: Write the failing test:**

```python
async def test_create_role_accepts_description(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "DESCRIBED", "description": "Sees billing", "permission_ids": []},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["description"] == "Sees billing"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "accepts_description" -v`
Expected: FAIL — `assert '' == 'Sees billing'`.

- [ ] **Step 3: Implement** — two edits in `roles.py`:

```python
class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    permission_ids: list[UUID] = Field(default_factory=list)
```

and in `create_role`, change the construction line to:

```python
    role = Role(tenant_id=tenant_id, name=body.name, description=body.description)
```

- [ ] **Step 4: Run to verify it passes** — same command, expected PASS. Also run the full roles group to catch regressions: `uv run pytest tests/integration/control_plane/test_admin.py -v`.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): accept description on role create"
```

---

### Task 4: `PATCH /roles/{role_id}` + `ROLE_UPDATED` event + migration

The largest backend task: edit name/description/permission-set, with every guard from the spec, holder cache invalidation, and the audit event (which needs the CHECK-widening migration).

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py` (add both new events here — `ROLE_DELETED` too, so one migration covers Task 5)
- Create: migration via `just makemigration` (name arg: `role-update-delete-events`)
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: `_role_permissions` / `_to_detail` (Task 2), `_conflict_or_raise`, `is_platform_permission`, `Resolver`.
- Produces: `UpdateRoleRequest(name: str | None, description: str | None, permission_ids: list[UUID] | None)` — `None` means "leave unchanged"; PATCH returns `ResponseModel[RoleDetailResponse]`. `AuthEvent.ROLE_UPDATED = "role_updated"`, `AuthEvent.ROLE_DELETED = "role_deleted"`.

- [ ] **Step 1: Add the enum members** — in `enums.py`, after `ROLE_CREATED`:

```python
    ROLE_CREATED = "role_created"
    ROLE_UPDATED = "role_updated"
    ROLE_DELETED = "role_deleted"
    ROLE_GRANT = "role_grant"
```

- [ ] **Step 2: Generate and write the migration** — run `just makemigration role-update-delete-events` (in `vera-backend/`), then replace the generated file's body following the exact pattern of `migrations/versions/0017_persona_tweak_event.py` (keep the generated revision id / down_revision):

```python
"""widen auth_audit_log.event_type CHECK for role_updated / role_deleted

`auth_audit_log.event_type` is constrained by a CHECK built from the `AuthEvent`
StrEnum (`ck_auth_audit_log_event_type_valid`; see 0006/0017). This release adds
`role_updated` and `role_deleted`, so an already-provisioned database rejects
them until the CHECK is widened. Drop-and-recreate the named constraint, exactly
as 0017: `DROP ... IF EXISTS` is a no-op on a fresh DB (0001 built it with the
new values) and an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers — KEEP the values `just makemigration` generated.
# revision: str = "<generated hex>"
# down_revision: str | None = "<generated>"

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The set before this migration (0017's set), used to restore on downgrade.
_OLD_VALUES = (
    "login_success",
    "login_failure",
    "mfa_challenge",
    "user_invited",
    "invite_accepted",
    "user_deactivated",
    "role_created",
    "role_grant",
    "role_revoke",
    "api_key_created",
    "api_key_revoked",
    "tenant_elevation_granted",
    "tenant_elevation_ended",
    "provider_enabled",
    "provider_disabled",
    "persona_tweak_updated",
    "authz_allow",
    "authz_deny",
)
_NEW_VALUES = _OLD_VALUES + ("role_updated", "role_deleted")


def _check(values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"CHECK (event_type IN ({quoted}))"


def _recreate(values: Sequence[str]) -> None:
    op.execute(f"ALTER TABLE auth_audit_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE auth_audit_log ADD CONSTRAINT {_CONSTRAINT} {_check(values)}")


def upgrade() -> None:
    _recreate(_NEW_VALUES)


def downgrade() -> None:
    _recreate(_OLD_VALUES)
```

⚠️ Before finalizing `_OLD_VALUES`, open `0017_persona_tweak_event.py` and copy its `_OLD_VALUES + ("persona_tweak_updated",)` result verbatim — the list above was transcribed from the current enum and must match the DB's actual constraint. Then run `just migrate` and confirm it applies cleanly.

- [ ] **Step 3: Write the failing tests:**

```python
async def test_patch_role_updates_fields_and_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    perms = (
        await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    ).json()["data"]
    users_read = next(p["id"] for p in perms if p["code"] == "users:read")
    calls_read = next(p["id"] for p in perms if p["code"] == "calls:read")
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "PATCH_ME", "permission_ids": [users_read]},
    )
    role_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "PATCHED", "description": "now different", "permission_ids": [calls_read]},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["name"] == "PATCHED"
    assert data["description"] == "now different"
    assert [p["code"] for p in data["permissions"]] == ["calls:read"]

    # Omitted fields stay unchanged (None = leave alone).
    partial = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"description": "only this"},
    )
    assert partial.json()["data"]["name"] == "PATCHED"
    assert partial.json()["data"]["description"] == "only this"


async def test_patch_system_role_is_forbidden(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    resp = await client.patch(
        f"/api/v1/roles/{supervisor_id}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "HIJACKED"},
    )
    assert resp.status_code == 403  # explicit ownership check, not a silent 0-row update


async def test_patch_role_rejects_platform_permission(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "NO_PLATFORM_VIA_PATCH", "permission_ids": []},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        platform_perm_id = await session.scalar(
            text("SELECT id FROM permission WHERE code = 'platform:elevations:read'")
        )
    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": [str(platform_perm_id)]},
    )
    assert resp.status_code == 403


async def test_patch_role_unknown_permission_is_400_and_dup_name_409(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    a = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "PATCH_A", "permission_ids": []},
    )
    await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "PATCH_B", "permission_ids": []},
    )
    a_id = a.json()["data"]["id"]

    bad_perm = await client.patch(
        f"/api/v1/roles/{a_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": [str(uuid4())]},
    )
    assert bad_perm.status_code == 400

    dup = await client.patch(
        f"/api/v1/roles/{a_id}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "PATCH_B"},
    )
    assert dup.status_code == 409  # unique (tenant_id, name)


async def test_patch_role_invalidates_holder_permission_cache(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # End-to-end proof of the cache rule: norole gains users:read via a custom
    # role, then loses it the moment PATCH strips the permission — no TTL wait.
    perms = (
        await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    ).json()["data"]
    users_read = next(p["id"] for p in perms if p["code"] == "users:read")
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "TEMP_USER_READERS", "permission_ids": [users_read]},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        norole_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'norole@test.example' AND tenant_id = :t")
            .bindparams(t=rbac_world.tenant_id)
        )

    denied = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert denied.status_code == 403  # baseline: no permission (and it is now cached)

    assign = await client.post(
        f"/api/v1/users/{norole_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": role_id},
    )
    assert assign.status_code == 200, assign.text
    allowed = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert allowed.status_code == 200  # assign invalidated norole's cache

    stripped = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": []},
    )
    assert stripped.status_code == 200, stripped.text
    denied_again = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert denied_again.status_code == 403  # PATCH invalidated every holder's cache

    # Cleanup so later tests see norole with no roles.
    await client.request(
        "DELETE",
        f"/api/v1/users/{norole_id}/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
    )
```

- [ ] **Step 4: Run to verify they fail**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "patch_role or patch_system" -v`
Expected: FAIL with 405 (no PATCH route).

- [ ] **Step 5: Implement the endpoint** — in `roles.py` (import `AuthEvent` already present; `ConflictError` not needed here):

```python
class UpdateRoleRequest(BaseModel):
    """PATCH semantics: a field left as None is not changed; `permission_ids`
    (when present) REPLACES the role's whole permission set."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    permission_ids: list[UUID] | None = None
```

```python
@router.patch(
    "/roles/{role_id}",
    response_model=ResponseModel[RoleDetailResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_role(
    role_id: UUID,
    body: UpdateRoleRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    resolver: Resolver,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[RoleDetailResponse]:
    role = (
        await session.execute(select(Role).where(Role.id == role_id))
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(message="no such role")
    # Explicit ownership check (spec): don't rely on RLS's silent 0-row update.
    # `tenant_id IS NULL` here means "global system role", not "platform caller".
    if role.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="system roles cannot be modified"
        )

    changed: list[str] = []
    if body.name is not None and body.name != role.name:
        role.name = body.name
        changed.append("name")
    if body.description is not None and body.description != role.description:
        role.description = body.description
        changed.append("description")

    if body.permission_ids is not None:
        permissions = (
            (
                await session.execute(
                    select(Permission).where(Permission.id.in_(body.permission_ids))
                )
            )
            .scalars()
            .all()
        )
        if len(permissions) != len(set(body.permission_ids)):
            raise BadRequestError(message="unknown permission id")
        if any(is_platform_permission(p.code) for p in permissions):
            raise CustomAPIException(
                DefaultExceptionCode.FORBIDDEN,
                message="cannot grant a platform-tier permission to a tenant role",
            )
        links = (
            (
                await session.execute(
                    select(RolePermission).where(RolePermission.role_id == role_id)
                )
            )
            .scalars()
            .all()
        )
        if {link.permission_id for link in links} != {p.id for p in permissions}:
            for link in links:
                await session.delete(link)
            for permission in permissions:
                session.add(
                    RolePermission(
                        tenant_id=tenant_id, role_id=role_id, permission_id=permission.id
                    )
                )
            changed.append("permissions")

    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict_or_raise(exc, "a role with that name already exists") from exc

    if "permissions" in changed:
        # The role's grants changed under live users — drop every holder's cached
        # permission set or they keep the old access until the cache TTL expires.
        holders = (
            (
                await session.execute(
                    select(UserRole.app_user_id).where(UserRole.role_id == role_id)
                )
            )
            .scalars()
            .all()
        )
        for holder_id in holders:
            await resolver.invalidate(tenant_id, holder_id)

    if changed:
        await emit_auth_event(
            audit,
            tenant_id=tenant_id,
            event=AuthEvent.ROLE_UPDATED,
            ip=client_ip(request),
            user_id=_caller.user_id,
            meta={"role_id": str(role_id), "changed": changed},
        )
    return ok(_to_detail(role, await _role_permissions(session, role_id)))
```

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "patch" -v`
Expected: 5 PASSED (including the cache-invalidation behavioral test).

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/packages/vera_core/src/vera_core/models/enums.py vera-backend/migrations/versions vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): PATCH /roles/{id} with guards, holder cache invalidation, ROLE_UPDATED audit"
```

---

### Task 5: `DELETE /roles/{role_id}` — blocked while held (409)

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: `AuthEvent.ROLE_DELETED` (added in Task 4), `func` (already imported in `roles.py`).
- Produces: `DELETE /roles/{role_id}` → `ResponseModel[None]`; while held → HTTP 409 whose envelope has `data={"holder_count": n}` and a human message the frontend shows verbatim.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_delete_role_blocked_while_held_then_succeeds(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "DELETE_ME", "permission_ids": []},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        norole_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'norole@test.example' AND tenant_id = :t")
            .bindparams(t=rbac_world.tenant_id)
        )
    await client.post(
        f"/api/v1/users/{norole_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": role_id},
    )

    blocked = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert blocked.status_code == 409  # DECISION: no silent cascade
    assert blocked.json()["data"]["holder_count"] == 1

    await client.request(
        "DELETE",
        f"/api/v1/users/{norole_id}/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
    )
    deleted = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert deleted.status_code == 200, deleted.text
    listing = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    assert "DELETE_ME" not in {r["name"] for r in listing.json()["data"]}


async def test_delete_system_role_forbidden_and_unknown_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    forbidden = await client.request(
        "DELETE", f"/api/v1/roles/{supervisor_id}", headers=_auth(rbac_world.admin_token)
    )
    assert forbidden.status_code == 403
    missing = await client.request(
        "DELETE", f"/api/v1/roles/{uuid4()}", headers=_auth(rbac_world.admin_token)
    )
    assert missing.status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "delete_role or delete_system" -v`
Expected: FAIL with 405 (no DELETE route on /roles/{id}).

- [ ] **Step 3: Implement:**

```python
@router.delete(
    "/roles/{role_id}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def delete_role(
    role_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[None]:
    role = (
        await session.execute(select(Role).where(Role.id == role_id))
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(message="no such role")
    if role.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="system roles cannot be deleted"
        )
    # DECISION: never cascade a delete over live grants. The admin revokes per
    # user first (each revoke is audited + cache-invalidating), so by the time
    # this runs there is no holder cache to invalidate.
    holder_count = (
        await session.execute(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
        )
    ).scalar_one()
    if holder_count:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=f"{holder_count} user(s) still hold this role — remove it from them first",
            data={"holder_count": holder_count},
        )

    await session.delete(role)  # FK cascade clears role_permission links
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_DELETED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"role_id": str(role_id), "name": role.name},
    )
    return ok(None, message="Role deleted.")
```

- [ ] **Step 4: Run to verify they pass** — same `-k` command, 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): DELETE /roles/{id} — 409 with holder count while held, ROLE_DELETED audit"
```

---

### Task 6: Self-lockout guard in `revoke_role`

DECISION: a caller cannot remove their **own last source** of `roles:manage` (recovery would need a platform break-glass ticket). Condition is "would lose the permission", not "is removing a specific role" — holding `roles:manage` via a second role makes the revoke fine. Self-only: no cross-user "last admin" check.

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py` (`revoke_role`)
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: existing `revoke_role`, `ConflictError` (add to the `control_plane.exceptions` import in `roles.py`).

- [ ] **Step 1: Write the failing tests:**

```python
async def test_cannot_revoke_own_last_roles_manage_source(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'admin@test.example' AND tenant_id = :t")
            .bindparams(t=rbac_world.tenant_id)
        )
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    tenant_admin_id = next(
        r["id"] for r in roles.json()["data"] if r["name"] == "TENANT_ADMIN"
    )
    # TENANT_ADMIN is the admin persona's only role → its only roles:manage source.
    resp = await client.request(
        "DELETE",
        f"/api/v1/users/{admin_id}/roles/{tenant_admin_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 409


async def test_revoking_one_of_two_roles_manage_sources_is_allowed(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'admin@test.example' AND tenant_id = :t")
            .bindparams(t=rbac_world.tenant_id)
        )
    perms = (
        await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    ).json()["data"]
    roles_manage = next(p["id"] for p in perms if p["code"] == "roles:manage")
    extra = await client.post(
        "/api/v1/roles",
        headers=_auth(rbac_world.admin_token),
        json={"name": "SECOND_MANAGER", "permission_ids": [roles_manage]},
    )
    extra_id = extra.json()["data"]["id"]
    await client.post(
        f"/api/v1/users/{admin_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": extra_id},
    )
    # Removing the EXTRA role is fine — TENANT_ADMIN still grants roles:manage.
    resp = await client.request(
        "DELETE",
        f"/api/v1/users/{admin_id}/roles/{extra_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "lockout or two_roles_manage" -v`
Expected: `test_cannot_revoke_own_last...` FAILS (`assert 200 == 409` — the revoke currently succeeds); the second may pass already.

⚠️ If the first test ran before the guard exists, it **revoked the shared admin persona's TENANT_ADMIN role** (session-scoped fixture) — restore it before continuing: rerun the suite fresh after implementing, or re-assign via `POST /api/v1/users/{admin_id}/roles`. Simplest: implement Step 3 first, then run both tests together on a clean session (`uv run pytest tests/integration/control_plane/test_admin.py -v`).

- [ ] **Step 3: Implement** — in `revoke_role`, after the `assignment is None` check and before `session.delete(assignment)`; also add `ConflictError` to the `control_plane.exceptions` import list:

```python
    # Self-lockout guard (settled decision): a caller may not remove their own
    # LAST source of roles:manage — nobody in the tenant could manage roles and
    # the only recovery is platform break-glass elevation. Self-only by design.
    if user_id == _caller.user_id:
        other_source = (
            await session.execute(
                select(Permission.id)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(UserRole, UserRole.role_id == RolePermission.role_id)
                .where(
                    UserRole.app_user_id == user_id,
                    UserRole.role_id != role_id,
                    Permission.code == "roles:manage",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if other_source is None:
            raise ConflictError(
                message="you cannot remove your own last role-management role"
            )
```

(`Permission` and `RolePermission` are already imported in `roles.py`.)

- [ ] **Step 4: Run to verify both pass**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -v`
Expected: full module PASSED (run the whole module — earlier tests confirm the admin persona kept its role).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): self-lockout guard — cannot revoke own last roles:manage source"
```

---

### Task 7: `GET /users/{user_id}/roles`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: `_user_in_tenant`, `_to_response`, existing `RoleResponse`.
- Produces: `GET /users/{user_id}/roles` → `ResponseModel[list[RoleResponse]]` — the frontend's `listUserRoles(userId)`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_list_user_roles(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "haroles@test.example", "send_email": False, "role_ids": [supervisor_id]},
    )
    user_id = invite.json()["data"]["user_id"]

    resp = await client.get(
        f"/api/v1/users/{user_id}/roles", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert [r["name"] for r in resp.json()["data"]] == ["SUPERVISOR"]


async def test_list_user_roles_unknown_user_404_and_norole_403(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    missing = await client.get(
        f"/api/v1/users/{uuid4()}/roles", headers=_auth(rbac_world.admin_token)
    )
    assert missing.status_code == 404
    denied = await client.get(
        f"/api/v1/users/{uuid4()}/roles", headers=_auth(rbac_world.norole_token)
    )
    assert denied.status_code == 403
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -k "list_user_roles" -v`
Expected: FAIL with 404-shape mismatches (no route → FastAPI 404 for the first test's 200 assertion).

- [ ] **Step 3: Implement:**

```python
@router.get(
    "/users/{user_id}/roles",
    response_model=ResponseModel[list[RoleResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_user_roles(
    user_id: UUID,
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[list[RoleResponse]]:
    if not await _user_in_tenant(session, user_id):
        raise NotFoundError(message="no such user in this tenant")
    rows = (
        (
            await session.execute(
                select(Role)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.app_user_id == user_id)
                .order_by(Role.name)
            )
        )
        .scalars()
        .all()
    )
    return ok([_to_response(r) for r in rows])
```

- [ ] **Step 4: Run to verify they pass** — `-k "list_user_roles"`, 2 PASSED. Then the full backend gate: `just check` (unit + lint + mypy; integration needs the DB up).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/roles.py vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): GET /users/{id}/roles"
```

---

### Task 8: Frontend API module `src/lib/roles.ts` + grouping helper

**Files:**
- Create: `vera-frontend/src/lib/roles.ts`
- Test: `vera-frontend/src/lib/roles.test.ts`

**Interfaces:**
- Consumes: `apiRequest`, `randomId` from `@/lib/api/client`.
- Produces (used by Tasks 9–12): types `Permission`, `Role`, `RoleDetail`, `PermissionGroup`; functions `listPermissions()`, `listRoles()`, `getRole(id)`, `createRole(name, description, permissionIds)`, `updateRole(id, patch)`, `deleteRole(id)`, `listUserRoles(userId)`, `assignRole(userId, roleId)`, `revokeRole(userId, roleId)`, `groupPermissionsByPrefix(permissions)`.

- [ ] **Step 1: Write the failing test** — `src/lib/roles.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { groupPermissionsByPrefix, type Permission } from "./roles"

const perm = (id: string, code: string): Permission => ({ id, code, description: "" })

describe("groupPermissionsByPrefix", () => {
  it("groups by the code's first segment, sorted within and across groups", () => {
    const groups = groupPermissionsByPrefix([
      perm("1", "users:manage"),
      perm("2", "calls:write"),
      perm("3", "calls:read"),
      perm("4", "users:read"),
    ])
    expect(groups.map((g) => g.prefix)).toEqual(["calls", "users"])
    expect(groups[0].permissions.map((p) => p.code)).toEqual(["calls:read", "calls:write"])
    expect(groups[1].permissions.map((p) => p.code)).toEqual(["users:manage", "users:read"])
  })

  it("uses the whole code when there is no colon, and handles empty input", () => {
    expect(groupPermissionsByPrefix([])).toEqual([])
    const groups = groupPermissionsByPrefix([perm("1", "standalone")])
    expect(groups).toEqual([{ prefix: "standalone", permissions: [perm("1", "standalone")] }])
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `vera-frontend/`): `npm run test`
Expected: FAIL — cannot resolve `./roles`.

- [ ] **Step 3: Implement** — `src/lib/roles.ts`:

```ts
// Typed wrappers over the RBAC endpoints (all gated by roles:manage server-side).
// Mirror the backend contract (snake_case), like api-keys.ts.

import { apiRequest, randomId } from "@/lib/api/client"

/** One catalog entry (GET /permissions). Read-only — permissions are code-defined. */
export type Permission = {
  id: string
  code: string
  description: string
}

/** A role as listed (GET /roles). `is_system` roles are read-only for tenants. */
export type Role = {
  id: string
  name: string
  description: string
  is_system: boolean
}

/** Role detail including its permission set (GET /roles/{id}). */
export type RoleDetail = Role & { permissions: Permission[] }

export function listPermissions(): Promise<Permission[]> {
  return apiRequest<Permission[]>("/permissions")
}

export function listRoles(): Promise<Role[]> {
  return apiRequest<Role[]>("/roles")
}

export function getRole(roleId: string): Promise<RoleDetail> {
  return apiRequest<RoleDetail>(`/roles/${encodeURIComponent(roleId)}`)
}

export function createRole(
  name: string,
  description: string,
  permissionIds: string[],
): Promise<Role> {
  return apiRequest<Role>("/roles", {
    method: "POST",
    body: { name, description, permission_ids: permissionIds },
    headers: { "Idempotency-Key": randomId() },
  })
}

/** PATCH semantics: omitted fields stay unchanged; permission_ids replaces the set. */
export function updateRole(
  roleId: string,
  patch: { name?: string; description?: string; permission_ids?: string[] },
): Promise<RoleDetail> {
  return apiRequest<RoleDetail>(`/roles/${encodeURIComponent(roleId)}`, {
    method: "PATCH",
    body: patch,
  })
}

/** 409 while users still hold the role — the message carries the holder count. */
export function deleteRole(roleId: string): Promise<null> {
  return apiRequest<null>(`/roles/${encodeURIComponent(roleId)}`, { method: "DELETE" })
}

export function listUserRoles(userId: string): Promise<Role[]> {
  return apiRequest<Role[]>(`/users/${encodeURIComponent(userId)}/roles`)
}

export function assignRole(userId: string, roleId: string): Promise<null> {
  return apiRequest<null>(`/users/${encodeURIComponent(userId)}/roles`, {
    method: "POST",
    body: { role_id: roleId },
  })
}

/** 409 when revoking your own last roles:manage source (self-lockout guard). */
export function revokeRole(userId: string, roleId: string): Promise<null> {
  return apiRequest<null>(
    `/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`,
    { method: "DELETE" },
  )
}

export type PermissionGroup = { prefix: string; permissions: Permission[] }

/** Group the catalog by code prefix (calls:*, users:*, …) for readable checkbox lists. */
export function groupPermissionsByPrefix(permissions: Permission[]): PermissionGroup[] {
  const groups = new Map<string, Permission[]>()
  const sorted = [...permissions].sort((a, b) => a.code.localeCompare(b.code))
  for (const p of sorted) {
    const prefix = p.code.includes(":") ? p.code.slice(0, p.code.indexOf(":")) : p.code
    const bucket = groups.get(prefix)
    if (bucket) bucket.push(p)
    else groups.set(prefix, [p])
  }
  return [...groups.entries()].map(([prefix, perms]) => ({ prefix, permissions: perms }))
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npm run test` → PASS; `npm run lint` → clean.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/roles.ts vera-frontend/src/lib/roles.test.ts
git commit -m "feat(rbac-ui): typed roles/permissions API module with prefix grouping"
```

---

### Task 9: `PermissionsTable` — read-only catalog component

*(No render-test harness exists in this repo — component tasks verify via `tsc`/eslint and the manual walkthrough in Task 13. Logic stays in `roles.ts`, already tested.)*

**Files:**
- Create: `vera-frontend/src/components/settings/PermissionsTable.tsx`

**Interfaces:**
- Consumes: `Permission` from `@/lib/roles`; `Table*` from `@/components/ui/table`.
- Produces: `<PermissionsTable permissions={Permission[]} />` — mounted by `RolesSection` (Task 11).

- [ ] **Step 1: Implement:**

```tsx
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { Permission } from "@/lib/roles"

/** Read-only permission catalog. Permissions are defined in code and seeded by the
 *  platform — "managing" one means adding it to a role, never editing it here. */
export function PermissionsTable({ permissions }: { permissions: Permission[] }) {
  return (
    <div className="space-y-2">
      <div>
        <h3 className="text-sm font-medium">Permission catalog</h3>
        <p className="text-sm text-muted-foreground">
          Platform-defined capabilities. Grant them to users by adding them to a role.
        </p>
      </div>
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Code</TableHead>
              <TableHead>Description</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {permissions.length === 0 && (
              <TableRow>
                <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {permissions.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-mono text-xs">{p.code}</TableCell>
                <TableCell className="text-muted-foreground">{p.description}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Verify:** `npm run lint` clean (tsc runs with the Task 11 build once the component is imported).

- [ ] **Step 3: Commit**

```bash
git add vera-frontend/src/components/settings/PermissionsTable.tsx
git commit -m "feat(rbac-ui): read-only permission catalog table"
```

---

### Task 10: `RoleDialog` — create/edit dialog with grouped checkboxes

**Files:**
- Create: `vera-frontend/src/components/settings/RoleDialog.tsx`

**Interfaces:**
- Consumes: `createRole`, `updateRole`, `groupPermissionsByPrefix`, types from `@/lib/roles`; `Dialog*`, `Checkbox`, `Input`, `Textarea`, `Button` from `@/components/ui/*`; `ApiError` from `@/lib/api/client`.
- Produces: `<RoleDialog open onOpenChange role={RoleDetail | null} permissions onSaved />` — `role === null` means create mode; a `RoleDetail` means edit mode.

- [ ] **Step 1: Implement:**

```tsx
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import {
  createRole,
  groupPermissionsByPrefix,
  updateRole,
  type Permission,
  type RoleDetail,
} from "@/lib/roles"

type RoleDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new role; a RoleDetail = edit that role. */
  role: RoleDetail | null
  permissions: Permission[]
  onSaved: () => void | Promise<void>
}

export function RoleDialog({ open, onOpenChange, role, permissions, onSaved }: RoleDialogProps) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Re-seed the form each time the dialog opens (create: blank; edit: the role).
  useEffect(() => {
    if (!open) return
    setName(role?.name ?? "")
    setDescription(role?.description ?? "")
    setSelected(new Set(role?.permissions.map((p) => p.id) ?? []))
    setError(null)
  }, [open, role])

  const toggle = (id: string, checked: boolean) => {
    setSelected((cur) => {
      const next = new Set(cur)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const handleSave = async () => {
    if (!name.trim()) return
    setSaving(true)
    setError(null)
    try {
      if (role) {
        await updateRole(role.id, {
          name: name.trim(),
          description,
          permission_ids: [...selected],
        })
      } else {
        await createRole(name.trim(), description, [...selected])
      }
      await onSaved()
      onOpenChange(false)
    } catch (err) {
      // 409 = duplicate name; message comes from the server.
      setError(err instanceof ApiError ? err.message : "Could not save the role.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{role ? `Edit role: ${role.name}` : "Create role"}</DialogTitle>
          <DialogDescription>
            A role bundles permissions; assign it to users in the section below.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground" htmlFor="role-name">
              Name
            </label>
            <Input
              id="role-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. BILLING_VIEWER"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground" htmlFor="role-description">
              Description
            </label>
            <Textarea
              id="role-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this role is for"
              rows={2}
            />
          </div>

          <div className="space-y-3">
            <p className="text-xs font-medium text-muted-foreground">Permissions</p>
            {groupPermissionsByPrefix(permissions).map((group) => (
              <div key={group.prefix} className="space-y-1">
                <p className="text-xs font-semibold uppercase tracking-wide">{group.prefix}</p>
                {group.permissions.map((p) => (
                  <label key={p.id} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={selected.has(p.id)}
                      onCheckedChange={(checked) => toggle(p.id, checked === true)}
                    />
                    <span className="font-mono text-xs">{p.code}</span>
                    <span className="truncate text-muted-foreground">{p.description}</span>
                  </label>
                ))}
              </div>
            ))}
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="button" disabled={saving || !name.trim()} onClick={handleSave}>
              {saving ? "Saving…" : role ? "Save changes" : "Create role"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2: Verify:** `npm run lint` clean. (If `@/components/ui/textarea` or `checkbox` export names differ, open those files and match them — both exist per the UI inventory.)

- [ ] **Step 3: Commit**

```bash
git add vera-frontend/src/components/settings/RoleDialog.tsx
git commit -m "feat(rbac-ui): role create/edit dialog with grouped permission checkboxes"
```

---

### Task 11: `RolesSection` + mount in Settings

**Files:**
- Create: `vera-frontend/src/components/settings/RolesSection.tsx`
- Modify: `vera-frontend/src/pages/Settings.tsx`

**Interfaces:**
- Consumes: `listRoles`, `listPermissions`, `getRole`, `deleteRole` from `@/lib/roles`; `PermissionsTable` (Task 9), `RoleDialog` (Task 10); `Badge`, `Button`, `Table*`.
- Produces: `<RolesSection />` self-contained; Task 12 adds `<UserRolesCard />` inside it.

- [ ] **Step 1: Implement `RolesSection.tsx`:**

```tsx
import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/lib/api/client"
import {
  deleteRole,
  getRole,
  listPermissions,
  listRoles,
  type Permission,
  type Role,
  type RoleDetail,
} from "@/lib/roles"
import { PermissionsTable } from "./PermissionsTable"
import { RoleDialog } from "./RoleDialog"

/** Roles & Permissions settings section. Mount only behind a `roles:manage`
 *  check — every endpoint underneath is gated server-side too. */
export function RolesSection() {
  const [roles, setRoles] = useState<Role[] | null>(null)
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [error, setError] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<RoleDetail | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setRoles(await listRoles())
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load roles.")
      setRoles([])
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listRoles()
      .then((rows) => {
        if (!cancelled) {
          setRoles(rows)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load roles.")
          setRoles([])
        }
      })
    listPermissions()
      .then((p) => {
        if (!cancelled) setPermissions(p)
      })
      .catch(() => {
        if (!cancelled) setPermissions([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const openCreate = useCallback(() => {
    setEditing(null)
    setDialogOpen(true)
  }, [])

  const openEdit = useCallback(async (role: Role) => {
    try {
      setEditing(await getRole(role.id))
      setDialogOpen(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the role.")
    }
  }, [])

  const handleDelete = useCallback(
    async (role: Role) => {
      if (!window.confirm(`Delete role "${role.name}"? This cannot be undone.`)) return
      setDeletingId(role.id)
      try {
        await deleteRole(role.id)
        await refresh()
      } catch (err) {
        // 409: "N user(s) still hold this role — remove it from them first".
        setError(err instanceof ApiError ? err.message : "Could not delete the role.")
      } finally {
        setDeletingId(null)
      }
    },
    [refresh],
  )

  return (
    <section className="space-y-4">
      <div className="flex items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium">Roles &amp; Permissions</h2>
          <p className="text-sm text-muted-foreground">
            Roles bundle permissions. System roles are managed by the platform and read-only.
          </p>
        </div>
        <Button type="button" onClick={openCreate}>
          Create role
        </Button>
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {roles === null && (
              <TableRow>
                <TableCell colSpan={3} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {roles?.map((role) => (
              <TableRow key={role.id}>
                <TableCell className="font-medium">
                  <span className="flex items-center gap-2">
                    {role.name}
                    {role.is_system && <Badge variant="secondary">System</Badge>}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground">{role.description}</TableCell>
                <TableCell className="text-right">
                  {!role.is_system && (
                    <span className="inline-flex gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => void openEdit(role)}
                      >
                        Edit
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={deletingId === role.id}
                        onClick={() => void handleDelete(role)}
                      >
                        {deletingId === role.id ? "Deleting…" : "Delete"}
                      </Button>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <RoleDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        role={editing}
        permissions={permissions}
        onSaved={refresh}
      />

      <PermissionsTable permissions={permissions} />
    </section>
  )
}
```

- [ ] **Step 2: Mount in `Settings.tsx`** — three edits:

```tsx
import { RolesSection } from "@/components/settings/RolesSection"
```

```tsx
  const canManageRoles = usePermission("roles:manage")
```

```tsx
      {canManageRoles && <RolesSection />}
```

(place the mount line alongside `{canManageApiKeys && <ApiKeysSection />}`).

- [ ] **Step 3: Verify:** `npm run lint && npm run build` — both clean (build runs `tsc -b`).

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/components/settings/RolesSection.tsx vera-frontend/src/pages/Settings.tsx
git commit -m "feat(rbac-ui): Roles & Permissions settings section (list, create, edit, delete)"
```

---

### Task 12: `UserRolesCard` — user-role assignment view

**Files:**
- Create: `vera-frontend/src/components/settings/UserRolesCard.tsx`
- Modify: `vera-frontend/src/components/settings/RolesSection.tsx` (mount it)

**Interfaces:**
- Consumes: `listUsers`, `UserSummary` from `@/lib/auth/api`; `listUserRoles`, `assignRole`, `revokeRole`, `Role` from `@/lib/roles`; `RichSelect*`, `Badge`, `Button`.
- Produces: `<UserRolesCard roles={Role[]} />` — `roles` is the assignable catalog from `RolesSection`.

- [ ] **Step 1: Implement:**

```tsx
import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  RichSelect,
  RichSelectContent,
  RichSelectItem,
  RichSelectTrigger,
  RichSelectValue,
} from "@/components/ui/rich-select"
import { ApiError } from "@/lib/api/client"
import { listUsers, type UserSummary } from "@/lib/auth/api"
import { assignRole, listUserRoles, revokeRole, type Role } from "@/lib/roles"

/** Pick a user, see their roles, add or remove one. Assign/revoke are audited and
 *  cache-invalidated server-side; the server also rejects platform-privileged roles
 *  (403) and revoking your own last roles:manage source (409) — show its message. */
export function UserRolesCard({ roles }: { roles: Role[] }) {
  const [users, setUsers] = useState<UserSummary[]>([])
  const [selectedUserId, setSelectedUserId] = useState("")
  const [userRoles, setUserRoles] = useState<Role[] | null>(null)
  const [addRoleId, setAddRoleId] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listUsers()
      .then((rows) => {
        if (!cancelled) setUsers(rows)
      })
      .catch((err) => {
        // Needs users:read on top of roles:manage; surface the denial plainly.
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Could not load users.")
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refreshUserRoles = useCallback(async (userId: string) => {
    try {
      setUserRoles(await listUserRoles(userId))
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the user's roles.")
      setUserRoles([])
    }
  }, [])

  useEffect(() => {
    setUserRoles(null)
    setAddRoleId("")
    if (selectedUserId) void refreshUserRoles(selectedUserId)
  }, [selectedUserId, refreshUserRoles])

  const handleAssign = useCallback(async () => {
    if (!selectedUserId || !addRoleId) return
    setBusy(true)
    try {
      await assignRole(selectedUserId, addRoleId)
      setAddRoleId("")
      await refreshUserRoles(selectedUserId)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not assign the role.")
    } finally {
      setBusy(false)
    }
  }, [selectedUserId, addRoleId, refreshUserRoles])

  const handleRevoke = useCallback(
    async (role: Role) => {
      if (!selectedUserId) return
      setBusy(true)
      try {
        await revokeRole(selectedUserId, role.id)
        await refreshUserRoles(selectedUserId)
      } catch (err) {
        // 409 here = the self-lockout guard; the server message explains it.
        setError(err instanceof ApiError ? err.message : "Could not remove the role.")
      } finally {
        setBusy(false)
      }
    },
    [selectedUserId, refreshUserRoles],
  )

  const assignable = roles.filter((r) => !userRoles?.some((held) => held.id === r.id))

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-medium">User role assignment</h3>
        <p className="text-sm text-muted-foreground">
          Pick a user to see and change the roles they hold.
        </p>
      </div>

      <RichSelect value={selectedUserId} onValueChange={setSelectedUserId}>
        <RichSelectTrigger className="w-80">
          <RichSelectValue placeholder={users.length ? "Select a user" : "No users"} />
        </RichSelectTrigger>
        <RichSelectContent>
          {users.map((u) => (
            <RichSelectItem key={u.id} value={u.id} caption={u.email}>
              {u.name || u.email}
            </RichSelectItem>
          ))}
        </RichSelectContent>
      </RichSelect>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {selectedUserId && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {userRoles === null && <p className="text-sm text-muted-foreground">Loading…</p>}
            {userRoles?.length === 0 && (
              <p className="text-sm text-muted-foreground">No roles yet.</p>
            )}
            {userRoles?.map((role) => (
              <Badge key={role.id} variant="outline" className="gap-1">
                {role.name}
                <button
                  type="button"
                  aria-label={`Remove ${role.name}`}
                  className="ml-1 text-muted-foreground hover:text-destructive"
                  disabled={busy}
                  onClick={() => void handleRevoke(role)}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <RichSelect value={addRoleId} onValueChange={setAddRoleId}>
              <RichSelectTrigger className="w-72">
                <RichSelectValue placeholder="Add a role…" />
              </RichSelectTrigger>
              <RichSelectContent>
                {assignable.map((r) => (
                  <RichSelectItem key={r.id} value={r.id} caption={r.description}>
                    {r.name}
                  </RichSelectItem>
                ))}
              </RichSelectContent>
            </RichSelect>
            <Button type="button" disabled={busy || !addRoleId} onClick={() => void handleAssign()}>
              Assign
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
```

Note: SUPER_ADMIN appears in the list (it's a visible system role) but the backend rejects assigning it with 403 — the inline error shows the server's message. Filtering it client-side would require knowledge the API deliberately doesn't expose.

- [ ] **Step 2: Mount it** — in `RolesSection.tsx`, add the import and render it after `<PermissionsTable …/>`:

```tsx
import { UserRolesCard } from "./UserRolesCard"
```

```tsx
      <UserRolesCard roles={roles ?? []} />
```

- [ ] **Step 3: Verify:** `npm run lint && npm run test && npm run build` — all clean.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/components/settings/UserRolesCard.tsx vera-frontend/src/components/settings/RolesSection.tsx
git commit -m "feat(rbac-ui): user role assignment view in settings"
```

---

### Task 13: Verify the platform-admin elevation flow (phase 5)

DECISION: no parallel `/platform/...` role APIs. Prove the settled flow — elevate → use the same tenant endpoints → end elevation → access gone — with an integration test in the existing elevation test file (its `world` fixture provides a SUPER_ADMIN token and target tenant).

**Files:**
- Test: `vera-backend/tests/integration/control_plane/test_platform_elevation.py`

**Interfaces:**
- Consumes: existing `world` fixture, `_auth`, `_create`, `_BASE` from that file; the Task 4/5 endpoints.

- [ ] **Step 1: Write the test** (append to the file):

```python
async def test_elevated_operator_manages_roles_like_a_tenant_admin(
    world: tuple[httpx.AsyncClient, World],
) -> None:
    # DECISION (RBAC tickets): platform admins get NO parallel role API — they
    # elevate, then drive the same tenant endpoints under that tenant's RLS.
    client, w = world

    before = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert before.status_code == 403  # no active elevation → no tenant access

    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    created = await client.post(
        "/api/v1/roles",
        headers=_auth(w.super_token),
        json={"name": "ELEVATED_MADE", "description": "made under elevation",
              "permission_ids": []},
    )
    assert created.status_code == 200, created.text
    role_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(w.super_token),
        json={"description": "edited under elevation"},
    )
    assert patched.status_code == 200, patched.text

    perms = await client.get("/api/v1/permissions", headers=_auth(w.super_token))
    assert perms.status_code == 200
    assert not any(p["code"].startswith("platform:") for p in perms.json()["data"])

    deleted = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers=_auth(w.super_token)
    )
    assert deleted.status_code == 200, deleted.text

    ended = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert ended.status_code == 200
    after = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert after.status_code == 403  # elevation over → access gone again
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/integration/control_plane/test_platform_elevation.py -k "elevated_operator_manages_roles" -v`
Expected: PASS with the endpoints from Tasks 1–5 in place (this task has no implementation step — a failure means a real integration bug; fix it before proceeding).

- [ ] **Step 3: Frontend affordance check (manual, no code expected):** SUPER_ADMIN carries every permission, so an elevated operator's `/auth/me` already includes `roles:manage` and the Settings section renders. Confirm during Task 14's walkthrough; only if the section is unreachable for an elevated operator does UI work exist (out of scope here — file it).

- [ ] **Step 4: Commit**

```bash
git add vera-backend/tests/integration/control_plane/test_platform_elevation.py
git commit -m "test(rbac): elevation flow drives tenant role management end to end"
```

---

### Task 14: Final gates — simplify, full checks, manual walkthrough

- [ ] **Step 1: Run the code-simplifier** (mandatory per repo `CLAUDE.md`): trigger **"simplify code"** on the changed backend + frontend files, same session as the implementation.

- [ ] **Step 2: Backend gate:** in `vera-backend/`: `just check` (with `just up && just migrate` done). Expected: ruff clean, mypy --strict clean, pytest all green.

- [ ] **Step 3: Frontend gate:** in `vera-frontend/`: `npm run lint && npm run test && npm run build`. Expected: all clean.

- [ ] **Step 4: Manual walkthrough:** backend `just api` + frontend `npm run dev`; log in as a `roles:manage` user (seeded tenant admin), open Settings → Roles & Permissions, and verify: catalog shows no `platform:*` codes; create → edit → delete a role (delete blocked with the holder-count message while assigned); assign/remove roles on a user; removing your own only TENANT_ADMIN role shows the self-lockout message.

- [ ] **Step 5: Commit any simplifier refinements**

```bash
git add -A
git commit -m "refactor(rbac): post-implementation simplification pass"
```

---

## Self-review notes (spec ↔ plan)

- Spec §1 (read-only permission catalog, platform filtered, 403 without `roles:manage`) → Task 1. No CRUD anywhere — none planned.
- Spec §2 (GET detail / PATCH / DELETE, description on create, system-role guard, platform-perm guard on PATCH, holder cache invalidation, delete-409 with count, dup-name 409, unknown-perm 400, ROLE_UPDATED/ROLE_DELETED audit) → Tasks 2–5. The AuthEvent CHECK migration (Task 4 Step 2) is an addition the spec missed — required by `auth.py`'s `check_in("event_type", AuthEvent)`.
- Spec §3 (GET user roles, assignment UI, elevation reuse, self-lockout 409 self-only) → Tasks 6, 7, 12, 13.
- Spec §4 universal rules → Global Constraints; idempotency note: `POST /roles` keeps the existing durable de-dup via the `(tenant_id, name)` UNIQUE constraint (same as today); PATCH (set-replacement) and DELETE are naturally idempotent, so no `Idempotency-Key` gate was added — matching the current `roles.py` endpoints rather than the invite endpoint.
- Spec §5 phase order preserved: reads (1–2) → mutations (3–6) → user-roles read (7) → UI (8–12) → elevation verify (13).
