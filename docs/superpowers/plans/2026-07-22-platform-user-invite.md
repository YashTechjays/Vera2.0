# Platform User Invite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an existing platform operator (SUPER_ADMIN) invite, list, deactivate, and resend invitations to other platform operators — end to end, backend to frontend — closing the gap where the only platform user is the one-time bootstrap script's output. Also fix a real lockout gap in the existing tenant invite flow (no resend/reset path today).

**Architecture:** Mirrors the existing tenant invite flow (Redis-backed single-use tokens, RBAC-gated endpoints, invite→accept→MFA-activate state machine) but for `account_type='platform', tenant_id=NULL` accounts. Every INSERT/UPDATE of a NULL-tenant row must go through new `SECURITY DEFINER` SQL functions (the platform-readable RLS policy's `WITH CHECK` blocks direct ORM writes to NULL-tenant rows — confirmed against `vera_core/db/rls.py` and the existing `f066c667ddc1` precedent for platform MFA). DELETE is unaffected (RLS only evaluates `USING` for DELETE), so the resend flow's stale-identity cleanup is a plain ORM delete.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Postgres RLS (backend), React + Redux + Vitest (frontend), `playwright-cli` for end-to-end verification.

**Full design context:** `docs/superpowers/specs/2026-07-22-platform-user-invite-design.md` — read it before starting; this plan implements it directly.

## Global Constraints

- Backend: `just check` (ruff check + format --check + mypy --strict + pytest) must pass before any commit that claims a task done. Run it **verbatim**, never a hand-picked subset.
- Frontend: `tsc -b`, `eslint`, `npm test`, `npm run build` must all pass.
- Run the **code-simplifier** ("simplify code") on the full diff before the final commit of this feature (repo-root `CLAUDE.md`, mandatory) — then re-run `just check` / the frontend four-command gate again.
- New migrations: scaffold with `just makemigration "<message>"` (never hand-number a revision id) — this auto-fills `revision`/`down_revision` against the current head; then replace the empty `upgrade()`/`downgrade()` bodies with the hand-written SQL shown in each task.
- Every mutating endpoint needs `Depends(require_idempotency_key)` + `claim_or_conflict(...)`. Platform-tier (tenant-less) callers pass `PLATFORM_IDEM_SCOPE` (`UUID(int=0)`, defined in `control_plane/idempotency.py`) as the `tenant_id` argument to `claim_or_conflict` — never `None` (the parameter is typed `UUID`, not optional).
- Never construct `AuditRecord`/`AuthAuditRecord` by hand — always `emit_auth_event(...)` / `emit_phi_read_audit(...)`.
- Timestamps: only the DB clock (`now()` / `func.now()`), never `datetime.now()`.
- No PHI in this feature's surface — platform operators and tenant users being invited are workforce, not patients (matches the existing `users.py` docstring's stated invariant).
- Frontend has no `@testing-library/react` dependency (verified via `package.json`) — new component tests follow the two existing conventions only: `renderToStaticMarkup` + string assertions for rendering, and `vi.mock("@/lib/api/client")` for API-client behavior. Do not add a new testing dependency as part of this feature.

---

