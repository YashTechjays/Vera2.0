# RBAC Backend Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the missing RBAC backend API — a read-only permission catalog, role detail/update/delete, per-user role listing, the self-lockout guard — plus the audit-enum migration and an end-to-end test of the platform-admin elevation flow.

**Architecture:** Everything extends the existing tenant-admin router pattern in `apps/control_plane/src/control_plane/api/v1/` (verified identity → `require("roles:manage")` → `TenantSession` under RLS → audit via `emit_auth_event`). No new tables, no new auth code. One migration widens the `auth_audit_log.event_type` CHECK for two new `AuthEvent` values. The platform-admin path reuses break-glass elevation — **no** parallel `/platform/...` assignment API (spec decision 3).

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + pytest (integration tests run against live Postgres with RLS enforced). All commands run from `vera-backend/`.

**Scope:** Backend only. The Settings-page UI (spec phase 4) is a separate frontend plan.

**Spec:** `docs/rbac-tasks-explained.md` (decisions there are settled — build to them).

## Global Constraints

- Every endpoint returns `ResponseModel[T]` via `ok(...)`; errors are `CustomAPIException` subclasses — never a bare dict, never `HTTPException`.
- Every query runs through the tenant-scoped session (`TenantSession`). Never raw SQL that skips tenant context.
- Every change that affects anyone's effective permissions calls `resolver.invalidate(tenant_id, user_id)` for **every affected user**.
- Every mutation emits `emit_auth_event(...)`; `meta` carries ids/names only (never PHI).
- `account_type` is the only way to tell platform from tenant users — never branch on `tenant_id IS NULL`.
- A `platform:*` permission must never enter a tenant role or reach a tenant caller as an option.
- Migrations via `just makemigration "<message>"` (random hex revision id) — never hand-numbered. RLS policies live in `migrations/`, not models.
- Definition of done: `just check` passes (ruff + mypy --strict + pytest). Integration tests need `just up && just migrate` first.
- Idempotency note (settled here): `PATCH` (replaces state) and `DELETE` are naturally idempotent, and durable de-dup for role creation is the existing `UNIQUE (tenant_id, name)` constraint — matching the existing `POST /roles`, none of the role endpoints take an `Idempotency-Key` header. Do not add `require_idempotency_key` to them.
- Repo rule: after all tasks, run the **code-simplifier** plugin on the change, then re-run `just check`, before calling the work done (see `Vera2.0/CLAUDE.md`).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `apps/control_plane/src/control_plane/api/v1/permissions.py` | **Create** | `GET /permissions` — read-only tenant-tier catalog + `PermissionResponse` model (shared with roles detail) |
| `apps/control_plane/src/control_plane/api/v1/__init__.py` | Modify | Register the permissions router |
| `apps/control_plane/src/control_plane/api/v1/roles.py` | Modify | Add `GET /roles/{id}`, `PATCH /roles/{id}`, `DELETE /roles/{id}`, `GET /users/{id}/roles`, description on create, self-lockout guard in `revoke_role` |
| `packages/vera_core/src/vera_core/models/enums.py` | Modify | Add `AuthEvent.ROLE_UPDATED`, `AuthEvent.ROLE_DELETED` |
| `migrations/versions/<generated>.py` | **Create** (via `just makemigration`) | Widen the `auth_audit_log.event_type` CHECK (pattern: `0017_persona_tweak_event.py`) |
| `tests/integration/control_plane/test_roles_admin.py` | **Create** | All new integration tests (reuses `client`/`rbac_world`/`admin_sessionmaker`/`session_store` fixtures from `conftest.py`) |
| `tests/integration/control_plane/test_platform_elevation.py` | Modify | End-to-end elevated role-management test (spec phase 5) |

Verified context you'd otherwise have to dig for:

- `roles.py` already has `_role_visible`, `_user_in_tenant`, `_conflict_or_raise`, `_to_response` helpers and imports `is_platform_permission`, `emit_auth_event`, `Resolver` from `api/v1/common.py`.
- `auth_audit_log.event_type` is CHECK-constrained to the `AuthEvent` values (`vera_core/models/auth.py:180`, constraint name `ck_auth_audit_log_event_type_valid`) — new enum values **reject at the DB** until the CHECK is widened. Migration `0017` is the exact precedent.
- `resolver.invalidate(tenant_id: UUID | None, user_id: UUID)` (`auth/rbac.py:84`).
- `RolePermission` / `UserRole` have `ondelete="CASCADE"` FKs to `role`, so deleting a role cascades its permission links at the DB layer.
- The catalog RLS on `role`/`role_permission` lets a tenant session READ global rows (system roles + their grants) but WRITE only its own; `user_role` is strict-tenant.
- Test world (`tests/integration/control_plane/conftest.py`): `rbac_world.admin_token` holds TENANT_ADMIN (has `roles:manage`, `users:read`, …), `rbac_world.norole_token` holds nothing. `session_store` (the shared `InMemorySessionStore`) and `_mint(...)` are importable, so tests can create extra active users and mint tokens for them. `admin_sessionmaker` is a superuser session for seeding/asserting rows directly.
- SUPER_ADMIN (system role) carries **all** permissions incl. `platform:*` and `roles:manage` (`rbac_defaults.py`) — that is why the elevated platform operator passes `require("roles:manage")` on tenant routes, and why role-detail must filter `platform:*` codes.
- `just test <args>` = `uv run pytest <args>`. Integration tests skip unless `just up && just migrate` was run.

---

### Task 1: `GET /permissions` — read-only tenant-tier catalog

**Files:**
- Create: `apps/control_plane/src/control_plane/api/v1/permissions.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/__init__.py`
- Test: `tests/integration/control_plane/test_roles_admin.py` (new file)

**Interfaces:**
- Consumes: `TenantId`, `TenantSession`, `is_platform_permission` from `api/v1/common.py`; `require` from `auth/rbac.py`.
- Produces: `PermissionResponse(id: UUID, code: str, description: str)` — imported by `roles.py` in Task 2; route `GET /api/v1/permissions` → `ResponseModel[list[PermissionResponse]]`.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/control_plane/test_roles_admin.py`:

```python
"""Integration tests for the RBAC settings surface (spec: docs/rbac-tasks-explained.md):
permission catalog, role detail/update/delete, per-user role listing, and the
self-lockout guard — over a live RLS-enforcing connection (same world as test_admin.py)."""

from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.session import InMemorySessionStore
from tests.integration.control_plane.conftest import RBACWorld, _mint
from vera_core.models import AppUser, UserRole


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _permission_ids(client: httpx.AsyncClient, token: str, *codes: str) -> list[str]:
    resp = await client.get("/api/v1/permissions", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    by_code = {p["code"]: p["id"] for p in resp.json()["data"]}
    return [by_code[c] for c in codes]


# --- GET /permissions --------------------------------------------------------


async def test_permissions_catalog_hides_platform_codes(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    codes = {r["code"] for r in rows}
    # The tenant-tier catalog is present, with descriptions...
    assert {"calls:read", "roles:manage", "users:manage"} <= codes
    assert all(r["description"] for r in rows)
    # ...and no platform-tier code ever reaches a tenant caller.
    assert not any(c.startswith("platform:") for c in codes)


async def test_permissions_requires_roles_manage(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: both FAIL with `assert 404 == 200` / `assert 404 == 403` (route does not exist).
(If tests SKIP, run `just up && just migrate` first.)

- [ ] **Step 3: Implement** — create `apps/control_plane/src/control_plane/api/v1/permissions.py`:

```python
"""Permission catalog (RBAC tickets, spec §1) — a read-only listing of the
tenant-tier permission codes, consumed by the role create/edit UI.

Permissions are defined in code and seeded from `rbac_defaults.py`; there is no
tenant CRUD on this catalog (a user-created permission would never be checked by
any route, and deleting one would break the guards that reference it). A new
permission is a code change plus a seed migration. `platform:*` codes are
platform-operator-only and are never surfaced to a tenant caller.
"""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from control_plane.api.v1.common import TenantId, TenantSession, is_platform_permission
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.models import Permission

router = APIRouter(tags=["permissions"])


class PermissionResponse(BaseModel):
    id: UUID
    code: str
    description: str


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
    rows = (await session.execute(select(Permission).order_by(Permission.code))).scalars().all()
    return ok(
        [
            PermissionResponse(id=p.id, code=p.code, description=p.description)
            for p in rows
            if not is_platform_permission(p.code)
        ]
    )
```

Register it in `apps/control_plane/src/control_plane/api/v1/__init__.py` (alphabetical with the others):

```python
from control_plane.api.v1.permissions import router as permissions_router
```

and after `router.include_router(roles_router)`:

```python
router.include_router(permissions_router)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/permissions.py \
        apps/control_plane/src/control_plane/api/v1/__init__.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): read-only GET /permissions catalog (tenant-tier codes only)"
```

---

### Task 2: `GET /roles/{role_id}` — role detail with its permissions

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Consumes: `PermissionResponse` from Task 1 (`from control_plane.api.v1.permissions import PermissionResponse` — no import cycle: `permissions.py` only imports from `common.py`).
- Produces: `RoleDetailResponse(RoleResponse)` with `permissions: list[PermissionResponse]`; route `GET /api/v1/roles/{role_id}` → `ResponseModel[RoleDetailResponse]`. The PATCH tests in Task 5 read back through this endpoint.

Decision baked in: a system role's detail also filters `platform:*` codes — SUPER_ADMIN is visible in the catalog, but its platform-tier grants are never a tenant's business (consistent with `GET /permissions`).

- [ ] **Step 1: Write the failing tests** — append to `test_roles_admin.py`:

```python
# --- GET /roles/{role_id} ----------------------------------------------------


async def _create_role(
    client: httpx.AsyncClient,
    token: str,
    name: str,
    permission_ids: list[str] | None = None,
    description: str = "",
) -> str:
    resp = await client.post(
        "/api/v1/roles",
        headers=_auth(token),
        json={
            "name": name,
            "description": description,
            "permission_ids": permission_ids or [],
        },
    )
    assert resp.status_code == 200, resp.text
    role_id: str = resp.json()["data"]["id"]
    return role_id


async def _role_id_by_name(client: httpx.AsyncClient, token: str, name: str) -> str:
    resp = await client.get("/api/v1/roles", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    role_id: str = next(r["id"] for r in resp.json()["data"] if r["name"] == name)
    return role_id


async def test_role_detail_includes_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    perm_ids = await _permission_ids(client, rbac_world.admin_token, "calls:read", "forms:read")
    role_id = await _create_role(client, rbac_world.admin_token, "detail-role", perm_ids)

    resp = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_system"] is False
    assert {p["code"] for p in data["permissions"]} == {"calls:read", "forms:read"}


async def test_role_detail_unknown_id_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    from vera_core.db import uuid7

    resp = await client.get(f"/api/v1/roles/{uuid7()}", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 404


async def test_system_role_detail_hides_platform_codes(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    super_admin_id = await _role_id_by_name(client, rbac_world.admin_token, "SUPER_ADMIN")
    resp = await client.get(
        f"/api/v1/roles/{super_admin_id}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_system"] is True
    codes = {p["code"] for p in data["permissions"]}
    # SUPER_ADMIN holds every permission, but only tenant-tier codes are shown.
    assert "roles:manage" in codes
    assert not any(c.startswith("platform:") for c in codes)
```

(Move the `from vera_core.db import uuid7` import to the top of the file with the others.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k role_detail or system_role_detail`
Expected: FAIL — `_create_role` asserts on `description` acceptance? No: description lands in Task 4; here creation succeeds (extra JSON field is ignored by pydantic default config only if not forbidden — if `CreateRoleRequest` rejects the extra `description` key with 422, temporarily pass `description=""` only in Task 4's test and drop the field here: `json={"name": name, "permission_ids": permission_ids or []}`; re-add `description` to the helper in Task 4). The detail GETs themselves must fail 404/405 (route missing).

- [ ] **Step 3: Implement** — in `roles.py`, add the import and response model near `RoleResponse`:

```python
from control_plane.api.v1.permissions import PermissionResponse
```

```python
class RoleDetailResponse(RoleResponse):
    permissions: list[PermissionResponse]
```

Add the route (after `list_roles`):

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
    if role is None:
        raise NotFoundError(message="no such role")
    permissions = (
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
    # Platform-tier codes are never surfaced to a tenant caller — SUPER_ADMIN is
    # visible in the catalog, but its platform grants are not tenant business.
    return ok(
        RoleDetailResponse(
            id=role.id,
            name=role.name,
            description=role.description,
            is_system=role.tenant_id is None,
            permissions=[
                PermissionResponse(id=p.id, code=p.code, description=p.description)
                for p in permissions
                if not is_platform_permission(p.code)
            ],
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): GET /roles/{id} detail with tenant-tier permissions"
```

---

### Task 3: `GET /users/{user_id}/roles` — read a user's roles

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py` (this path's siblings `POST /users/{id}/roles` / `DELETE .../roles/{id}` already live here)
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Consumes: existing `_user_in_tenant`, `_to_response` helpers in `roles.py`.
- Produces: route `GET /api/v1/users/{user_id}/roles` → `ResponseModel[list[RoleResponse]]`. Gated by `roles:manage` (it feeds the role-assignment view, which sits behind that permission).

Decision baked in (spec offers either): a dedicated endpoint, not a `roles` field on `GET /users` — keeps the user list response minimal (minimum-necessary) and avoids an N+1 on the list.

- [ ] **Step 1: Write the failing tests** — append to `test_roles_admin.py`:

```python
# --- GET /users/{user_id}/roles ----------------------------------------------


async def test_list_user_roles(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    # The admin persona holds exactly TENANT_ADMIN (see conftest).
    me = await client.get("/api/v1/auth/me", headers=_auth(rbac_world.admin_token))
    admin_id = me.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/users/{admin_id}/roles", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()["data"]}
    assert "TENANT_ADMIN" in names


async def test_list_user_roles_unknown_user_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        f"/api/v1/users/{uuid7()}/roles", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404


async def test_list_user_roles_requires_roles_manage(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    me = await client.get("/api/v1/auth/me", headers=_auth(rbac_world.norole_token))
    own_id = me.json()["data"]["id"]
    resp = await client.get(
        f"/api/v1/users/{own_id}/roles", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403
```

Note: if `/auth/me` does not return `id`, look up the user id via `admin_sessionmaker` instead: `SELECT id FROM app_user WHERE email = 'admin@test.example'` — check `auth.py`'s `/me` response first and adjust.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k list_user_roles`
Expected: FAIL with 404/405 responses on the roles path (route missing). (The 404-case test may pass trivially — confirm the other two fail.)

- [ ] **Step 3: Implement** — in `roles.py`, after `revoke_role`:

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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): GET /users/{id}/roles for the assignment view"
```

---

### Task 4: `AuthEvent.ROLE_UPDATED` / `ROLE_DELETED` + CHECK-widening migration, and description on `POST /roles`

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py`
- Create: `migrations/versions/<generated>.py` (via `just makemigration`)
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py` (CreateRoleRequest description)
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Produces: `AuthEvent.ROLE_UPDATED = "role_updated"`, `AuthEvent.ROLE_DELETED = "role_deleted"` — consumed by Tasks 5 and 6; `CreateRoleRequest.description: str` persisted on create.

Why the migration: `auth_audit_log.event_type` has a CHECK built from the enum (`ck_auth_audit_log_event_type_valid`, `vera_core/models/auth.py:180`). On an already-provisioned DB the insert of a new value is **rejected** until the CHECK is recreated. Migration `0017_persona_tweak_event.py` is the exact pattern — copy it.

- [ ] **Step 1: Write the failing test** — append to `test_roles_admin.py`:

```python
# --- POST /roles description + new audit event values ------------------------


async def test_create_role_with_description(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    role_id = await _create_role(
        client, rbac_world.admin_token, "desc-role", description="handles intake calls"
    )
    resp = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert resp.json()["data"]["description"] == "handles intake calls"


async def test_auth_audit_accepts_new_role_events(
    rbac_world: RBACWorld, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The CHECK on auth_audit_log.event_type must admit the two new values —
    # this fails on an un-migrated DB and passes after the widen migration.
    async with admin_sessionmaker() as s, s.begin():
        for event in ("role_updated", "role_deleted"):
            await s.execute(
                text(
                    "INSERT INTO auth_audit_log (tenant_id, event_type, meta)"
                    " VALUES (:t, :e, '{}'::jsonb)"
                ).bindparams(t=rbac_world.tenant_id, e=event)
            )
        await s.execute(
            text(
                "DELETE FROM auth_audit_log WHERE tenant_id = :t"
                " AND event_type IN ('role_updated', 'role_deleted')"
            ).bindparams(t=rbac_world.tenant_id)
        )
```

Note: check `auth_audit_log`'s NOT NULL columns before finalizing the INSERT (`packages/vera_core/src/vera_core/models/auth.py` around line 180 — e.g. hash-chain columns from migration 0012 may be trigger-filled or need values); adjust the column list so the only intended failure mode is the CHECK.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k description or audit_accepts`
Expected: `test_create_role_with_description` FAILS (description not persisted — today `create_role` hardcodes `description=""`); `test_auth_audit_accepts_new_role_events` FAILS with a CHECK-violation `IntegrityError`.

- [ ] **Step 3: Implement the enum + request field**

In `enums.py`, inside `AuthEvent`:

```python
    ROLE_CREATED = "role_created"
    ROLE_UPDATED = "role_updated"
    ROLE_DELETED = "role_deleted"
    ROLE_GRANT = "role_grant"
    ROLE_REVOKE = "role_revoke"
```

In `roles.py`:

```python
class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    permission_ids: list[UUID] = Field(default_factory=list)
```

and in `create_role`, replace `description=""`:

```python
    role = Role(tenant_id=tenant_id, name=body.name, description=body.description)
```

(If Task 2's `_create_role` helper dropped the `description` key, restore it now.)

- [ ] **Step 4: Generate and edit the migration**

```bash
just makemigration "widen auth_audit event_type for role update and delete"
```

Autogenerate does not detect CHECK-constraint changes, so the generated `upgrade()`/`downgrade()` will be empty (delete any spurious autogenerated ops). Keep the generated `revision`/`down_revision` (down_revision should be the current head, `1e6c84132026`) and replace the body following `0017_persona_tweak_event.py`:

```python
from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The set before this migration — the current enum minus role_updated/role_deleted.
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


def _check(values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"CHECK (event_type IN ({quoted}))"


def _recreate(values: Sequence[str]) -> None:
    op.execute(f"ALTER TABLE auth_audit_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE auth_audit_log ADD CONSTRAINT {_CONSTRAINT} {_check(values)}")


def upgrade() -> None:
    _recreate(values_of(AuthEvent))


def downgrade() -> None:
    _recreate(_OLD_VALUES)
```

Then apply it:

```bash
just migrate
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add packages/vera_core/src/vera_core/models/enums.py migrations/versions/ \
        apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): ROLE_UPDATED/ROLE_DELETED audit events + role description on create"
```

---

### Task 5: `PATCH /roles/{role_id}` — edit a custom role, invalidate holders

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Consumes: `AuthEvent.ROLE_UPDATED` (Task 4), `RoleDetailResponse` read-back (Task 2), `resolver.invalidate(tenant_id, user_id)`.
- Produces: route `PATCH /api/v1/roles/{role_id}` with body `UpdateRoleRequest(name?: str, description?: str, permission_ids?: list[UUID])` → `ResponseModel[RoleResponse]`. Omitted fields are unchanged; `permission_ids`, when present, **replaces** the whole set.

Guards (each is a test): system role → 403; unknown permission id → 400; `platform:*` permission → 403 (same rule as `create_role` at `roles.py:114`); duplicate name → 409 via `_conflict_or_raise`; permission change → invalidate **every** holder's cache.

- [ ] **Step 1: Write the failing tests** — append to `test_roles_admin.py`. Also add the module-level helper used here and in later tasks (needs `session_store` — the same store `rbac_world` minted into):

```python
# --- PATCH /roles/{role_id} ---------------------------------------------------


async def _make_active_user(
    sm: async_sessionmaker[AsyncSession],
    store: InMemorySessionStore,
    tenant_id: UUID,
    email: str,
    role_ids: list[str],
) -> tuple[UUID, str]:
    """Create an ACTIVE tenant user holding `role_ids` and mint a session for them.

    The permission resolver only resolves active users, so invited-status users
    can't exercise permission checks. Cleaned up by rbac_world's per-tenant
    teardown (app_user/user_role are deleted by tenant_id)."""
    async with sm() as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=email,
            name="RBAC test user",
            status="active",
            account_type="tenant",
        )
        session.add(user)
        await session.flush()
        for rid in role_ids:
            session.add(
                UserRole(tenant_id=tenant_id, app_user_id=user.id, role_id=UUID(rid))
            )
        user_id = user.id
    token = await _mint(store, user_id=user_id, tenant_id=tenant_id, email=email)
    return user_id, token


async def test_patch_renames_and_replaces_permissions(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old = await _permission_ids(client, rbac_world.admin_token, "calls:read")
    new = await _permission_ids(client, rbac_world.admin_token, "forms:read", "forms:write")
    role_id = await _create_role(client, rbac_world.admin_token, "patch-role", old)

    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "patched-role", "description": "now forms", "permission_ids": new},
    )
    assert resp.status_code == 200, resp.text

    detail = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    data = detail.json()["data"]
    assert data["name"] == "patched-role"
    assert data["description"] == "now forms"
    assert {p["code"] for p in data["permissions"]} == {"forms:read", "forms:write"}

    # The mutation is audited with ids/names only.
    async with admin_sessionmaker() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM auth_audit_log WHERE event_type = 'role_updated'"
                    " AND meta->>'role_id' = :r"
                ).bindparams(r=role_id)
            )
        ).scalar_one()
    assert count >= 1


async def test_patch_system_role_forbidden(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    system_id = await _role_id_by_name(client, rbac_world.admin_token, "TENANT_ADMIN")
    resp = await client.patch(
        f"/api/v1/roles/{system_id}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "hijacked"},
    )
    assert resp.status_code == 403


async def test_patch_unknown_permission_400(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    role_id = await _create_role(client, rbac_world.admin_token, "patch-unknown-perm")
    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": [str(uuid7())]},
    )
    assert resp.status_code == 400


async def test_patch_platform_permission_forbidden(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # platform:* ids are filtered out of GET /permissions, so fetch one directly.
    async with admin_sessionmaker() as s:
        platform_perm = (
            await s.execute(
                text("SELECT id FROM permission WHERE code = 'platform:elevations:create'")
            )
        ).scalar_one()
    role_id = await _create_role(client, rbac_world.admin_token, "patch-platform-perm")
    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": [str(platform_perm)]},
    )
    assert resp.status_code == 403


async def test_patch_duplicate_name_conflict(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    await _create_role(client, rbac_world.admin_token, "dup-a")
    role_b = await _create_role(client, rbac_world.admin_token, "dup-b")
    resp = await client.patch(
        f"/api/v1/roles/{role_b}",
        headers=_auth(rbac_world.admin_token),
        json={"name": "dup-a"},
    )
    assert resp.status_code == 409


async def test_patch_invalidates_holder_permission_cache(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    # End to end: a live holder's effective permissions change the moment the
    # role is patched — the resolver cache must not serve the stale set.
    perm = await _permission_ids(client, rbac_world.admin_token, "users:read")
    role_id = await _create_role(client, rbac_world.admin_token, "readers", perm)
    _user_id, token = await _make_active_user(
        admin_sessionmaker,
        session_store,
        rbac_world.tenant_id,
        "cache-probe@test.example",
        [role_id],
    )

    before = await client.get("/api/v1/users", headers=_auth(token))
    assert before.status_code == 200, before.text  # users:read via the role (and caches it)

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
        json={"permission_ids": []},
    )
    assert patched.status_code == 200, patched.text

    after = await client.get("/api/v1/users", headers=_auth(token))
    assert after.status_code == 403  # stale cache would still return 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k patch`
Expected: all FAIL with 405 (no PATCH route).

- [ ] **Step 3: Implement** — in `roles.py`:

Add to the imports: `from sqlalchemy import delete, select` (replacing the bare `select` import).

Add the request model near the others:

```python
class UpdateRoleRequest(BaseModel):
    """Partial update: omitted fields are unchanged; `permission_ids`, when
    present, REPLACES the role's whole permission set."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    permission_ids: list[UUID] | None = None
```

Add the route (after `get_role`):

```python
@router.patch(
    "/roles/{role_id}",
    response_model=ResponseModel[RoleResponse],
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
) -> ResponseModel[RoleResponse]:
    role = (
        await session.execute(select(Role).where(Role.id == role_id))
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(message="no such role")
    # System roles (tenant_id IS NULL) are read-only for a tenant. RLS would block
    # the write anyway, but a silent 0-row update is not an answer — reject cleanly.
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
        # Same rule as create_role: a tenant role never carries a platform-tier
        # permission.
        if any(is_platform_permission(p.code) for p in permissions):
            raise CustomAPIException(
                DefaultExceptionCode.FORBIDDEN,
                message="cannot grant a platform-tier permission to a tenant role",
            )
        await session.execute(
            delete(RolePermission).where(RolePermission.role_id == role.id)
        )
        for permission in permissions:
            session.add(
                RolePermission(
                    tenant_id=tenant_id, role_id=role.id, permission_id=permission.id
                )
            )
        changed.append("permissions")

    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict_or_raise(exc, "a role with that name already exists") from exc

    if "permissions" in changed:
        # Every live holder keeps serving from the effective-permission cache
        # until it is invalidated — one call per holder.
        holder_ids = (
            (
                await session.execute(
                    select(UserRole.app_user_id).where(UserRole.role_id == role.id)
                )
            )
            .scalars()
            .all()
        )
        for holder_id in holder_ids:
            await resolver.invalidate(tenant_id, holder_id)

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_UPDATED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"role_id": str(role.id), "name": role.name, "changed": changed},
    )
    return ok(_to_response(role))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS. The cache-invalidation test is the one most likely to surprise — if `after` returns 200, the resolver was not invalidated for the holder; if `before` returns 403, the probe user was not active or the role grant did not land.

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): PATCH /roles/{id} with guards and holder cache invalidation"
```

---

### Task 6: `DELETE /roles/{role_id}` — blocked while held (409 + holder count)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py`
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Consumes: `AuthEvent.ROLE_DELETED` (Task 4), `ConflictError` from `control_plane.exceptions`, `func` (already imported in `roles.py` as `from sqlalchemy.sql import func`).
- Produces: route `DELETE /api/v1/roles/{role_id}` → `ResponseModel[None]`. While users hold the role: 409 whose `message` contains the holder count (the UI shows it). No cascade — the admin revokes per user first through the existing audited, cache-invalidating revoke endpoint, so DELETE itself needs no bulk invalidation.

- [ ] **Step 1: Write the failing tests** — append to `test_roles_admin.py`:

```python
# --- DELETE /roles/{role_id} --------------------------------------------------


async def test_delete_role_happy_path(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    role_id = await _create_role(client, rbac_world.admin_token, "delete-me")
    resp = await client.delete(
        f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text

    gone = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert gone.status_code == 404

    async with admin_sessionmaker() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM auth_audit_log WHERE event_type = 'role_deleted'"
                    " AND meta->>'role_id' = :r"
                ).bindparams(r=role_id)
            )
        ).scalar_one()
    assert count == 1


async def test_delete_held_role_conflict_with_count(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    role_id = await _create_role(client, rbac_world.admin_token, "held-role")
    holder_id, _token = await _make_active_user(
        admin_sessionmaker,
        session_store,
        rbac_world.tenant_id,
        "holder@test.example",
        [role_id],
    )

    blocked = await client.delete(
        f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert blocked.status_code == 409
    assert "1" in blocked.json()["message"]  # holder count surfaces to the UI

    # Revoke per user (the audited path), then delete succeeds.
    revoked = await client.delete(
        f"/api/v1/users/{holder_id}/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert revoked.status_code == 200, revoked.text
    deleted = await client.delete(
        f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token)
    )
    assert deleted.status_code == 200, deleted.text


async def test_delete_system_role_forbidden(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    system_id = await _role_id_by_name(client, rbac_world.admin_token, "SUPERVISOR")
    resp = await client.delete(
        f"/api/v1/roles/{system_id}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 403


async def test_delete_unknown_role_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.delete(
        f"/api/v1/roles/{uuid7()}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k delete`
Expected: FAIL with 405 (no DELETE route on `/roles/{id}`) — except `test_delete_unknown_role_404`, which fails with `assert 405 == 404`.

- [ ] **Step 3: Implement** — in `roles.py`, add `ConflictError` to the `control_plane.exceptions` import, then after `update_role`:

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
    # No silent cascade: while users hold the role, deleting it would strip their
    # access with no per-user trail. Revoke per user first (each revoke is audited
    # and cache-invalidating), so by the time delete runs there is nothing to
    # invalidate.
    holders = (
        await session.execute(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
        )
    ).scalar_one()
    if holders:
        raise ConflictError(
            message=f"{holders} user(s) still hold this role — remove it from them first"
        )

    name = role.name
    await session.delete(role)  # role_permission rows follow via FK ON DELETE CASCADE
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_DELETED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"role_id": str(role_id), "name": name},
    )
    return ok(None, message="Role deleted.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): DELETE /roles/{id} — 409 with holder count while held"
```

---

### Task 7: Self-lockout guard in `revoke_role`

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/roles.py` (`revoke_role`, `roles.py:214-242`)
- Test: `tests/integration/control_plane/test_roles_admin.py`

**Interfaces:**
- Consumes: existing `revoke_role` body; `Permission`/`RolePermission`/`UserRole` models; `ConflictError`.
- Produces: `DELETE /users/{user_id}/roles/{role_id}` now returns 409 when the caller revokes **their own** last source of the `roles:manage` **permission** (not "a specific role" — they may hold it via two roles, and removing one of those is fine). Self-only by design: no cross-user "last admin in the tenant" check (an admin removing another admin is an explicit audited act; break-glass elevation is the recovery path).

- [ ] **Step 1: Write the failing tests** — append to `test_roles_admin.py`:

```python
# --- self-lockout guard in revoke_role ----------------------------------------


async def test_revoke_own_last_roles_manage_source_conflict(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    manage = await _permission_ids(client, rbac_world.admin_token, "roles:manage")
    only_role = await _create_role(client, rbac_world.admin_token, "lockout-only", manage)
    user_id, token = await _make_active_user(
        admin_sessionmaker,
        session_store,
        rbac_world.tenant_id,
        "lockout@test.example",
        [only_role],
    )

    resp = await client.delete(
        f"/api/v1/users/{user_id}/roles/{only_role}", headers=_auth(token)
    )
    assert resp.status_code == 409
    assert "role-management" in resp.json()["message"]

    # The grant is untouched — the user can still manage roles.
    still = await client.get(f"/api/v1/users/{user_id}/roles", headers=_auth(token))
    assert still.status_code == 200


async def test_revoke_own_role_ok_with_second_manage_source(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    manage = await _permission_ids(client, rbac_world.admin_token, "roles:manage")
    role_a = await _create_role(client, rbac_world.admin_token, "manage-a", manage)
    role_b = await _create_role(client, rbac_world.admin_token, "manage-b", manage)
    user_id, token = await _make_active_user(
        admin_sessionmaker,
        session_store,
        rbac_world.tenant_id,
        "two-sources@test.example",
        [role_a, role_b],
    )

    # roles:manage survives via role_b, so removing role_a is allowed.
    resp = await client.delete(f"/api/v1/users/{user_id}/roles/{role_a}", headers=_auth(token))
    assert resp.status_code == 200, resp.text


async def test_admin_can_revoke_another_admins_manage_role(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    # The guard is self-only: one admin removing ANOTHER admin's role is an
    # explicit, audited act (elevation is the recovery path).
    manage = await _permission_ids(client, rbac_world.admin_token, "roles:manage")
    role = await _create_role(client, rbac_world.admin_token, "other-admin-role", manage)
    user_id, _token = await _make_active_user(
        admin_sessionmaker,
        session_store,
        rbac_world.tenant_id,
        "other-admin@test.example",
        [role],
    )

    resp = await client.delete(
        f"/api/v1/users/{user_id}/roles/{role}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v -k lockout or manage_source or another_admins`
Expected: `test_revoke_own_last_roles_manage_source_conflict` FAILS (`assert 200 == 409` — today the self-revoke succeeds). The other two PASS already (they pin current behavior so the guard can't over-block).

- [ ] **Step 3: Implement** — in `revoke_role` (`roles.py`), after the `assignment is None` check and **before** `await session.delete(assignment)`:

```python
    # Self-lockout guard: the caller may not remove their own LAST source of the
    # `roles:manage` permission — nobody in the tenant could manage roles anymore
    # (recovery would be a platform operator's break-glass elevation). Keyed on
    # the permission, not the role: a second role carrying `roles:manage` keeps
    # this removal legal. Self-only — removing another admin is an explicit,
    # audited act.
    if user_id == _caller.user_id:
        still_manages = (
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
        if still_manages is None:
            raise ConflictError(
                message="you cannot remove your own last role-management role"
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `just test tests/integration/control_plane/test_roles_admin.py -v`
Expected: all PASS. Also run the existing suite to confirm the guard doesn't break normal revokes: `just test tests/integration/control_plane/test_admin.py -v` — all PASS (`test_assign_and_revoke_role` revokes a *different* user, unaffected).

- [ ] **Step 5: Lint/typecheck, then commit**

```bash
just lint && just typecheck
git add apps/control_plane/src/control_plane/api/v1/roles.py \
        tests/integration/control_plane/test_roles_admin.py
git commit -m "feat(rbac): self-lockout guard — cannot revoke own last roles:manage source"
```

---

### Task 8: Verify the platform-admin flow end to end (spec phase 5 — test only, no new API)

**Files:**
- Modify: `tests/integration/control_plane/test_platform_elevation.py`

**Interfaces:**
- Consumes: the `world` fixture and `_create`/`_auth`/`_BASE` helpers already in that file; the endpoints from Tasks 1–6. SUPER_ADMIN carries `roles:manage` (it holds every permission), so the elevated operator passes the tenant route guards.
- Produces: proof that elevate → manage roles with the **same** tenant endpoints → end elevation works, and that access drops to 403 the moment the elevation ends. Do **not** add any `/platform/tenants/{id}/users/{id}/roles` endpoint.

- [ ] **Step 1: Write the test** — append to `test_platform_elevation.py`:

```python
async def test_elevated_super_admin_manages_tenant_roles(
    world: tuple[httpx.AsyncClient, World],
) -> None:
    # Spec decision 3: platform-admin cross-tenant role assignment IS the elevation
    # flow — elevate into the tenant, use the same /roles endpoints a tenant admin
    # uses, end the elevation. No parallel platform API.
    client, w = world
    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    # Elevated: the tenant role surface works under that tenant's RLS.
    roles = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert roles.status_code == 200, roles.text
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")

    catalog = await client.get("/api/v1/permissions", headers=_auth(w.super_token))
    assert catalog.status_code == 200, catalog.text
    assert not any(p["code"].startswith("platform:") for p in catalog.json()["data"])

    assigned = await client.post(
        f"/api/v1/users/{w.tenant_admin_id}/roles",
        headers=_auth(w.super_token),
        json={"role_id": supervisor_id},
    )
    assert assigned.status_code == 200, assigned.text

    listed = await client.get(
        f"/api/v1/users/{w.tenant_admin_id}/roles", headers=_auth(w.super_token)
    )
    assert "SUPERVISOR" in {r["name"] for r in listed.json()["data"]}

    revoked = await client.delete(
        f"/api/v1/users/{w.tenant_admin_id}/roles/{supervisor_id}",
        headers=_auth(w.super_token),
    )
    assert revoked.status_code == 200, revoked.text

    # Ended: the same routes immediately deny the platform token.
    ended = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert ended.status_code == 200
    after = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert after.status_code == 403
```

- [ ] **Step 2: Run it**

Run: `just test tests/integration/control_plane/test_platform_elevation.py -v`
Expected: all PASS with **no production-code change**. If the new test fails, that is a real finding about the elevation path (spec phase 5's purpose) — debug with superpowers:systematic-debugging; do not paper over it by adding a parallel platform API.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/control_plane/test_platform_elevation.py
git commit -m "test(rbac): elevated platform admin manages tenant roles end to end"
```

---

### Task 9: Full verification + mandatory simplification pass

**Files:** none new — verification only.

- [ ] **Step 1: Full local gate**

```bash
just up && just migrate   # ensure infra + schema current
just check                # ruff format --check + ruff check + mypy --strict + pytest
```

Expected: all green. Fix anything that isn't before proceeding.

- [ ] **Step 2: Migration round-trip sanity**

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: both succeed (the CHECK narrows back to `_OLD_VALUES`, then widens again). Confirm exactly one head: `uv run alembic heads` → a single revision. If two heads appear (a parallel branch landed), run `just merge-heads` — never renumber.

- [ ] **Step 3: Run the code-simplifier (repo-mandated)**

Trigger the `code-simplifier` agent with **"simplify code"** targeting the files changed in Tasks 1–8 (`api/v1/permissions.py`, `api/v1/roles.py`, `api/v1/__init__.py`, `models/enums.py`, the new migration, both test files). It must not change behavior.

- [ ] **Step 4: Re-run the gate after simplification**

```bash
just check
```

Expected: all green.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor(rbac): simplification pass over the RBAC endpoint work"
```

(Skip the commit if the simplifier changed nothing.)

---

## Self-Review (performed while writing)

- **Spec coverage:** §1 → Task 1 (read-only catalog, platform codes filtered, 403 test). §2 → Tasks 2, 4, 5, 6 (detail incl. permissions; description on create; PATCH with all five guards; DELETE-while-held 409 with count; ROLE_UPDATED/ROLE_DELETED events; holder invalidation). §3 → Tasks 3, 7, 8 (user-roles read; self-lockout guard keyed on the permission, self-only; elevation flow verified, no parallel API). §4 rules → Global Constraints (envelope, RLS, invalidation, audit, migrations, DoD; idempotency decision documented). §5 phases 1–3 and 5 → Tasks 1–8 in that order; phase 4 (UI) explicitly out of scope per the request.
- **Deliberate deviations from spec asides:** none affecting behavior. One spec aside ("the unique constraint means a tenant cannot reuse a system role's name") is not actually true of `UNIQUE (tenant_id, name) NULLS NOT DISTINCT` — a tenant row never collides with a NULL-tenant row — so no test asserts it.
- **Type consistency:** `PermissionResponse` defined once (Task 1) and imported (Task 2); `RoleDetailResponse` extends the existing `RoleResponse`; `resolver.invalidate(tenant_id, user_id)` matches `auth/rbac.py:84`; helpers `_create_role`/`_role_id_by_name`/`_permission_ids`/`_make_active_user` are defined before first use and reused verbatim.
- **Known verify-at-implementation points (flagged inline):** the `/auth/me` `id` field (Task 3 Step 1 note) and `auth_audit_log` NOT NULL/hash-chain columns for the raw INSERT (Task 4 Step 1 note).