### Task 1: Seed `platform:users:invite` / `platform:users:read` permissions

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Create: `migrations/versions/<timestamp>_<hex>_seed_platform_users_permissions.py` (via `just makemigration`)
- Test: `tests/unit/models/test_rbac_defaults.py` (create if it doesn't already exist)

**Interfaces:**
- Produces: permission codes `"platform:users:invite"`, `"platform:users:read"`, present in `PLATFORM_PERMISSIONS` / `ALL_PERMISSIONS` / `SYSTEM_ROLES["SUPER_ADMIN"]`, and present as `role_permission` rows for the `SUPER_ADMIN` role in the DB after migration.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/models/test_rbac_defaults.py
from vera_core.models.rbac_defaults import ALL_PERMISSIONS, PLATFORM_PERMISSIONS, SYSTEM_ROLES


def test_platform_users_permissions_are_seeded() -> None:
    assert "platform:users:invite" in PLATFORM_PERMISSIONS
    assert "platform:users:read" in PLATFORM_PERMISSIONS
    assert "platform:users:invite" in ALL_PERMISSIONS
    assert "platform:users:read" in ALL_PERMISSIONS


def test_super_admin_holds_the_new_platform_permissions() -> None:
    assert "platform:users:invite" in SYSTEM_ROLES["SUPER_ADMIN"]
    assert "platform:users:read" in SYSTEM_ROLES["SUPER_ADMIN"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/models/test_rbac_defaults.py -v`
Expected: FAIL — `assert "platform:users:invite" in PLATFORM_PERMISSIONS` (KeyError-shaped assertion failure; the codes don't exist yet).

- [ ] **Step 3: Add the permissions to the catalog**

In `packages/vera_core/src/vera_core/models/rbac_defaults.py`, add two entries to `PLATFORM_PERMISSIONS` (after the existing `"platform:form_schemas:read"` entry):

```python
    "platform:form_schemas:read": "View form schemas and their versions",
    "platform:users:invite": "Invite, resend invitations to, and deactivate platform operators",
    "platform:users:read": "View platform operators",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/models/test_rbac_defaults.py -v`
Expected: PASS

- [ ] **Step 5: Scaffold and write the migration**

Run: `cd vera-backend && just makemigration "seed platform users invite and read permissions"`

This creates an empty-bodied migration file with an auto-generated revision id and `down_revision` set to the current head. Open it and replace the generated content with:

```python
"""seed platform users invite and read permissions

Revision ID: <auto-generated — keep as scaffolded>
Revises: <auto-generated — keep as scaffolded>
Create Date: <auto-generated — keep as scaffolded>

Platform operators gain a Platform Operators screen to invite, list, and deactivate
other operators, gated by two new permissions. Seeds them and grants both to the
global SUPER_ADMIN role, mirroring rbac_defaults.py. No backfill: new capability,
not a rename.

Runs on the privileged migration connection (not RLS-bound) — the strict WITH CHECK
on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "<keep the auto-generated value>"
down_revision: str | None = "<keep the auto-generated value>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = {
    "platform:users:invite": "Invite, resend invitations to, and deactivate platform operators",
    "platform:users:read": "View platform operators",
}


def upgrade() -> None:
    for code, description in _PERMISSIONS.items():
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            "WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' "
            f"AND p.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as every prior permission seed migration (e.g. f503e82734cc):
    # grants are indistinguishable from live product data added since — revert by
    # hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_platform_users_permissions: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
```

- [ ] **Step 6: Apply the migration and verify**

Run: `cd vera-backend && just up && just migrate`
Expected: migration applies cleanly. Then verify manually:

```bash
docker compose exec postgres psql -U vera -d vera -c \
  "SELECT p.code FROM permission p JOIN role_permission rp ON rp.permission_id = p.id JOIN role r ON r.id = rp.role_id WHERE r.name = 'SUPER_ADMIN' AND p.code LIKE 'platform:users:%'"
```
Expected output: both `platform:users:invite` and `platform:users:read` rows.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/models/rbac_defaults.py migrations/versions/*seed_platform_users_permissions.py tests/unit/models/test_rbac_defaults.py
git commit -m "feat(rbac): seed platform:users:invite and platform:users:read permissions"
```

---

### Task 2: New `AuthEvent` members + widen `auth_audit_log` CHECK constraint

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py`
- Create: `migrations/versions/<timestamp>_<hex>_widen_auth_audit_log_event_type_check_for_platform_invite.py` (via `just makemigration`)
- Test: `tests/integration/control_plane/test_platform_users.py` (create — will be reused/extended in later tasks)

**Interfaces:**
- Produces: `AuthEvent.INVITE_RESENT`, `AuthEvent.PLATFORM_USER_INVITED`, `AuthEvent.PLATFORM_INVITE_ACCEPTED`, `AuthEvent.PLATFORM_USER_ACTIVATED`, `AuthEvent.PLATFORM_USER_DEACTIVATED`, `AuthEvent.PLATFORM_INVITE_RESENT` — used by every later task's `emit_auth_event(...)` calls.

- [ ] **Step 1: Add the new members to `AuthEvent`**

In `packages/vera_core/src/vera_core/models/enums.py`, extend the `AuthEvent` enum (after `RETENTION_POLICY_UPDATED`):

```python
    RETENTION_POLICY_UPDATED = "retention_policy_updated"
    # Tenant-tier invite resend (fixes a gap: neither the invite link nor the MFA
    # bridge token had any recovery path before this feature).
    INVITE_RESENT = "invite_resent"
    # Platform-operator lifecycle — kept distinct from the tenant USER_INVITED /
    # INVITE_ACCEPTED / USER_DEACTIVATED events so privilege-granting activity is
    # separately auditable.
    PLATFORM_USER_INVITED = "platform_user_invited"
    PLATFORM_INVITE_ACCEPTED = "platform_invite_accepted"
    PLATFORM_USER_ACTIVATED = "platform_user_activated"
    PLATFORM_USER_DEACTIVATED = "platform_user_deactivated"
    PLATFORM_INVITE_RESENT = "platform_invite_resent"
```

- [ ] **Step 2: Write a failing unit test for the enum values**

```python
# tests/unit/models/test_enums.py (add to existing file, or create it if absent)
from vera_core.models.enums import AuthEvent


def test_new_platform_invite_auth_events_exist() -> None:
    assert AuthEvent.INVITE_RESENT == "invite_resent"
    assert AuthEvent.PLATFORM_USER_INVITED == "platform_user_invited"
    assert AuthEvent.PLATFORM_INVITE_ACCEPTED == "platform_invite_accepted"
    assert AuthEvent.PLATFORM_USER_ACTIVATED == "platform_user_activated"
    assert AuthEvent.PLATFORM_USER_DEACTIVATED == "platform_user_deactivated"
    assert AuthEvent.PLATFORM_INVITE_RESENT == "platform_invite_resent"
```

Run: `cd vera-backend && uv run pytest tests/unit/models/test_enums.py -v`
Expected (before Step 1 is applied — reorder if you're doing strict red-green — otherwise this passes immediately since Step 1 already added them): PASS once Step 1 is done. If you want a true red-green cycle, write this test first, confirm it fails with `AttributeError: PLATFORM_USER_INVITED`, then do Step 1.

- [ ] **Step 3: Scaffold and write the CHECK-widening migration**

Run: `cd vera-backend && just makemigration "widen auth audit event check for platform invite"`

Replace the generated body, following the exact pattern of `3f8ecb6efb86`/`fb43bdd169b2`:

```python
"""widen auth audit event check for platform invite

Revision ID: <auto-generated — keep as scaffolded>
Revises: <auto-generated — keep as scaffolded>
Create Date: <auto-generated — keep as scaffolded>

Adds invite_resent, platform_user_invited, platform_invite_accepted,
platform_user_activated, platform_user_deactivated, platform_invite_resent.
Same pattern as fb43bdd169b2/3f8ecb6efb86: drop-and-recreate the named CHECK from
the CURRENT enum — a no-op on a fresh DB and an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "<keep the auto-generated value>"
down_revision: str | None = "<keep the auto-generated value>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = (
    "invite_resent",
    "platform_user_invited",
    "platform_invite_accepted",
    "platform_user_activated",
    "platform_user_deactivated",
    "platform_invite_resent",
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
    _recreate(tuple(v for v in values_of(AuthEvent) if v not in _NEW_VALUES))
```

- [ ] **Step 4: Apply and verify**

Run: `cd vera-backend && just migrate`
Expected: applies cleanly.

```bash
docker compose exec postgres psql -U vera -d vera -c \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_auth_audit_log_event_type_valid'"
```
Expected: the printed CHECK clause's `IN (...)` list includes `'platform_user_invited'` etc.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/models/enums.py migrations/versions/*widen_auth_audit_log_event_type_check_for_platform_invite.py tests/unit/models/test_enums.py
git commit -m "feat(auth): add platform-invite AuthEvent members and widen the audit CHECK"
```

---

### Task 3: Widen `InviteData` for platform invites + new Redis namespaces

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/invitations.py`
- Test: `tests/unit/auth/test_invitations.py` (extend the existing file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `InviteData.tenant_id: UUID | None` (was `UUID`), `PLATFORM_INVITE_NS = "platform_invite"`, `PLATFORM_INVITE_MFA_NS = "platform_invite_mfa"` — used by every later backend task that touches invite tokens.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/auth/test_invitations.py`:

```python
from control_plane.auth.invitations import PLATFORM_INVITE_NS, PLATFORM_INVITE_MFA_NS


def _platform_data() -> InviteData:
    return InviteData(tenant_id=None, app_user_id=USER, email="operator@example.com")


async def test_platform_invite_roundtrip_with_null_tenant() -> None:
    store = InMemoryInvitationStore()
    token = await store.put(PLATFORM_INVITE_NS, _platform_data(), 60)
    assert await store.get(PLATFORM_INVITE_NS, token) == _platform_data()


def test_platform_invite_data_json_roundtrip_with_null_tenant() -> None:
    assert InviteData.from_json(_platform_data().to_json()) == _platform_data()


def test_platform_namespaces_are_distinct_from_tenant_namespaces() -> None:
    assert PLATFORM_INVITE_NS not in (INVITE_NS, INVITE_MFA_NS)
    assert PLATFORM_INVITE_MFA_NS not in (INVITE_NS, INVITE_MFA_NS)
```

(`INVITE_MFA_NS` needs adding to that file's existing import line too: `from control_plane.auth.invitations import (INVITE_NS, INVITE_MFA_NS, InMemoryInvitationStore, InviteData, _hashed, _key)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/auth/test_invitations.py -v`
Expected: FAIL — `ImportError: cannot import name 'PLATFORM_INVITE_NS'`.

- [ ] **Step 3: Widen `InviteData` and add the namespaces**

In `apps/control_plane/src/control_plane/auth/invitations.py`:

```python
INVITE_NS = "invite"
INVITE_MFA_NS = "invite_mfa"
PLATFORM_INVITE_NS = "platform_invite"
PLATFORM_INVITE_MFA_NS = "platform_invite_mfa"


@dataclass(frozen=True)
class InviteData:
    """What an invite token resolves to — only identifiers, never a secret.
    `tenant_id` is None for a platform-operator invite (no tenant); the caller's
    namespace (tenant vs. platform) is the actual security boundary, this field is
    just the data payload."""

    tenant_id: UUID | None
    app_user_id: UUID
    email: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_id": str(self.tenant_id) if self.tenant_id is not None else None,
                "app_user_id": str(self.app_user_id),
                "email": self.email,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "InviteData":
        d = json.loads(raw)
        return cls(
            tenant_id=UUID(d["tenant_id"]) if d["tenant_id"] is not None else None,
            app_user_id=UUID(d["app_user_id"]),
            email=d["email"],
        )
```

Also update the module docstring's "Two namespaces share one store" list to mention the platform pair, and update the `InvitationStore` protocol's return-type comment if needed (no signature change required there).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/auth/test_invitations.py -v`
Expected: PASS. Also re-run the whole file to confirm the widened `tenant_id` type didn't break the existing tenant-scoped tests (`_data()` still passes a real UUID, which stays valid under `UUID | None`).

- [ ] **Step 5: Fix the one existing call site whose type now needs a second look**

`apps/control_plane/src/control_plane/api/v1/users.py:172` constructs `InviteData(tenant_id=tenant_id, ...)` where `tenant_id: TenantId` (never None in that tenant-scoped route) — no change needed, `UUID` is a valid `UUID | None`. Run `cd vera-backend && uv run mypy --strict apps/control_plane/src/control_plane/auth/invitations.py apps/control_plane/src/control_plane/api/v1/users.py apps/control_plane/src/control_plane/api/v1/auth.py` to confirm mypy is satisfied with the widened type at every existing call site.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/invitations.py tests/unit/auth/test_invitations.py
git commit -m "feat(auth): widen InviteData for platform invites, add platform invite namespaces"
```

---

### Task 4: SECURITY DEFINER functions for NULL-tenant writes + Python wrappers

**Files:**
- Create: `migrations/versions/<timestamp>_<hex>_platform_operator_lifecycle_definer_functions.py` (via `just makemigration`)
- Create: `apps/control_plane/src/control_plane/auth/platform_provisioning.py`
- Test: `tests/integration/control_plane/test_platform_provisioning.py`

**Interfaces:**
- Produces:
  - `async def create_operator_invite(session: AsyncSession, *, email: str, name: str, invited_by: UUID) -> UUID` — creates the invited `AppUser` + `SUPER_ADMIN` grant, returns the new `AppUser.id`.
  - `async def create_password_identity(session: AsyncSession, *, app_user_id: UUID, email: str, hashed_password: str) -> UUID` — creates the `UserIdentity`, returns its id.
  - `async def set_operator_status(session: AsyncSession, *, app_user_id: UUID, status: str) -> bool` — flips status to `"active"` or `"deactivated"`; returns whether a row was updated.
- Consumes: an `AsyncSession` obtained from `platform_session(sessionmaker)` (the RLS context with `app.platform='on'`).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/control_plane/test_platform_provisioning.py
"""Integration tests for the platform-operator SECURITY DEFINER write helpers —
run against a real RLS-enforcing Postgres (not mocked), since the whole point of
these functions is to work around a real RLS restriction."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from control_plane.auth.platform_provisioning import (
    create_operator_invite,
    create_password_identity,
    set_operator_status,
)
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.db import platform_session
from vera_core.models import AppUser, UserIdentity, UserRole

pytestmark = pytest.mark.anyio


@pytest.fixture
async def rls_sessionmaker(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """A plain RLS-bound sessionmaker (no HTTP app) for testing the definer-wrapper
    functions directly. `database_url` is the superuser connection, used only to
    seed the permission catalog + SUPER_ADMIN role before RLS-bound tests run against
    `rls_database_url` — mirrors test_platform_elevation.py's `world` fixture split
    between the two connection strings, minus the HTTP app layer this file doesn't need."""
    seed_engine = create_async_engine(database_url)
    seed_sm = async_sessionmaker(seed_engine, expire_on_commit=False)
    async with seed_sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)

    rls_engine = create_async_engine(rls_database_url)
    rls_sm = async_sessionmaker(rls_engine, expire_on_commit=False)
    yield rls_sm

    async with seed_sm() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM user_identity WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_role WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(text("DELETE FROM app_user WHERE account_type = 'platform'"))
    await rls_engine.dispose()
    await seed_engine.dispose()


async def test_create_operator_invite_creates_invited_platform_user_with_super_admin(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="new-operator@example.com", name="New Operator", invited_by=None
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one()
        assert user.account_type == "platform"
        assert user.tenant_id is None
        assert user.status == "invited"
        assert user.email == "new-operator@example.com"

        role_ids = (
            await session.execute(select(UserRole.role_id).where(UserRole.app_user_id == user_id))
        ).scalars().all()
        role_names = (
            await session.execute(
                text("SELECT name FROM role WHERE id = ANY(:ids)").bindparams(ids=list(role_ids))
            )
        ).scalars().all()
        assert "SUPER_ADMIN" in role_names


async def test_plain_orm_insert_of_null_tenant_app_user_is_rejected_by_rls(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Proves the constraint this task exists to work around — a direct ORM insert
    of a NULL-tenant row must fail under RLS, confirming the definer function is
    actually necessary and not incidental complexity."""
    async with platform_session(rls_sessionmaker) as session:
        session.add(
            AppUser(
                tenant_id=None,
                account_type="platform",
                email="should-fail@example.com",
                name="",
                status="invited",
            )
        )
        with pytest.raises(Exception):  # asyncpg raises a RLS policy violation
            await session.flush()


async def test_create_password_identity_then_set_status_active(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="accepts@example.com", name="", invited_by=None
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        identity_id = await create_password_identity(
            session, app_user_id=user_id, email="accepts@example.com", hashed_password="hashed"
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        identity = (
            await session.execute(select(UserIdentity).where(UserIdentity.id == identity_id))
        ).scalar_one()
        assert identity.app_user_id == user_id
        assert identity.tenant_id is None
        assert identity.hashed_password == "hashed"

        flipped = await set_operator_status(session, app_user_id=user_id, status="active")
        await session.commit()
        assert flipped is True

    async with platform_session(rls_sessionmaker) as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one()
        assert user.status == "active"


async def test_set_operator_status_rejects_invalid_status(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="bad-status@example.com", name="", invited_by=None
        )
        await session.commit()
    async with platform_session(rls_sessionmaker) as session:
        with pytest.raises(Exception):
            await set_operator_status(session, app_user_id=user_id, status="not-a-real-status")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && just up && just migrate && uv run pytest tests/integration/control_plane/test_platform_provisioning.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'control_plane.auth.platform_provisioning'`.

- [ ] **Step 3: Scaffold and write the migration**

Run: `cd vera-backend && just makemigration "platform operator lifecycle definer functions"`

Replace the generated body:

```python
"""platform operator lifecycle definer functions

Revision ID: <auto-generated — keep as scaffolded>
Revises: <auto-generated — keep as scaffolded>
Create Date: <auto-generated — keep as scaffolded>

The platform-operator invite/accept/deactivate lifecycle needs to INSERT/UPDATE
NULL-tenant app_user / user_identity / user_role rows. The platform-readable RLS
policy's WITH CHECK is strict equality (vera_core/db/rls.py), so the RLS-bound app
role can never write a NULL-tenant row directly — same restriction that migration
f066c667ddc1 worked around for platform MFA enrollment, now extended to the
invite/deactivate lifecycle. Mirror that sanctioned pattern: narrow, fixed-search_path
SECURITY DEFINER functions owned by vera_definer_owner (NOLOGIN, BYPASSRLS), each
guarded by current_setting('app.platform', true) = 'on' so only a platform session
can invoke them.

DELETE is unaffected by this restriction (RLS only evaluates USING, not WITH CHECK,
for DELETE) — the invite-resend flow's stale-identity cleanup stays a plain ORM
delete and needs no definer function.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "<keep the auto-generated value>"
down_revision: str | None = "<keep the auto-generated value>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"
_GUARD = "current_setting('app.platform', true) = 'on'"

_CREATE_OPERATOR_INVITE = f"""
CREATE OR REPLACE FUNCTION platform_create_operator_invite(
    p_email text,
    p_name text,
    p_invited_by uuid
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_user_id uuid;
    v_role_id uuid;
BEGIN
    IF NOT ({_GUARD}) THEN
        RAISE EXCEPTION 'platform_create_operator_invite: not a platform session';
    END IF;

    SELECT id INTO v_role_id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN';
    IF v_role_id IS NULL THEN
        RAISE EXCEPTION 'platform_create_operator_invite: SUPER_ADMIN role not found';
    END IF;

    INSERT INTO app_user (id, tenant_id, account_type, email, name, status, invited_by)
    VALUES (gen_random_uuid(), NULL, 'platform', p_email, p_name, 'invited', p_invited_by)
    RETURNING id INTO v_user_id;

    INSERT INTO user_role (id, tenant_id, app_user_id, role_id, granted_by, granted_at)
    VALUES (gen_random_uuid(), NULL, v_user_id, v_role_id, p_invited_by, now());

    RETURN v_user_id;
END;
$$
"""

_CREATE_PASSWORD_IDENTITY = f"""
CREATE OR REPLACE FUNCTION platform_create_password_identity(
    p_app_user_id uuid,
    p_email text,
    p_hashed_password text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_identity_id uuid;
BEGIN
    IF NOT ({_GUARD}) THEN
        RAISE EXCEPTION 'platform_create_password_identity: not a platform session';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM app_user
         WHERE id = p_app_user_id AND tenant_id IS NULL AND account_type = 'platform'
    ) THEN
        RAISE EXCEPTION 'platform_create_password_identity: no such platform operator';
    END IF;

    INSERT INTO user_identity (
        id, tenant_id, app_user_id, provider_type, provider_subject, email,
        hashed_password, mfa_enabled
    )
    VALUES (
        gen_random_uuid(), NULL, p_app_user_id, 'password', p_email, p_email,
        p_hashed_password, false
    )
    RETURNING id INTO v_identity_id;

    RETURN v_identity_id;
END;
$$
"""

_SET_OPERATOR_STATUS = f"""
CREATE OR REPLACE FUNCTION platform_set_operator_status(
    p_app_user_id uuid,
    p_status text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF NOT ({_GUARD}) THEN
        RAISE EXCEPTION 'platform_set_operator_status: not a platform session';
    END IF;
    IF p_status NOT IN ('active', 'deactivated') THEN
        RAISE EXCEPTION 'platform_set_operator_status: invalid status %', p_status;
    END IF;

    UPDATE app_user
       SET status = p_status
     WHERE id = p_app_user_id
       AND tenant_id IS NULL
       AND account_type = 'platform';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

_FUNCTIONS = (_CREATE_OPERATOR_INVITE, _CREATE_PASSWORD_IDENTITY, _SET_OPERATOR_STATUS)
_SIGNATURES = (
    "platform_create_operator_invite(text, text, uuid)",
    "platform_create_password_identity(uuid, text, text)",
    "platform_set_operator_status(uuid, text)",
)


def upgrade() -> None:
    # Column-scoped grants — the definer owner can only touch exactly the columns
    # each function needs, never e.g. app_user.tenant_id or user_identity.totp_seed_ct.
    op.execute(f"GRANT SELECT ON role TO {DEFINER_ROLE}")
    op.execute(f"GRANT SELECT ON app_user TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT INSERT (id, tenant_id, account_type, email, name, status, invited_by) "
        f"ON app_user TO {DEFINER_ROLE}"
    )
    op.execute(f"GRANT UPDATE (status) ON app_user TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT INSERT (id, tenant_id, app_user_id, role_id, granted_by, granted_at) "
        f"ON user_role TO {DEFINER_ROLE}"
    )
    op.execute(
        f"GRANT INSERT (id, tenant_id, app_user_id, provider_type, provider_subject, "
        f"email, hashed_password, mfa_enabled) ON user_identity TO {DEFINER_ROLE}"
    )
    for fn in _FUNCTIONS:
        op.execute(fn)
    for sig in _SIGNATURES:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(f"REVOKE ALL ON app_user FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE ALL ON user_role FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE ALL ON user_identity FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE SELECT ON role FROM {DEFINER_ROLE}")
```

Note: the `REVOKE ALL ON user_identity` in `downgrade()` would also revoke the `SELECT, UPDATE (...)` grant that `f066c667ddc1` gave this same role — if this migration is ever downgraded, `f066c667ddc1`'s functions would need their grant re-applied too. This mirrors how these migrations already treat downgrade as a rare, manually-supervised path (most of them simply `raise RuntimeError`); leave a comment noting it rather than trying to surgically revoke only this migration's own grant additions.

- [ ] **Step 4: Write the Python wrapper module**

```python
# apps/control_plane/src/control_plane/auth/platform_provisioning.py
"""SECURITY DEFINER write helpers for NULL-tenant (platform-operator) rows
(migration: platform operator lifecycle definer functions). The platform-readable
RLS policy's WITH CHECK is strict equality (vera_core/db/rls.py), so the RLS-bound
app role can never INSERT or UPDATE a NULL-tenant row directly — only SELECT and
DELETE work unassisted (RLS evaluates USING, not WITH CHECK, for those). Mirrors the
platform MFA definer pattern (control_plane/auth/mfa.py, migration f066c667ddc1) for
the invite/accept/deactivate lifecycle instead of MFA enrollment.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_operator_invite(
    session: AsyncSession, *, email: str, name: str, invited_by: UUID | None
) -> UUID:
    """Create an invited platform AppUser and grant it SUPER_ADMIN, atomically."""
    result = await session.execute(
        text(
            "SELECT platform_create_operator_invite(:email, :name, CAST(:invited_by AS uuid))"
        ).bindparams(email=email, name=name, invited_by=invited_by)
    )
    return result.scalar_one()


async def create_password_identity(
    session: AsyncSession, *, app_user_id: UUID, email: str, hashed_password: str
) -> UUID:
    """Create the password UserIdentity for a platform operator accepting their invite."""
    result = await session.execute(
        text(
            "SELECT platform_create_password_identity("
            "CAST(:app_user_id AS uuid), :email, :hashed_password)"
        ).bindparams(app_user_id=app_user_id, email=email, hashed_password=hashed_password)
    )
    return result.scalar_one()


async def set_operator_status(session: AsyncSession, *, app_user_id: UUID, status: str) -> bool:
    """Flip a platform operator's status to 'active' or 'deactivated'. Returns
    whether a row was actually updated (False if the id doesn't match a platform
    operator)."""
    result = await session.execute(
        text(
            "SELECT platform_set_operator_status(CAST(:app_user_id AS uuid), :status)"
        ).bindparams(app_user_id=app_user_id, status=status)
    )
    return bool(result.scalar_one())
```

- [ ] **Step 5: Apply the migration and run the tests**

Run: `cd vera-backend && just migrate && uv run pytest tests/integration/control_plane/test_platform_provisioning.py -v`
Expected: PASS on all five tests, including the RLS-rejection proof test.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/*platform_operator_lifecycle_definer_functions.py apps/control_plane/src/control_plane/auth/platform_provisioning.py tests/integration/control_plane/test_platform_provisioning.py
git commit -m "feat(auth): SECURITY DEFINER functions for platform operator invite/accept/deactivate"
```

---

### Task 5: Shared resend/reset helper (tenant + platform)

**Files:**
- Create: `apps/control_plane/src/control_plane/auth/invite_reset.py`
- Test: `tests/integration/control_plane/test_invite_reset.py` (needs a real Postgres — see Step 1)

**Interfaces:**
- Consumes: `InvitationStore` (Task 3), `AppUser` model.
- Produces: `async def reset_and_reissue_invite(session: AsyncSession, invites: InvitationStore, *, namespace: str, app_user: AppUser, ttl_seconds: int) -> str` — deletes any stale password `UserIdentity` for `app_user`, mints and returns a fresh token in `namespace`.

- [ ] **Step 1: Write the failing test**

This test needs a real Postgres (it exercises a real DELETE + a real `AppUser` row), so despite the module name matching the `tests/unit/` convention for this file's OTHER content, place it under `tests/integration/control_plane/test_invite_reset.py` instead — `tests/unit/` in this repo is reserved for no-DB tests (confirmed by every `tests/unit/auth/test_invitations.py` test using only `InMemoryInvitationStore`, no Postgres). Self-contained — seeds its own tiny tenant + AppUser via the superuser `database_url` connection, then exercises `reset_and_reissue_invite` through a real `tenant_session`:

```python
# tests/integration/control_plane/test_invite_reset.py
"""Integration test for the shared resend/reset helper — needs a real Postgres
since it exercises a real DELETE against a real AppUser/UserIdentity pair."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from control_plane.auth.invitations import INVITE_NS, InMemoryInvitationStore
from control_plane.auth.invite_reset import reset_and_reissue_invite
from vera_core.db import tenant_session, uuid7
from vera_core.models import AppUser, Tenant, UserIdentity
from vera_core.models.enums import ProviderKind

pytestmark = pytest.mark.anyio


@dataclass
class TenantWorld:
    tenant_id: UUID
    user_id: UUID
    email: str


@pytest.fixture
async def tenant_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], TenantWorld]]:
    seed_engine = create_async_engine(database_url)
    seed_sm = async_sessionmaker(seed_engine, expire_on_commit=False)
    tenant_id, user_id = uuid7(), uuid7()
    email = "stuck-invitee@test.example"

    async with seed_sm() as s, s.begin():
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name="Invite Reset Test", status="active"))
        await s.flush()
        s.add(
            AppUser(
                id=user_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=email,
                name="Stuck Invitee",
                status="invited",
            )
        )

    rls_engine = create_async_engine(rls_database_url)
    rls_sm = async_sessionmaker(rls_engine, expire_on_commit=False)
    yield rls_sm, TenantWorld(tenant_id=tenant_id, user_id=user_id, email=email)

    async with seed_sm() as s, s.begin():
        await s.execute(text("DELETE FROM user_identity WHERE app_user_id = :u").bindparams(u=user_id))
        await s.execute(text("DELETE FROM app_user WHERE id = :u").bindparams(u=user_id))
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await rls_engine.dispose()
    await seed_engine.dispose()


async def test_reset_and_reissue_deletes_stale_identity_and_mints_fresh_token(
    tenant_world: tuple[async_sessionmaker[AsyncSession], TenantWorld],
) -> None:
    rls_sm, world = tenant_world
    invites = InMemoryInvitationStore()

    async with tenant_session(rls_sm, world.tenant_id) as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == world.user_id))).scalar_one()
        session.add(
            UserIdentity(
                tenant_id=world.tenant_id,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=user.email,
                email=user.email,
                hashed_password="stale-hash",
                mfa_enabled=False,
            )
        )
        await session.flush()

        token = await reset_and_reissue_invite(
            session, invites, namespace=INVITE_NS, app_user=user, ttl_seconds=60
        )

        remaining = (
            await session.execute(select(UserIdentity).where(UserIdentity.app_user_id == user.id))
        ).scalars().all()
        assert remaining == []

    fetched = await invites.get(INVITE_NS, token)
    assert fetched is not None
    assert fetched.app_user_id == world.user_id
    assert fetched.email == world.email
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_invite_reset.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'control_plane.auth.invite_reset'`.

- [ ] **Step 3: Write the helper**

```python
# apps/control_plane/src/control_plane/auth/invite_reset.py
"""Shared reset/reissue logic for a stuck invitee — someone whose invite link or
MFA bridge token expired before they finished onboarding, leaving `status="invited"`
permanently stuck (no prior resend/reset path existed in this codebase for either
tier). Used by both the tenant and platform resend-invitation endpoints; the only
difference between tiers is the InvitationStore namespace and which AuthEvent the
caller emits."""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.invitations import InvitationStore, InviteData
from vera_core.models import AppUser, UserIdentity
from vera_core.models.enums import ProviderKind


async def reset_and_reissue_invite(
    session: AsyncSession,
    invites: InvitationStore,
    *,
    namespace: str,
    app_user: AppUser,
    ttl_seconds: int,
) -> str:
    """Delete any stale password UserIdentity for `app_user` (safe: it's useless if
    MFA was never completed — a fresh accept will create a new one) and mint a
    fresh invite token in `namespace`. Returns the raw token for the caller to
    build a fresh invite_url. DELETE needs no SECURITY DEFINER helper even for a
    NULL-tenant platform row: RLS only evaluates USING (not WITH CHECK) for DELETE,
    and the platform-readable policy's USING clause already permits NULL-tenant
    rows under a platform session."""
    await session.execute(
        delete(UserIdentity).where(
            UserIdentity.app_user_id == app_user.id,
            UserIdentity.provider_type == ProviderKind.PASSWORD.value,
        )
    )
    return await invites.put(
        namespace,
        InviteData(tenant_id=app_user.tenant_id, app_user_id=app_user.id, email=app_user.email),
        ttl_seconds,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_invite_reset.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/invite_reset.py tests/integration/control_plane/test_invite_reset.py
git commit -m "feat(auth): shared resend/reset helper for stuck tenant and platform invitees"
```

---

### Task 6: Tenant resend-invitation endpoint

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/users.py`
- Test: `tests/integration/control_plane/test_admin.py` (extend)

**Interfaces:**
- Consumes: `reset_and_reissue_invite` (Task 5).
- Produces: `POST /users/{user_id}/resend-invitation` → `ResponseModel[InviteUserResponse]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/control_plane/test_admin.py`:

```python
async def test_resend_invitation_reissues_a_working_token(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "stuck@test.example", "name": "Stuck", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    stale_token = _extract_token(invite)

    resend = await client.post(
        f"/api/v1/users/{user_id}/resend-invitation",
        headers={**_auth(rbac_world.admin_token), **_idem()},
    )
    assert resend.status_code == 200, resend.text
    fresh_token = _extract_token(resend)
    assert fresh_token != stale_token

    # The stale token no longer validates; the fresh one does.
    tid = rbac_world.tenant_id
    stale_check = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate", params={"token": stale_token}
    )
    assert stale_check.json()["data"]["state"] == "invalid"
    fresh_check = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate", params={"token": fresh_token}
    )
    assert fresh_check.json()["data"]["state"] == "valid"


async def test_resend_invitation_409s_if_already_accepted(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "already-active@test.example", "name": "", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    token = _extract_token(invite)
    tid = rbac_world.tenant_id
    accept = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text

    resend = await client.post(
        f"/api/v1/users/{user_id}/resend-invitation",
        headers={**_auth(rbac_world.admin_token), **_idem()},
    )
    assert resend.status_code == 409, resend.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_admin.py -k resend_invitation -v`
Expected: FAIL — 404 (route doesn't exist yet).

- [ ] **Step 3: Add the endpoint**

In `apps/control_plane/src/control_plane/api/v1/users.py`, add the import and the new route (after `deactivate_user`):

```python
from control_plane.auth.invite_reset import reset_and_reissue_invite
```

```python
@router.post(
    "/users/{user_id}/resend-invitation",
    response_model=ResponseModel[InviteUserResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def resend_invitation(
    user_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: VerifiedIdentity = require("users:manage"),
) -> ResponseModel[InviteUserResponse]:
    """Reissue a fresh invite link for a user stuck in status="invited" (their
    original link or MFA bridge token expired before they finished onboarding).
    Deletes any stale password UserIdentity and mints a new INVITE_NS token."""
    if caller.tenant_slug is None:
        raise UnauthorizedError(message="malformed session: tenant slug missing")
    await claim_or_conflict(
        get_idempotency_store(request),
        tenant_id,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such user in this tenant")
    if user.status != "invited":
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="user is not in invited status"
        )

    token = await reset_and_reissue_invite(
        session, invites, namespace=INVITE_NS, app_user=user, ttl_seconds=settings.invite_ttl_seconds
    )
    invite_url = (
        f"{settings.frontend_base_url}/tenants/{caller.tenant_slug}/accept-invite?token={token}"
    )

    email_sent = False
    try:
        await email_sender.send(
            EmailMessage(
                to=user.email,
                subject="You're invited to Vera",
                body=(
                    f"Hello{(' ' + user.name) if user.name else ''},\n\n"
                    "Here is a fresh link to set your password "
                    f"(valid for {settings.invite_ttl_seconds // 3600} hours):\n\n"
                    f"{invite_url}\n\n"
                    "If you didn't expect this, you can ignore this email."
                ),
            )
        )
        email_sent = True
    except Exception:
        logger.warning("resend invitation email to %s could not be sent", user.email, exc_info=True)

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.INVITE_RESENT,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user.id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteUserResponse(
            user_id=user.id, email=user.email, invite_url=invite_url, email_sent=email_sent
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_admin.py -k resend_invitation -v`
Expected: PASS on both tests.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/users.py tests/integration/control_plane/test_admin.py
git commit -m "feat(users): add tenant resend-invitation endpoint"
```

---

### Task 7: Platform invite + list operators endpoints

**Files:**
- Create: `apps/control_plane/src/control_plane/api/v1/platform_users.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/__init__.py`
- Test: `tests/integration/control_plane/test_platform_users.py` (extend the file started in Task 2)

**Interfaces:**
- Consumes: `create_operator_invite` (Task 4), `PLATFORM_INVITE_NS` (Task 3).
- Produces: `POST /platform/users/invitations` → `ResponseModel[InviteOperatorResponse]`; `GET /platform/users` → `ResponseModel[list[OperatorResponse]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_platform_users.py
"""Integration tests for the platform-operator invite/list/deactivate/resend
endpoints, using the World/_mint pattern from test_platform_elevation.py (there is
no shared platform-tier conftest fixture in this repo yet — this file follows the
same local-fixture convention rather than inventing a different one)."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from control_plane.auth.session import InMemorySessionStore, SessionData
from vera_core.models import AppUser, UserRole

pytestmark = pytest.mark.anyio


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@dataclass
class PlatformWorld:
    super_admin_id: UUID
    super_admin_token: str


async def _mint_platform(store: InMemorySessionStore, *, user_id: UUID, email: str) -> str:
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=None,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="platform",
            tenant_slug=None,
        ),
        3600,
        3600,
    )


# The `platform_world` fixture (added in Step 5 below) follows test_platform_elevation.py's
# `World` pattern exactly — seed a SUPER_ADMIN AppUser + UserRole, mint a session token via
# InMemorySessionStore, build the app via create_app(...). It yields a single
# `(client, PlatformWorld)` tuple, so every test below destructures it as `client, pw = platform_world`
# rather than taking `client` as a separate fixture parameter.


async def test_invite_operator_creates_invited_platform_user_with_super_admin(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    resp = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "new-op@test.example", "name": "New Op", "send_email": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["email"] == "new-op@test.example"
    assert "/platform/accept-invite?token=" in body["invite_url"]


async def test_list_operators_includes_the_new_invite(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "listed-op@test.example", "name": "", "send_email": False},
    )
    resp = await client.get("/api/v1/platform/users", headers=_auth(pw.super_admin_token))
    assert resp.status_code == 200, resp.text
    emails = {row["email"] for row in resp.json()["data"]}
    assert "listed-op@test.example" in emails
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -v`
Expected: FAIL — 404 (routes don't exist yet) or a fixture error if `platform_world` isn't wired yet (wire it first, following `test_platform_elevation.py`'s exact `world` fixture, before running).

- [ ] **Step 3: Write the new router file**

```python
# apps/control_plane/src/control_plane/api/v1/platform_users.py
"""Platform-operator administration — an existing SUPER_ADMIN invites, lists,
deactivates, and resends invitations to platform operators. Mirrors
api/v1/users.py, but for account_type='platform' (tenant_id=NULL) accounts.
Invite acceptance lives in api/v1/platform_auth.py (pre-auth, no tenant slug).
Gated by `platform:users:invite` (write) / `platform:users:read` (list). Carries
no PHI — platform operators are workforce, not patients."""

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings, AuthAudit, Email, Invites
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invite_reset import reset_and_reissue_invite
from control_plane.auth.invitations import PLATFORM_INVITE_NS, InviteData
from control_plane.auth.platform_provisioning import create_operator_invite
from control_plane.auth.rbac import platform_require
from control_plane.deps import client_ip, get_idempotency_store, platform_scoped_session
from control_plane.email import EmailMessage
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.idempotency import PLATFORM_IDEM_SCOPE, claim_or_conflict, require_idempotency_key
from control_plane.responses import ResponseModel, ok
from vera_core.audit import emit_auth_event
from vera_core.models import AppUser
from vera_core.models.enums import AccountType, AuthEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform", tags=["platform-users"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]


class InviteOperatorRequest(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=255)
    send_email: bool = True


class InviteOperatorResponse(BaseModel):
    user_id: UUID
    email: str
    invite_url: str
    email_sent: bool


class OperatorResponse(BaseModel):
    id: UUID
    email: str
    name: str
    status: str
    last_login_at: datetime | None


def _to_response(row: AppUser) -> OperatorResponse:
    return OperatorResponse(
        id=row.id,
        email=row.email,
        name=row.name,
        status=row.status,
        last_login_at=row.last_login_at,
    )


@router.post(
    "/users/invitations",
    response_model=ResponseModel[InviteOperatorResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def invite_operator(
    body: InviteOperatorRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[InviteOperatorResponse]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    email = body.email
    existing = (
        await session.execute(select(AppUser.id).where(AppUser.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="a platform operator with that email already exists",
        )

    user_id = await create_operator_invite(
        session, email=email, name=body.name, invited_by=caller.user_id
    )

    token = await invites.put(
        PLATFORM_INVITE_NS,
        InviteData(tenant_id=None, app_user_id=user_id, email=email),
        settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.frontend_base_url}/platform/accept-invite?token={token}"

    email_sent = False
    if body.send_email:
        try:
            await email_sender.send(
                EmailMessage(
                    to=email,
                    subject="You're invited to Vera as a platform operator",
                    body=(
                        f"Hello{(' ' + body.name) if body.name else ''},\n\n"
                        "You've been invited as a Vera platform operator. Set your "
                        "password using the link below "
                        f"(valid for {settings.invite_ttl_seconds // 3600} hours). "
                        "Two-factor authentication is required to finish setup.\n\n"
                        f"{invite_url}\n\n"
                        "If you didn't expect this, you can ignore this email."
                    ),
                )
            )
            email_sent = True
        except Exception:
            logger.warning(
                "platform invitation email to %s could not be sent", email, exc_info=True
            )

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_INVITED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user_id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteOperatorResponse(
            user_id=user_id, email=email, invite_url=invite_url, email_sent=email_sent
        )
    )


@router.get(
    "/users",
    response_model=ResponseModel[list[OperatorResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_operators(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require("platform:users:read")],
) -> ResponseModel[list[OperatorResponse]]:
    rows = (await session.execute(select(AppUser).order_by(AppUser.email))).scalars().all()
    return ok([_to_response(r) for r in rows])
```

(`deactivate_operator` and `resend_operator_invitation` are added to this same file in Tasks 8–9 — kept as separate plan tasks since each has its own test cycle, but they land in the same file.)

- [ ] **Step 4: Wire the router**

In `apps/control_plane/src/control_plane/api/v1/__init__.py`, add the import and registration:

```python
from control_plane.api.v1.platform_users import router as platform_users_router
```

```python
router.include_router(platform_users_router)
```

(Add it near `platform_router`/`platform_auth_router` for locality.)

- [ ] **Step 5: Wire the `platform_world` test fixture**

In `tests/integration/control_plane/test_platform_users.py`, add the fixture, inlined verbatim from `test_platform_elevation.py`'s `world` fixture pattern (same `database_url`/`rls_database_url` fixtures already provided by the shared conftest, same `create_app(...)`/`InMemorySessionStore`/`LocalDevKMS`/`InMemoryPermissionCache` wiring, same function-scoped per-test cleanup) — only the seeded persona and teardown table list differ, since this feature needs no tenant at all:

```python
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7


@pytest.fixture
async def platform_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, PlatformWorld]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    super_id = uuid7()

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        s.add(
            AppUser(
                id=super_id,
                tenant_id=None,
                account_type="platform",
                email="root@vera.example",
                name="Root",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))

    store = InMemorySessionStore()
    super_admin_token = await _mint_platform(store, user_id=super_id, email="root@vera.example")

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=store,
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, PlatformWorld(super_admin_id=super_id, super_admin_token=super_admin_token)

    # Cleanup covers every platform app_user this test created (invite creates new
    # ones with generated emails, not just the seeded super_id), mirroring the
    # elevation test's per-test teardown scope.
    async with sm() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM auth_audit_log WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_identity WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_role WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(text("DELETE FROM app_user WHERE account_type = 'platform'"))
    await engine.dispose()
```

This fixture is function-scoped (no explicit `scope=` kwarg), matching `test_platform_elevation.py`'s `world` fixture exactly — every test gets an isolated, freshly-seeded platform world and full teardown, so tests never leak platform users into each other.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/platform_users.py apps/control_plane/src/control_plane/api/v1/__init__.py tests/integration/control_plane/test_platform_users.py
git commit -m "feat(platform): add invite-operator and list-operators endpoints"
```

---

### Task 8: Platform deactivate-operator endpoint with lockout guard

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/platform_users.py`
- Test: `tests/integration/control_plane/test_platform_users.py` (extend)

**Interfaces:**
- Consumes: `set_operator_status` (Task 4).
- Produces: `POST /platform/users/{user_id}/deactivate` → `ResponseModel[None]`.

- [ ] **Step 1: Write the failing test**

```python
async def test_deactivate_operator_succeeds_when_another_active_operator_remains(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "second-op@test.example", "name": "", "send_email": False},
    )
    second_id = invite.json()["data"]["user_id"]
    # Deactivating an invited (not yet active) second operator is fine — the lockout
    # guard only counts ACTIVE operators, and the seeded super_admin is still active.
    resp = await client.post(
        f"/api/v1/platform/users/{second_id}/deactivate",
        headers=_auth(pw.super_admin_token),
    )
    assert resp.status_code == 200, resp.text


async def test_deactivate_operator_blocks_the_last_active_operator(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    resp = await client.post(
        f"/api/v1/platform/users/{pw.super_admin_id}/deactivate",
        headers=_auth(pw.super_admin_token),
    )
    assert resp.status_code == 409, resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k deactivate_operator -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add the endpoint**

Add to `apps/control_plane/src/control_plane/api/v1/platform_users.py` (imports: add `func` to the existing `sqlalchemy` import, add `set_operator_status` to the `platform_provisioning` import):

```python
from sqlalchemy import func, select
from control_plane.auth.platform_provisioning import create_operator_invite, set_operator_status
```

```python
@router.post(
    "/users/{user_id}/deactivate",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def deactivate_operator(
    user_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[None]:
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such platform operator")

    if user.status == "active":
        active_count = (
            await session.execute(
                select(func.count())
                .select_from(AppUser)
                .where(
                    AppUser.account_type == AccountType.PLATFORM.value,
                    AppUser.status == "active",
                )
            )
        ).scalar_one()
        if active_count <= 1:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT,
                message="cannot deactivate the last active platform operator",
            )

    flipped = await set_operator_status(session, app_user_id=user_id, status="deactivated")
    if not flipped:
        raise NotFoundError(message="no such platform operator")

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_DEACTIVATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user_id)},
    )
    return ok(None, message="Platform operator deactivated.")
```

Note the accepted minor race: two concurrent deactivate calls against the second-to-last active operator could both pass the `active_count <= 1` check before either commits. This is a low-frequency admin action already protected from exact-retry duplication by the idempotency key; a `SELECT ... FOR UPDATE` lock across all active platform operators would add real complexity for a race that requires two admins racing the same rare action within milliseconds — not worth it here.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k deactivate_operator -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/platform_users.py tests/integration/control_plane/test_platform_users.py
git commit -m "feat(platform): add deactivate-operator endpoint with lockout guard"
```

---

### Task 9: Platform resend-invitation endpoint

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/platform_users.py`
- Test: `tests/integration/control_plane/test_platform_users.py` (extend)

**Interfaces:**
- Consumes: `reset_and_reissue_invite` (Task 5).
- Produces: `POST /platform/users/{user_id}/resend-invitation` → `ResponseModel[InviteOperatorResponse]`.

- [ ] **Step 1: Write the failing test**

```python
async def test_resend_operator_invitation_reissues_a_working_token(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "stuck-op@test.example", "name": "", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    stale_token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    resend = await client.post(
        f"/api/v1/platform/users/{user_id}/resend-invitation",
        headers={**_auth(pw.super_admin_token), **_idem()},
    )
    assert resend.status_code == 200, resend.text
    fresh_token = resend.json()["data"]["invite_url"].split("token=", 1)[1]
    assert fresh_token != stale_token

    stale_check = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": stale_token}
    )
    assert stale_check.json()["data"]["state"] == "invalid"
    fresh_check = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": fresh_token}
    )
    assert fresh_check.json()["data"]["state"] == "valid"
```

(This test depends on the `/platform/auth/invitations/validate` endpoint from Task 10 — either implement Task 10 first, or stub the validate assertions out temporarily and complete them once Task 10 lands. Given the natural dependency, execute Task 10 before finishing this test's assertions, or run Tasks 9 and 10 back-to-back.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k resend_operator -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add the endpoint**

Add to `apps/control_plane/src/control_plane/api/v1/platform_users.py`:

```python
@router.post(
    "/users/{user_id}/resend-invitation",
    response_model=ResponseModel[InviteOperatorResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def resend_operator_invitation(
    user_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[InviteOperatorResponse]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such platform operator")
    if user.status != "invited":
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="operator is not in invited status"
        )

    token = await reset_and_reissue_invite(
        session,
        invites,
        namespace=PLATFORM_INVITE_NS,
        app_user=user,
        ttl_seconds=settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.frontend_base_url}/platform/accept-invite?token={token}"

    email_sent = False
    try:
        await email_sender.send(
            EmailMessage(
                to=user.email,
                subject="You're invited to Vera as a platform operator",
                body=(
                    f"Hello{(' ' + user.name) if user.name else ''},\n\n"
                    "Here is a fresh link to set your password "
                    f"(valid for {settings.invite_ttl_seconds // 3600} hours). "
                    "Two-factor authentication is required to finish setup.\n\n"
                    f"{invite_url}\n\n"
                    "If you didn't expect this, you can ignore this email."
                ),
            )
        )
        email_sent = True
    except Exception:
        logger.warning(
            "resend platform invitation email to %s could not be sent", user.email, exc_info=True
        )

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_INVITE_RESENT,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user.id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteOperatorResponse(
            user_id=user.id, email=user.email, invite_url=invite_url, email_sent=email_sent
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k resend_operator -v`
Expected: PASS (once Task 10's validate endpoint exists).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/platform_users.py tests/integration/control_plane/test_platform_users.py
git commit -m "feat(platform): add resend-invitation endpoint for platform operators"
```

---

### Task 10: Platform pre-auth accept-invite flow (validate / accept / activate-mfa)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/platform_auth.py`
- Test: `tests/integration/control_plane/test_platform_users.py` (extend — full-flow test)

**Interfaces:**
- Consumes: `create_password_identity`, `set_operator_status` (Task 4); `PLATFORM_INVITE_NS`, `PLATFORM_INVITE_MFA_NS` (Task 3); existing `mfa.enroll_platform`, `mfa.activate_platform` helpers.
- Produces: `GET /platform/auth/invitations/validate`, `POST /platform/auth/invitations/accept`, `POST /platform/auth/invitations/activate-mfa`.

- [ ] **Step 1: Write the failing full-flow test**

```python
# In tests/integration/control_plane/test_platform_users.py
import pyotp


async def test_full_platform_invite_accept_activate_flow(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "flow-op@test.example", "name": "Flow Op", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    valid = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": token}
    )
    assert valid.json()["data"]["state"] == "valid"

    accept = await client.post(
        "/api/v1/platform/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text
    accept_body = accept.json()["data"]
    assert accept_body["mfa_required"] is True
    assert accept_body["provisioning_uri"] is not None
    mfa_token = accept_body["mfa_token"]

    # Extract the TOTP secret from the provisioning URI to compute a live code.
    secret = pyotp.parse_uri(accept_body["provisioning_uri"]).secret
    code = pyotp.TOTP(secret).now()

    activate = await client.post(
        "/api/v1/platform/auth/invitations/activate-mfa",
        json={"mfa_token": mfa_token, "code": code},
    )
    assert activate.status_code == 200, activate.text

    # The token is single-use — replaying validate now shows "invalid" (accepted).
    revalidate = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": token}
    )
    assert revalidate.json()["data"]["state"] == "invalid"

    listed = await client.get("/api/v1/platform/users", headers=_auth(pw.super_admin_token))
    row = next(r for r in listed.json()["data"] if r["id"] == user_id)
    assert row["status"] == "active"


async def test_platform_accept_invalid_token_is_unauthorized(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    # Only needs a client — reuses platform_world for that, ignoring its persona
    # data, rather than standing up a second app-construction fixture for one test.
    client, _pw = platform_world
    resp = await client.post(
        "/api/v1/platform/auth/invitations/accept",
        json={"token": "not-a-real-token", "password": "whatever-password"},
    )
    assert resp.status_code == 401, resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k platform_invite_accept_activate -v`
Expected: FAIL — 404 (routes don't exist).

- [ ] **Step 3: Add the endpoints**

In `apps/control_plane/src/control_plane/api/v1/platform_auth.py`, extend the imports:

```python
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
```

```python
from control_plane.api.v1.auth import (
    AcceptInviteRequest,
    AcceptInviteResponse,
    ActivateInviteMfaRequest,
    InviteValidateResponse,
    LoginRequest,
    LoginResponse,
    MfaEnrollActivateRequest,
    MfaVerifyRequest,
    SessionResponse,
    _load_password_creds,
    _password_identity_row,
    _unauthorized,
    raise_for_inactive,
)
from control_plane.api.v1.common import AppSettings, AuthAudit, Invites
from control_plane.auth import mfa
from control_plane.auth.invitations import PLATFORM_INVITE_MFA_NS, PLATFORM_INVITE_NS
from control_plane.auth.password import MAX_PASSWORD_BYTES, hash_password, verify_password_or_dummy
from control_plane.auth.platform_provisioning import create_password_identity, set_operator_status
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
)
from vera_core.models import AppUser
```

Add the three endpoints (after `_require_platform_challenge`, before `platform_login`):

```python
@router.get(
    "/invitations/validate",
    response_model=ResponseModel[InviteValidateResponse],
)
async def validate_platform_invitation(
    token: str,
    response: Response,
    sessionmaker: Sessionmaker,
    invites: Invites,
) -> ResponseModel[InviteValidateResponse]:
    """Token-scoped invite pre-flight for a platform operator — no tenant slug, the
    invitee belongs to no tenant. Mirrors validate_invitation (auth.py)."""
    response.headers["Cache-Control"] = "no-store"
    invite = await invites.get(PLATFORM_INVITE_NS, token)
    if invite is None or invite.tenant_id is not None:
        return ok(InviteValidateResponse(state="invalid"))

    async with platform_session(sessionmaker) as session:
        row = (
            await session.execute(select(AppUser.status).where(AppUser.id == invite.app_user_id))
        ).one_or_none()

    if row is None:
        return ok(InviteValidateResponse(state="invalid"))
    if row.status == "invited":
        return ok(InviteValidateResponse(state="valid"))
    if row.status == "deactivated":
        return ok(InviteValidateResponse(state="deactivated"))
    return ok(InviteValidateResponse(state="invalid"))


@router.post(
    "/invitations/accept",
    response_model=ResponseModel[AcceptInviteResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def accept_platform_invitation(
    body: AcceptInviteRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
    settings: AppSettings,
) -> ResponseModel[AcceptInviteResponse]:
    """Unauthenticated, token-gated: a platform invitee sets their password. MFA is
    ALWAYS required for platform operators — no enforce_mfa branch, unlike the
    tenant flow — this always returns a provisioning URI + bridge mfa_token and
    leaves status "invited" until activate-mfa. Single-use (token consumed here)."""
    invite = await invites.get(PLATFORM_INVITE_NS, body.token)
    if invite is None or invite.tenant_id is not None:
        raise _unauthorized()
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        raise BadRequestError(message="password too long")

    async with platform_session(sessionmaker) as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == invite.app_user_id))
        ).scalar_one_or_none()
        if user is None or user.status != "invited":
            raise _unauthorized()
        if await _password_identity_row(session, user.id) is not None:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT, message="invitation already accepted"
            )
        await create_password_identity(
            session,
            app_user_id=user.id,
            email=invite.email,
            hashed_password=hash_password(body.password),
        )
        identity = await _password_identity_row(session, user.id)
        assert identity is not None  # just created above, in the same transaction
        provisioning_uri = await mfa.enroll_platform(
            kms, session, identity=identity, account_email=invite.email
        )

    await invites.delete(PLATFORM_INVITE_NS, body.token)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_INVITE_ACCEPTED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    mfa_token = await invites.put(PLATFORM_INVITE_MFA_NS, invite, settings.invite_ttl_seconds)
    return ok(
        AcceptInviteResponse(
            mfa_required=True, provisioning_uri=provisioning_uri, mfa_token=mfa_token
        )
    )


@router.post(
    "/invitations/activate-mfa",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def activate_platform_invitation_mfa(
    body: ActivateInviteMfaRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
) -> ResponseModel[None]:
    """Completes MFA enrollment for a platform invitee, flipping status to active.
    No recovery codes are returned — platform MFA is TOTP-only everywhere in this
    codebase (see mfa.py module docstring); consuming a recovery code would need
    yet another definer write on an already-enrolled row."""
    invite = await invites.get(PLATFORM_INVITE_MFA_NS, body.mfa_token)
    if invite is None or invite.tenant_id is not None:
        raise _unauthorized()

    async with platform_session(sessionmaker) as session:
        ident = await _password_identity_row(session, invite.app_user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        activated = await mfa.activate_platform(kms, session, identity=ident, code=body.code)
        if not activated:
            raise BadRequestError(message="invalid code")
        flipped = await set_operator_status(
            session, app_user_id=invite.app_user_id, status="active"
        )
        if not flipped:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT, message="could not activate operator"
            )

    await invites.delete(PLATFORM_INVITE_MFA_NS, body.mfa_token)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_ACTIVATED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    return ok(None, message="Platform operator activated.")
```

Also add `from vera_core.audit import emit_auth_event` and `from control_plane.responses import ResponseModel, ok` to the imports if not already present (they already are, per the existing file), and add `AuthEvent` (already imported).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -v`
Expected: PASS on the full-flow test and the invalid-token test. Also re-run Task 9's resend test now that `/platform/auth/invitations/validate` exists.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/platform_auth.py tests/integration/control_plane/test_platform_users.py
git commit -m "feat(platform): add platform accept-invite flow with mandatory MFA activation"
```

---

### Task 11: Session-shape enforcement + RBAC invariant tests

**Files:**
- Test: `tests/integration/control_plane/test_platform_users.py` (extend)
- Test: existing RBAC invariant test file (search for the current test that asserts `platform:*` permissions can never attach to a tenant role — likely near `roles_grant_platform_permission`'s tests — extend it with the two new permission codes)

**Interfaces:**
- Consumes: everything from Tasks 6–10.

- [ ] **Step 1: Write the failing tests**

```python
# In tests/integration/control_plane/test_platform_users.py
async def test_elevated_tenant_session_cannot_reach_platform_user_endpoints(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,  # the existing tenant-tier fixture from conftest.py
) -> None:
    """A tenant admin who elevates into break-glass access must not be able to
    reach the platform-operator endpoints — these require an actual platform
    session (no tenant GUC), not an elevated tenant one."""
    resp = await client.get(
        "/api/v1/platform/users", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code in (401, 403), resp.text
```

For the RBAC invariant extension, find the existing test (likely named something like `test_platform_permission_cannot_be_granted_to_tenant_role` — search `tests/` for `roles_grant_platform_permission` usages in test files) and add `platform:users:invite`/`platform:users:read` to whatever parametrized list or explicit assertion it already runs, following that test's existing style exactly (do not invent a new test file for this — extend the existing one).

- [ ] **Step 2: Run test to verify it fails or passes for the right reason**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_platform_users.py -k elevated_tenant_session -v`
Expected: this should already PASS if the endpoints correctly gate on `platform_require`/`platform_scoped_session` (which only recognize `account_type='platform'` identities) — a tenant admin's token, even elevated, carries `account_type='tenant'`, so `platform_require`'s permission resolution over `platform_scoped_session` won't find any grant. If it unexpectedly passes with a 200, that's a real bug to fix in Tasks 7–10's route dependencies before proceeding — don't treat an unexpected pass as success.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/control_plane/test_platform_users.py <the extended RBAC invariant test file>
git commit -m "test(platform): verify elevated tenant sessions can't reach platform-user endpoints"
```

---

### Task 12: Frontend API clients

**Files:**
- Modify: `src/lib/auth/api.ts`
- Modify: `src/lib/api/platform.ts`
- Test: `src/lib/auth/api.test.ts` (create)
- Test: `src/lib/api/platform.test.ts` (create)

**Interfaces:**
- Produces: `resendInvitation(userId: string)`; `platformValidateInvite(token: string)`, `platformAcceptInvite(token: string, password: string)`, `platformActivateInviteMfa(mfaToken: string, code: string)`; `listOperators()`, `inviteOperator(input)`, `deactivateOperator(id)`, `resendOperatorInvitation(id)`.

- [ ] **Step 1: Write the failing tests**

```ts
// src/lib/auth/api.test.ts
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    httpStatus: number
    errorCode: string | null
    constructor(httpStatus: number, errorCode: string | null, message: string) {
      super(message)
      this.name = "ApiError"
      this.httpStatus = httpStatus
      this.errorCode = errorCode
    }
  }
  return { apiRequest: vi.fn(), ApiError, randomId: () => "test-idempotency-key" }
})

import { apiRequest } from "@/lib/api/client"
import {
  platformAcceptInvite,
  platformActivateInviteMfa,
  platformValidateInvite,
  resendInvitation,
} from "./api"

describe("auth api client — resend and platform invite", () => {
  beforeEach(() => vi.resetAllMocks())

  it("resends a tenant invitation with the conventional Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await resendInvitation("user-1")
    expect(apiRequest).toHaveBeenCalledWith("/users/user-1/invitations/resend", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("validates a platform invite with no tenant slug", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ state: "valid" })
    await platformValidateInvite("tok123")
    expect(apiRequest).toHaveBeenCalledWith(
      "/platform/auth/invitations/validate?token=tok123",
      { method: "GET", auth: false },
    )
  })

  it("accepts a platform invite", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ mfa_required: true })
    await platformAcceptInvite("tok123", "a-strong-password")
    expect(apiRequest).toHaveBeenCalledWith("/platform/auth/invitations/accept", {
      method: "POST",
      body: { token: "tok123", password: "a-strong-password" },
      auth: false,
    })
  })

  it("activates platform invite MFA", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await platformActivateInviteMfa("mfa-tok", "123456")
    expect(apiRequest).toHaveBeenCalledWith("/platform/auth/invitations/activate-mfa", {
      method: "POST",
      body: { mfa_token: "mfa-tok", code: "123456" },
      auth: false,
    })
  })
})
```

```ts
// src/lib/api/platform.test.ts
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    httpStatus: number
    errorCode: string | null
    constructor(httpStatus: number, errorCode: string | null, message: string) {
      super(message)
      this.name = "ApiError"
      this.httpStatus = httpStatus
      this.errorCode = errorCode
    }
  }
  return { apiRequest: vi.fn(), ApiError, randomId: () => "test-idempotency-key" }
})

import { apiRequest } from "@/lib/api/client"
import { deactivateOperator, inviteOperator, listOperators, resendOperatorInvitation } from "./platform"

describe("platform api client — operators", () => {
  beforeEach(() => vi.resetAllMocks())

  it("lists platform operators", async () => {
    vi.mocked(apiRequest).mockResolvedValue([])
    await listOperators()
    expect(apiRequest).toHaveBeenCalledWith("/platform/users")
  })

  it("invites a platform operator with an Idempotency-Key", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await inviteOperator({ email: "a@b.com", name: "A", sendEmail: true })
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/invitations", {
      method: "POST",
      body: { email: "a@b.com", name: "A", send_email: true },
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })

  it("deactivates a platform operator", async () => {
    vi.mocked(apiRequest).mockResolvedValue(null)
    await deactivateOperator("op-1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/op-1/deactivate", { method: "POST" })
  })

  it("resends a platform operator invitation", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await resendOperatorInvitation("op-1")
    expect(apiRequest).toHaveBeenCalledWith("/platform/users/op-1/resend-invitation", {
      method: "POST",
      headers: { "Idempotency-Key": "test-idempotency-key" },
    })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npm test -- src/lib/auth/api.test.ts src/lib/api/platform.test.ts`
Expected: FAIL — the new functions don't exist yet.

- [ ] **Step 3: Add the functions**

In `src/lib/auth/api.ts`, add near `deactivateUser`:

```ts
/** Reissue a fresh invite link for a user stuck in status="invited" (their
 *  original link or MFA bridge token expired). Requires `users:manage`. */
export function resendInvitation(userId: string) {
  return apiRequest<null>(`/users/${encodeURIComponent(userId)}/invitations/resend`, {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
  })
}

// --- Platform-operator invite acceptance: no tenant slug (the invitee belongs to
// no tenant). MFA is always required (see PlatformAcceptInvite). ---

export function platformValidateInvite(token: string) {
  return apiRequest<InviteValidateResult>(
    `/platform/auth/invitations/validate?token=${encodeURIComponent(token)}`,
    { method: "GET", auth: false },
  )
}

export function platformAcceptInvite(token: string, password: string) {
  return apiRequest<AcceptInviteResult>(`/platform/auth/invitations/accept`, {
    method: "POST",
    body: { token, password },
    auth: false,
  })
}

/** No recovery codes on this path — platform MFA is TOTP-only everywhere. */
export function platformActivateInviteMfa(mfaToken: string, code: string) {
  return apiRequest<null>(`/platform/auth/invitations/activate-mfa`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}
```

Note: `resendInvitation`'s path above is `/users/{id}/invitations/resend` — reconcile this against the ACTUAL backend route wired in Task 6, which is `POST /users/{user_id}/resend-invitation`. **Use `/users/${encodeURIComponent(userId)}/resend-invitation`** to match the backend exactly (the snippet above has the segments in the wrong order — fix this before running Step 4; this note exists precisely so the mismatch gets caught here rather than at integration time).

In `src/lib/api/platform.ts`, add:

```ts
export type Operator = {
  id: string
  email: string
  name: string
  /** "invited" | "active" | "deactivated" */
  status: string
  last_login_at: string | null
}

export type InviteOperatorInput = {
  email: string
  name: string
  sendEmail: boolean
}

export type InviteOperatorResult = {
  user_id: string
  email: string
  invite_url: string
  email_sent: boolean
}

/** List all platform operators. Requires `platform:users:read`. */
export function listOperators() {
  return apiRequest<Operator[]>("/platform/users")
}

/** Invite a new platform operator (always granted SUPER_ADMIN). Requires
 *  `platform:users:invite`. */
export function inviteOperator(input: InviteOperatorInput) {
  return apiRequest<InviteOperatorResult>("/platform/users/invitations", {
    method: "POST",
    body: { email: input.email, name: input.name, send_email: input.sendEmail },
    headers: { "Idempotency-Key": randomId() },
  })
}

/** Deactivate a platform operator. Requires `platform:users:invite`. Blocked
 *  (409) if this would leave zero active operators. */
export function deactivateOperator(id: string) {
  return apiRequest<null>(`/platform/users/${encodeURIComponent(id)}/deactivate`, {
    method: "POST",
  })
}

/** Reissue a fresh invite link for an operator stuck in status="invited".
 *  Requires `platform:users:invite`. */
export function resendOperatorInvitation(id: string) {
  return apiRequest<InviteOperatorResult>(
    `/platform/users/${encodeURIComponent(id)}/resend-invitation`,
    { method: "POST", headers: { "Idempotency-Key": randomId() } },
  )
}
```

`platform.ts` needs `randomId` added to its existing `import { apiRequest } from "@/lib/api/client"` line: `import { apiRequest, randomId } from "@/lib/api/client"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-frontend && npm test -- src/lib/auth/api.test.ts src/lib/api/platform.test.ts`
Expected: PASS (after fixing the `resendInvitation` path per the note in Step 3).

- [ ] **Step 5: Commit**

```bash
git add src/lib/auth/api.ts src/lib/api/platform.ts src/lib/auth/api.test.ts src/lib/api/platform.test.ts
git commit -m "feat(frontend): add resend-invitation and platform-operator API clients"
```

---

### Task 13: Tenant Users page — add Resend invitation action

**Files:**
- Modify: `src/pages/Users.tsx`

**Interfaces:**
- Consumes: `resendInvitation` (Task 12).

- [ ] **Step 1: Add resend state and handler**

In `src/pages/Users.tsx`, add alongside the existing `pending`/`busy`/`dialogError` state (near line 37-39):

```tsx
const [resendingId, setResendingId] = useState<string | null>(null)
```

Add the import: `import { deactivateUser, listUsers, resendInvitation, type UserSummary } from "@/lib/auth/api"`.

Add the handler function (near `confirmDeactivate`):

```tsx
async function onResend(user: UserSummary) {
  setResendingId(user.id)
  setError(null)
  try {
    await resendInvitation(user.id)
    setNotice(`A fresh invite link was sent to ${user.name || user.email}.`)
  } catch (err) {
    setError(err instanceof ApiError ? err.message : "Could not resend the invitation.")
  } finally {
    setResendingId(null)
  }
}
```

- [ ] **Step 2: Add the button to the row-action cell**

In the `<TableCell>` at line 187-202 (the `canManage && (...)` block), add a resend button before the existing deactivate button, conditioned on invited status:

```tsx
{canManage && (
  <TableCell>
    {u.status === "invited" && (
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2 mr-1"
        onClick={() => onResend(u)}
        disabled={resendingId === u.id}
      >
        {resendingId === u.id ? "Resending…" : "Resend invitation"}
      </Button>
    )}
    {u.status !== "deactivated" && (
      <Button
        variant="ghost"
        size="sm"
        className="text-destructive hover:text-destructive"
        onClick={() => askDeactivate(u)}
      >
        Deactivate
      </Button>
    )}
  </TableCell>
)}
```

(Drop the original `-ml-2` from the Deactivate button since Resend now owns the left-alignment offset when both render; keep it on Deactivate alone via a conditional class if both need independent alignment — verify visually once the dev server is running in Task 16.)

- [ ] **Step 3: Manual verification**

This is a UI change without an automated interaction test (no `@testing-library/react` in this repo — see Global Constraints). Verify visually once the frontend dev server is running (Task 16 covers running the app end-to-end); do not skip this — a compile-clean component is not the same as a working button.

- [ ] **Step 4: Commit**

```bash
git add src/pages/Users.tsx
git commit -m "feat(users): add resend-invitation action to the tenant Users page"
```

---

### Task 14: Invite Platform Operator dialog + Platform Operators page

**Files:**
- Create: `src/components/platform/InvitePlatformOperatorDialog.tsx`
- Create: `src/pages/PlatformOperators.tsx`
- Test: `src/pages/PlatformOperators.test.tsx`

**Interfaces:**
- Consumes: `listOperators`, `inviteOperator`, `deactivateOperator`, `resendOperatorInvitation` (Task 12).
- Produces: `<PlatformOperators />` page component; `<InvitePlatformOperatorDialog onInvited?={() => void} />`.

- [ ] **Step 1: Write the failing rendering test**

```tsx
// src/pages/PlatformOperators.test.tsx
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { InvitePlatformOperatorDialog } from "@/components/platform/InvitePlatformOperatorDialog"

describe("InvitePlatformOperatorDialog", () => {
  it("renders an Invite operator trigger button", () => {
    const html = renderToStaticMarkup(<InvitePlatformOperatorDialog />)
    expect(html).toContain("Invite operator")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-frontend && npm test -- src/pages/PlatformOperators.test.tsx`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write `InvitePlatformOperatorDialog.tsx`**

Modeled directly on `src/components/users/InviteUserDialog.tsx`, dropping the role-select field (a platform invite always grants `SUPER_ADMIN` — no role picker needed, per the approved design):

```tsx
// src/components/platform/InvitePlatformOperatorDialog.tsx
import { useEffect, useState, type FormEvent } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { inviteOperator, type InviteOperatorResult } from "@/lib/api/platform"
import { copyText } from "@/lib/clipboard"

export function InvitePlatformOperatorDialog({ onInvited }: { onInvited?: () => void } = {}) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [sendEmail, setSendEmail] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<InviteOperatorResult | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  async function copyLink() {
    if (!result) return
    setCopied(await copyText(result.invite_url))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await inviteOperator({ email, name, sendEmail })
      setResult(res)
      onInvited?.()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send invitation.")
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setOpen(false)
    setEmail("")
    setName("")
    setSendEmail(true)
    setError(null)
    setResult(null)
    setBusy(false)
    setCopied(false)
  }

  const submitLabel = sendEmail ? "Send invitation" : "Create invitation"
  const submitBusyLabel = sendEmail ? "Sending…" : "Creating…"

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : reset())}>
      <DialogTrigger asChild>
        <Button>Invite operator</Button>
      </DialogTrigger>
      <DialogContent showCloseButton className="max-w-md gap-0 p-0">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle className="text-base font-semibold">Invite a platform operator</DialogTitle>
          <DialogDescription>
            They'll get a link to set a password and enroll two-factor authentication.
            Every platform operator is granted full super-admin access.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <>
            <div className="space-y-4 p-5">
              <p className="text-sm">
                Invitation created for <span className="font-medium">{result.email}</span>
                {result.email_sent ? " and emailed." : "."}
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="operator-invite-url">Invite link</Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="operator-invite-url"
                    readOnly
                    value={result.invite_url}
                    onFocus={(e) => e.target.select()}
                    className="font-mono text-xs"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={copyLink}
                    aria-label={copied ? "Link copied" : "Copy invite link"}
                    title={copied ? "Copied" : "Copy"}
                  >
                    {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                  </Button>
                </div>
              </div>
            </div>
            <div className="flex justify-end border-t border-border p-4">
              <Button onClick={reset} className="min-w-[120px]">Done</Button>
            </div>
          </>
        ) : (
          <form onSubmit={onSubmit}>
            <div className="space-y-4 p-5">
              <div className="space-y-1.5">
                <Label htmlFor="operator-invite-email">Email</Label>
                <Input
                  id="operator-invite-email"
                  type="email"
                  required
                  autoFocus
                  placeholder="operator@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="operator-invite-name">Name</Label>
                <Input
                  id="operator-invite-name"
                  placeholder="Jane Doe"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Checkbox
                  id="operator-invite-send-email"
                  checked={sendEmail}
                  onCheckedChange={(checked) => setSendEmail(checked === true)}
                />
                <Label htmlFor="operator-invite-send-email" className="font-normal">
                  Send invitation email
                </Label>
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button type="submit" disabled={busy} className="min-w-[120px]">
                {busy ? submitBusyLabel : submitLabel}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-frontend && npm test -- src/pages/PlatformOperators.test.tsx`
Expected: PASS.

- [ ] **Step 5: Write `PlatformOperators.tsx`**

Modeled on `src/pages/Users.tsx`'s table structure and `src/pages/TenantAccess.tsx`'s self-guard pattern:

```tsx
// src/pages/PlatformOperators.tsx
import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { InvitePlatformOperatorDialog } from "@/components/platform/InvitePlatformOperatorDialog"
import { ApiError } from "@/lib/api/client"
import {
  deactivateOperator, listOperators, resendOperatorInvitation, type Operator,
} from "@/lib/api/platform"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline"> = {
  active: "default",
  invited: "secondary",
  deactivated: "outline",
}

export function PlatformOperators() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [operators, setOperators] = useState<Operator[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [resendingId, setResendingId] = useState<string | null>(null)

  const [pending, setPending] = useState<Operator | null>(null)
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setOperators(await listOperators())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load platform operators.")
    }
  }, [])

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listOperators()
      .then((ops) => {
        if (!cancelled) setOperators(ops)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load platform operators.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 5000)
    return () => clearTimeout(timer)
  }, [notice])

  if (!isSuperAdmin) {
    return (
      <p className="text-sm text-muted-foreground">
        This page is only available to platform operators.
      </p>
    )
  }

  const activeCount = operators?.filter((o) => o.status === "active").length ?? 0

  async function onResend(operator: Operator) {
    setResendingId(operator.id)
    setError(null)
    try {
      await resendOperatorInvitation(operator.id)
      setNotice(`A fresh invite link was sent to ${operator.name || operator.email}.`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resend the invitation.")
    } finally {
      setResendingId(null)
    }
  }

  function askDeactivate(operator: Operator) {
    setNotice(null)
    setDialogError(null)
    setPending(operator)
  }

  function closeDialog() {
    if (busy) return
    setPending(null)
    setDialogError(null)
  }

  async function confirmDeactivate() {
    if (!pending) return
    const label = pending.name || pending.email
    setBusy(true)
    setDialogError(null)
    try {
      await deactivateOperator(pending.id)
      setPending(null)
      await load()
      setNotice(`${label} has been deactivated.`)
    } catch (err) {
      setDialogError(err instanceof ApiError ? err.message : "Could not deactivate operator.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Platform Operators</h1>
          <p className="text-sm text-muted-foreground">
            {operators ? `${activeCount} active` : "Loading…"} — every operator holds full
            super-admin access.
          </p>
        </div>
        <InvitePlatformOperatorDialog onInvited={load} />
      </div>

      {notice && <p className="text-sm text-emerald-600 dark:text-emerald-400" role="status">{notice}</p>}
      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {operators === null && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {operators?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No platform operators yet.
                </TableCell>
              </TableRow>
            )}
            {operators?.map((o) => {
              const isLastActive = o.status === "active" && activeCount <= 1
              return (
                <TableRow key={o.id}>
                  <TableCell className="font-medium">{o.name || "—"}</TableCell>
                  <TableCell>{o.email}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[o.status] ?? "outline"} className="capitalize">
                      {o.status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {o.status === "invited" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="-ml-2 mr-1"
                        onClick={() => onResend(o)}
                        disabled={resendingId === o.id}
                      >
                        {resendingId === o.id ? "Resending…" : "Resend invitation"}
                      </Button>
                    )}
                    {o.status !== "deactivated" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:text-destructive"
                        onClick={() => askDeactivate(o)}
                        disabled={isLastActive}
                        title={isLastActive ? "Cannot deactivate the last active operator" : undefined}
                      >
                        Deactivate
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <Dialog open={pending !== null} onOpenChange={(o) => (o ? undefined : closeDialog())}>
        <DialogContent showCloseButton={!busy} className="max-w-sm gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">Deactivate operator?</DialogTitle>
            <DialogDescription>
              {pending && (
                <>
                  <span className="font-medium text-foreground">{pending.name || pending.email}</span>{" "}
                  will lose access immediately and won't be able to sign in.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p className="px-5 pb-1 text-sm text-destructive" role="alert">{dialogError}</p>
          )}
          <div className="flex justify-end gap-3 border-t border-border p-4">
            <Button variant="outline" onClick={closeDialog} disabled={busy}>Cancel</Button>
            <Button
              variant="destructive"
              onClick={confirmDeactivate}
              disabled={busy}
              className="min-w-[120px]"
            >
              {busy ? "Deactivating…" : "Deactivate"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
```

Note: the deactivate confirmation dialog is reachable via `askDeactivate`, which the row Button calls even when `isLastActive` is true visually — but the button itself is `disabled` in that case, so the click never fires. This mirrors the pattern from `TenantAccess.tsx`/`Users.tsx` of disabling at the source rather than double-guarding in the handler; the backend's 409 lockout guard (Task 8) remains the authoritative enforcement regardless.

- [ ] **Step 6: Add a rendering test for the page**

Add to `src/pages/PlatformOperators.test.tsx`:

```tsx
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { PlatformOperators } from "@/pages/PlatformOperators"

// PlatformOperators reads Redux state via useAppSelector — wrap with the same
// minimal store-provider pattern this repo's other page-level tests use (check
// how src/components/agent-prompt/componentTests.test.tsx wraps a connected
// component, if it does; otherwise construct a real configureStore() with the
// authSlice reducer and a pre-seeded isSuperAdmin=false state for this smoke test).
describe("PlatformOperators", () => {
  it("renders the platform-only guard message for a non-super-admin", () => {
    // Render with a store where isSuperAdmin is false — verifies the guard text
    // renders instead of the table, without needing to mock listOperators at all.
    const html = renderToStaticMarkup(<PlatformOperators />)
    expect(html).toContain("only available to platform operators")
  })
})
```

If `PlatformOperators` cannot render without a live Redux `Provider` in the test tree (it calls `useAppSelector`), wrap it in a minimal `<Provider store={configureStore({ reducer: { auth: authReducer } })}>` in the test, matching whatever store-wrapping convention this repo's existing connected-component tests already use — check for one before inventing a new wrapping pattern.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd vera-frontend && npm test -- src/pages/PlatformOperators.test.tsx`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/components/platform/InvitePlatformOperatorDialog.tsx src/pages/PlatformOperators.tsx src/pages/PlatformOperators.test.tsx
git commit -m "feat(frontend): add Platform Operators page and invite dialog"
```

---

### Task 15: Platform Accept Invite page + nav/route wiring

**Files:**
- Create: `src/pages/PlatformAcceptInvite.tsx`
- Modify: `src/App.tsx`
- Modify: `src/lib/nav.ts`
- Test: `src/pages/PlatformAcceptInvite.test.tsx`

**Interfaces:**
- Consumes: `platformValidateInvite`, `platformAcceptInvite`, `platformActivateInviteMfa` (Task 12).

- [ ] **Step 1: Write the failing rendering test**

```tsx
// src/pages/PlatformAcceptInvite.test.tsx
import { MemoryRouter } from "react-router-dom"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { PlatformAcceptInvite } from "@/pages/PlatformAcceptInvite"

describe("PlatformAcceptInvite", () => {
  it("renders the invalid-invitation state when no token is present", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/platform/accept-invite"]}>
        <PlatformAcceptInvite />
      </MemoryRouter>,
    )
    expect(html).toContain("Invalid invitation")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-frontend && npm test -- src/pages/PlatformAcceptInvite.test.tsx`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write `PlatformAcceptInvite.tsx`**

Modeled directly on `src/pages/AcceptInvite.tsx`, with no tenant slug and no `deactivated`-vs-`enforce_mfa` branch (MFA is always required, so there is no plain "done" state reachable without it, and no recovery-codes state — platform MFA never issues recovery codes):

```tsx
// src/pages/PlatformAcceptInvite.tsx
import { useState, type FormEvent, useEffect } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import {
  platformAcceptInvite, platformActivateInviteMfa, platformValidateInvite,
} from "@/lib/auth/api"

type Phase =
  | { kind: "checking" }
  | { kind: "invalid" }
  | { kind: "deactivated" }
  | { kind: "password" }
  | { kind: "mfa"; mfaToken: string; provisioningUri: string | null }
  | { kind: "done" }

export function PlatformAcceptInvite() {
  const [params] = useSearchParams()
  const token = params.get("token") ?? ""
  const navigate = useNavigate()

  const [phase, setPhase] = useState<Phase>(() => (token ? { kind: "checking" } : { kind: "invalid" }))
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const loginHref = "/platform/login"

  useEffect(() => {
    if (!token) return
    let cancelled = false
    platformValidateInvite(token)
      .then((res) => {
        if (cancelled) return
        if (res.state === "valid") {
          setPhase({ kind: "password" })
        } else if (res.state === "deactivated") {
          setPhase({ kind: "deactivated" })
        } else {
          setPhase({ kind: "invalid" })
        }
      })
      .catch(() => {
        if (!cancelled) setPhase({ kind: "invalid" })
      })
    return () => {
      cancelled = true
    }
  }, [token])

  async function onSetPassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await platformAcceptInvite(token, password)
      setPhase({ kind: "mfa", mfaToken: res.mfa_token ?? "", provisioningUri: res.provisioning_uri })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "This invitation is invalid or has expired.")
    } finally {
      setBusy(false)
    }
  }

  async function onActivateMfa(e: FormEvent) {
    e.preventDefault()
    if (phase.kind !== "mfa") return
    setError(null)
    setBusy(true)
    try {
      await platformActivateInviteMfa(phase.mfaToken, code)
      setPhase({ kind: "done" })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Activation failed.")
    } finally {
      setBusy(false)
    }
  }

  if (phase.kind === "checking") {
    return (
      <CenteredCard title="Checking invitation…" desc="Please wait a moment.">
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
      </CenteredCard>
    )
  }

  if (phase.kind === "invalid") {
    return (
      <CenteredCard title="Invalid invitation" desc="This invite link is missing, invalid, or has expired.">
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "deactivated") {
    return (
      <CenteredCard
        title="Account deactivated"
        desc="This account has been deactivated. Please contact another platform operator."
      >
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "done") {
    return (
      <CenteredCard title="Account active" desc="Your platform operator account is ready.">
        <Button className="w-full" onClick={() => navigate(loginHref, { replace: true })}>Sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "mfa") {
    return (
      <CenteredCard title="Set up two-factor" desc="Scan the QR code, then enter a code to finish. Two-factor authentication is required for all platform operators.">
        <div className="space-y-4">
          {phase.provisioningUri && (
            <div className="flex justify-center rounded-md bg-white p-4">
              <QRCodeSVG value={phase.provisioningUri} size={180} />
            </div>
          )}
          <form onSubmit={onActivateMfa} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Authentication code</Label>
              <Input id="code" inputMode="numeric" autoComplete="one-time-code" required
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Activating…" : "Activate"}</Button>
          </form>
        </div>
      </CenteredCard>
    )
  }

  return (
    <CenteredCard title="Accept your invitation" desc="Choose a password to activate your platform operator account.">
      <form onSubmit={onSetPassword} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <PasswordInput id="password" autoComplete="new-password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Saving…" : "Set password"}</Button>
      </form>
    </CenteredCard>
  )
}

function CenteredCard({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">{title}</CardTitle>
          <CardDescription>{desc}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  )
}
```

- [ ] **Step 4: Wire the route**

In `src/App.tsx`, add the import and the route (alongside `/tenants/:tenantSlug/accept-invite`, outside `RequireAuth`/`AppShell`):

```tsx
import { PlatformAcceptInvite } from "@/pages/PlatformAcceptInvite"
import { PlatformOperators } from "@/pages/PlatformOperators"
```

```tsx
<Route path="/platform/accept-invite" element={<PlatformAcceptInvite />} />
```

And, inside the `AppShell` block (alongside `tenant-access`, `agent-prompt`, etc. — self-guarding, no `RequireNavRoute` wrapper, matching the existing super-admin-only page pattern):

```tsx
<Route path="platform-operators" element={<PlatformOperators />} />
```

- [ ] **Step 5: Wire the nav entry**

In `src/lib/nav.ts`, add to `navItems` (alongside the other `platform:*` entries):

```ts
{ title: "Platform Operators", to: "/platform-operators", icon: Users, permission: "platform:users:read" },
```

(`Users` icon is already imported in this file for the tenant "Users" nav item — reusing it here is fine, both represent people-management.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd vera-frontend && npm test -- src/pages/PlatformAcceptInvite.test.tsx`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/pages/PlatformAcceptInvite.tsx src/pages/PlatformAcceptInvite.test.tsx src/App.tsx src/lib/nav.ts
git commit -m "feat(frontend): add Platform Accept Invite page and wire routes/nav"
```

---

### Task 16: Run the app and verify end-to-end with playwright-cli

**Files:** none (verification only).

- [ ] **Step 1: Boot the stack**

Run: `cd vera-backend && just up && just migrate && LOCAL_KMS_MASTER_KEY=<generate per README> just bootstrap-platform` (env: `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` set to test values), then `just api` in one terminal.
Run: `cd vera-frontend && npm run dev` in another terminal.

- [ ] **Step 2: Playwright-cli — platform invite flow**

Use `playwright-cli` to drive the actual browser (not a headless test mock):
1. `playwright-cli open http://localhost:5173/platform/login`, sign in as the bootstrapped operator, complete MFA.
2. Navigate to `/platform-operators`. Confirm the page renders the operator list with the bootstrapped account shown `active`.
3. Click "Invite operator", fill in a test email, submit, capture the returned `invite_url` from the page.
4. Open the `invite_url` in a fresh browser context (`playwright-cli` new context / incognito), set a password.
5. Confirm the MFA QR step renders; extract the TOTP secret from the provisioning URI shown on the page (or via `read_page`/`get_page_text` if the URI itself is exposed in the DOM/query param), compute a live TOTP code (e.g. via a small local script using the same `pyotp`-equivalent logic, or a JS TOTP one-liner), submit it.
6. Confirm the "Account active" state renders.
7. Navigate back to the first context's `/platform-operators` page, refresh, confirm the new operator now shows `active`.

- [ ] **Step 3: Playwright-cli — resend flow**

1. Invite another test operator, capture the token from the invite_url.
2. In the backend Redis, manually expire/delete the token's key (`docker compose exec redis redis-cli DEL "vera:platform_invite:<sha256(token)>"` — or simply wait past a short TTL if the dev environment's `VERA_INVITE_TTL_SECONDS` is set low for this manual check) to simulate a stuck invitee.
3. Click "Resend invitation" on that row in `/platform-operators`.
4. Confirm the old link now shows "Invalid invitation" when opened, and the new link (captured from the resend response, or a second click surfacing it) proceeds through accept normally.

- [ ] **Step 4: Playwright-cli — lockout guard**

1. With only the bootstrapped operator remaining active (deactivate the test invitees created above first, or use a fresh DB), attempt to click "Deactivate" on the last active operator.
2. Confirm the button is disabled with the "Cannot deactivate the last active operator" tooltip, and/or that attempting the action surfaces the 409 error text if somehow triggered.

- [ ] **Step 5: Tenant-side resend verification**

1. Sign in as a tenant admin, navigate to `/users`, invite a test user, note the link.
2. Click "Resend invitation" on that row, confirm a fresh link is issued and the UI shows a success notice.

- [ ] **Step 6: Report findings**

If any step fails, fix the underlying code (not the test) and re-run from Step 1 of the failing flow. Do not mark this task done until all five verifications pass against the real running app.

---

### Task 17: Final checks — simplify, full test suites, commit

- [ ] **Step 1: Run the code-simplifier**

Invoke "simplify code" (the `code-simplifier` agent) on the full diff introduced by this feature — repo-root `CLAUDE.md` mandates this before claiming any non-trivial implementation done.

- [ ] **Step 2: Re-run backend checks**

Run: `cd vera-backend && just check`
Expected: PASS (ruff check + format --check + mypy --strict + pytest, verbatim — not a hand-picked subset).

- [ ] **Step 3: Re-run frontend checks**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS on all four.

- [ ] **Step 4: Final commit**

If the simplifier or the check re-runs produced any changes:

```bash
git add -A
git commit -m "refactor: simplify platform user invite feature per code-simplifier pass"
```

---

## Post-implementation

Once all 17 tasks are green, this feature closes the gap the original platform-operator-login plan explicitly deferred ("additional operators are added by re-running the bootstrap script until the invite-flow plan ships" — `docs/superpowers/plans/2026-06-19-platform-operator-login.md`). Consider updating that plan's docstring and `scripts/bootstrap_platform_admin.py`'s module docstring to point at this shipped feature instead of "separate plan" — a small follow-up cleanup, not part of this plan's task list, but worth flagging to the user once this lands.
