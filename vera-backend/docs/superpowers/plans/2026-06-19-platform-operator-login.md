# Platform Operator Login — Implementation Plan

> **STATUS: BLOCKED — do not execute yet.** Per decision (2026-06-19), this plan runs only **after both** `feat/mfa-db-secret-store` **and** `refactore/remove-tenant-slug-from-url-scope` are merged into `main`. On resume, first apply the rebase adjustments in "⚠️ Branch coordination — READ FIRST" below (drop Task 6, adapt Tasks 7/8 to the DB-backed `mfa` API, skip Tasks 1–2 if `account_type` already exists, renumber the migration to `0010`), then re-verify each task's file paths against the merged `main`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tenant-less login path for platform operators (SUPER_ADMIN, `account_type='platform'`, `tenant_id=NULL`) using local password + mandatory TOTP MFA, behind a provider seam so GCIP SSO can drop in later.

**Architecture:** Mirror the tenant login flow but tenant-less. A new `/api/v1/platform/auth/*` router authenticates against null-tenant `app_user`/`user_identity` rows inside a `platform_session` (the same RLS context the existing platform routes use), mints a `SessionData` with `account_type='platform'`, `tenant_id=None`, and gates it behind TOTP MFA. Provider config lives in a single global `platform_login_provider` row resolved by a `resolve_platform_login_provider()` function that returns the existing `LoginProvider` shape. Operator #1 is seeded by an idempotent bootstrap script (runs as the RLS-bypassing superuser, like `seed.py`).

**Tech Stack:** FastAPI, SQLAlchemy 2.x async, Alembic, Postgres RLS, Redis (opaque sessions), bcrypt, pyotp, pytest + pytest-asyncio.

**Scope:** This plan covers the **core login subsystem** only. The platform **invite flow** for ongoing operators (mirroring the tenant invite + MFA-on-invite flow, gated by a new `platform:users:invite` permission) is a separable subsystem deferred to its own follow-on plan. After this plan, operator #1 can log in and use the existing `/platform/elevations` routes; additional operators are added by re-running the bootstrap script until the invite-flow plan ships.

**Source spec:** `docs/superpowers/specs/2026-06-19-platform-operator-login-design.md`

## Global Constraints

These bind every task (copied from the spec + repo `CLAUDE.md` files):

- **`just check` must pass at the end of every task** — `lint` (ruff) + `typecheck` (mypy --strict) + `test` (pytest).
- **PEP 695 type params** (`class Foo[T]`, `def f[T]`); ruff rejects `Generic[T]`/`TypeVar`.
- **asyncio only** — no `import anyio`, no new `anyio` dependency; use stdlib `asyncio` primitives.
- **DB clock only** — every timestamp/audit/elevation time via Postgres `now()`/`func.now()`, never Python `datetime.now()`.
- **Errors in `api/v1` route code:** `raise CustomAPIException`/subclasses (`exceptions.py`), never `HTTPException`. The dependency layer (`deps.py`, `rbac.py`) already raises `HTTPException` — match each file's existing pattern; don't introduce a new style.
- **`account_type` (`'tenant'`/`'platform'`) is the definitive platform-vs-tenant signal.** Where you branch on plane, branch on `account_type` and assert `tenant_id` nullability as an invariant (fail closed) — never infer the plane from `tenant_id is None`.
- **Never log/trace/print PHI or credentials** (`hashed_password`, TOTP seed, recovery codes). Login failures return a uniform 401 (no user/provider enumeration).
- **Responses:** every endpoint returns `ResponseModel[T]` via `ok(...)`, declares `response_model=ResponseModel[T]` + `responses=CustomAPIResponse.custom(...)`, and sets `Cache-Control: no-store`.
- **Pre-launch, no data:** dev DB is wiped/recreated (`just up` + `just migrate`); no data backfill. Tables are materialized from `Base.metadata` by migration `0001`; new migrations add only RLS policies / ALTERs / seeds, never `create_table` for a model-backed table.

## ⚠️ Branch coordination — READ FIRST

This work overlaps **two** other in-flight branches. The plan below is written against `main`; depending on merge order, whole tasks drop or change. Confirm the sequencing decision (recorded by the author) before executing.

**1. `refactore/remove-tenant-slug-from-url-scope`** — introduces the **same** `AccountType` enum and `account_type` field on `SessionData`/`VerifiedIdentity` (its Tasks 1–2), plus the `tenant_context` resolver that *consumes* the null-tenant session this plan *produces*.
- If it has **already merged**: verify `AccountType` + `SessionData.account_type` exist and **SKIP Tasks 1–2**, starting at Task 3.
- If not: Tasks 1–2 here match its shape verbatim so the later merge is a trivial conflict.

**2. `feat/mfa-db-secret-store`** — **rewrites the MFA subsystem** and **collides directly** with this plan:
- New API: `mfa.enroll(kms, *, identity: UserIdentity, account_email)` / `mfa.verify(kms, *, identity, code)` / `mfa.activate(kms, *, identity, code)` — takes a `KeyManagementService` + the `UserIdentity` ORM row, **not** a `secret_ref`.
- MFA material moves **onto `user_identity`** (`totp_seed_ct`, `totp_dek_ct`, envelope-encrypted via new `config/kms.py`); **`mfa_secret_ref` is removed**.
- It uses migration **`0009_mfa_db_envelope.py`** — a **number collision** with this plan's `0009_platform_login.py`.
- It **solves a problem this plan otherwise has:** on `main`, MFA seeds live in a per-process `InMemorySecretProvider`, so a bootstrap *script* and the API server can't share them — mandatory-MFA platform login can't be exercised cross-process in dev. DB-stored seeds fix this.

**If `feat/mfa-db-secret-store` merges first (RECOMMENDED ordering), this plan changes:**
- **DROP Task 6** (`platform_mfa_secret_ref`) — no ref keyspace exists.
- **Task 7** loads the operator's `UserIdentity` row and calls `mfa.verify(kms, identity=row, code=...)` (inject the `KeyManagementService` from app state, as that branch wires it).
- **Task 8** calls `mfa.enroll(kms, identity=user_identity, ...)`, which writes the encrypted seed to the DB row the API later reads — no secret-provider factory, no cross-process hack.
- **Renumber** this plan's migration to **`0010_platform_login.py`** (`down_revision = "0009"`).

**Resolved during authoring (no longer open):**
- `user_role.tenant_id` **is already nullable** (`UserRole` uses `NullableTenantColumnMixin`) — the global SUPER_ADMIN grant to a null-tenant operator needs no schema change, and `user_role` is already in `PLATFORM_READABLE_TABLES`.

**Recommended sequencing:** land `feat/mfa-db-secret-store` first, then this plan (rebased per the bullets above), with the tenant-slug refactor independently. Rationale: the MFA branch deletes the very `mfa_secret_ref` API Tasks 6–8 are written against *and* removes the in-memory cross-process blocker — building on `main`'s MFA is throwaway.

## File Structure

| File | Responsibility | Tasks |
|------|----------------|-------|
| `packages/vera_core/src/vera_core/models/enums.py` | add `AccountType` enum | 1 |
| `apps/control_plane/src/control_plane/auth/session.py` | `account_type` on `SessionData` (+ json round-trip), surface in `verify` | 2 |
| `apps/control_plane/src/control_plane/auth/identity.py` | `account_type` on `VerifiedIdentity` | 2 |
| `apps/control_plane/src/control_plane/api/v1/auth.py` | load `account_type` in `_PasswordCreds`; set on tenant mint; `/me` reads from identity | 2 |
| `packages/vera_core/src/vera_core/models/auth.py` | `UserIdentity` → nullable tenant; new `PlatformLoginProvider` model | 3 |
| `packages/vera_core/src/vera_core/models/__init__.py` | export `PlatformLoginProvider` | 3 |
| `migrations/versions/0009_platform_login.py` | user_identity nullable + platform-readable policy; platform_login_provider policy + seed row | 4 |
| `apps/control_plane/src/control_plane/auth/providers.py` | `resolve_platform_login_provider()` | 5 |
| `apps/control_plane/src/control_plane/auth/mfa.py` | `platform_mfa_secret_ref()` | 6 |
| `apps/control_plane/src/control_plane/api/v1/platform_auth.py` | new `/platform/auth/*` router: login + mfa/verify + `_load_platform_password_creds` | 7 |
| `apps/control_plane/src/control_plane/api/v1/__init__.py` | mount the new router | 7 |
| `scripts/bootstrap_platform_admin.py` | idempotent operator-#1 bootstrap | 8 |
| `adr/0006-platform-runtime-and-elevation.md`, `adr/devops-todo.md` | record interim password login + last_login gap | 9 |

---

## Task 1: `AccountType` enum

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py`
- Test: `packages/vera_core/tests/test_enums.py` (create if absent)

**Interfaces:**
- Produces: `AccountType` StrEnum with members `TENANT = "tenant"`, `PLATFORM = "platform"`.

- [ ] **Step 1: Write the failing test**

Create or append to `packages/vera_core/tests/test_enums.py`:

```python
from vera_core.models.enums import AccountType


def test_account_type_values():
    assert AccountType.TENANT.value == "tenant"
    assert AccountType.PLATFORM.value == "platform"
    assert {a.value for a in AccountType} == {"tenant", "platform"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/vera_core/tests/test_enums.py::test_account_type_values -v`
Expected: FAIL with `ImportError: cannot import name 'AccountType'`.

- [ ] **Step 3: Add the enum**

In `enums.py`, add after the `ProviderKind` class (keep alphabetical-ish grouping with the other auth enums):

```python
class AccountType(enum.StrEnum):
    """Whether an app_user is a tenant member or a platform operator. The
    definitive platform-vs-tenant signal — branch on this, never on
    `tenant_id is None` (a session is minted in code, so tenant_id is only a
    structural proxy). A DB CHECK pairs it with tenant_id nullability at rest."""

    TENANT = "tenant"
    PLATFORM = "platform"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/vera_core/tests/test_enums.py::test_account_type_values -v`
Expected: PASS.

- [ ] **Step 5: Export it (if the package re-exports enums)**

Check how `ProviderKind`/`AuthEvent` are imported elsewhere — they are imported as `from vera_core.models.enums import ...`, so no `__init__` re-export is required. Confirm with: `grep -rn "from vera_core.models.enums import" packages apps | head`. No change needed if all imports go through `enums`.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/models/enums.py packages/vera_core/tests/test_enums.py
git commit -m "feat(models): add AccountType enum (tenant/platform)"
```

---

## Task 2: carry `account_type` through the session

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/identity.py`
- Modify: `apps/control_plane/src/control_plane/auth/session.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py` (`_PasswordCreds`, `_load_password_creds`, tenant login mint ~line 273, `/me` ~line 436)
- Test: `apps/control_plane/tests/test_session_data.py` (create), plus update any fixture building `SessionData`/`VerifiedIdentity`.

**Interfaces:**
- Consumes: `AccountType` (Task 1).
- Produces: `SessionData.account_type: str` (required field, emitted in `to_json`, read as a required key in `from_json`); `VerifiedIdentity.account_type: AccountType`; `_PasswordCreds.account_type: str`.

- [ ] **Step 1: Write the failing test**

Create `apps/control_plane/tests/test_session_data.py`:

```python
import pytest

from control_plane.auth.session import SessionData


def _sample(**over):
    base = dict(
        user_id=__import__("uuid").uuid4(),
        tenant_id=None,
        email="op@vera.example",
        subject="op@vera.example",
        provider_type="password",
        mfa_passed=True,
        account_type="platform",
    )
    base.update(over)
    return SessionData(**base)


def test_account_type_round_trips_through_json():
    data = _sample()
    assert SessionData.from_json(data.to_json()).account_type == "platform"


def test_from_json_requires_account_type():
    raw = _sample().to_json()
    import json

    payload = json.loads(raw)
    del payload["account_type"]
    with pytest.raises(KeyError):
        SessionData.from_json(json.dumps(payload))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/control_plane/tests/test_session_data.py -v`
Expected: FAIL — `SessionData.__init__` got an unexpected keyword `account_type` / missing field.

- [ ] **Step 3: Add `account_type` to `VerifiedIdentity`**

In `auth/identity.py`, add the field to the `VerifiedIdentity` dataclass (import `AccountType` from `vera_core.models.enums`):

```python
account_type: AccountType
```

Place it alongside the existing identity fields (e.g. after `email`). It is required — no default.

- [ ] **Step 4: Add `account_type` to `SessionData` + json**

In `auth/session.py`, `SessionData`:

```python
    user_id: UUID
    tenant_id: UUID | None
    email: str
    subject: str
    provider_type: str
    mfa_passed: bool
    account_type: str
    tenant_slug: str | None = None
```

In `to_json`, add `"account_type": self.account_type,` to the dict. In `from_json`, add `account_type=d["account_type"],` (a **required** key — no `.get`, so a legacy payload raises). Update the `_ABS_SENTINEL` construction (~line 99) to pass `account_type=""`.

In `SessionVerifier.verify` (~line 248) pass it into `VerifiedIdentity`:

```python
        return VerifiedIdentity(
            user_id=data.user_id,
            subject=data.subject,
            email=data.email,
            account_type=AccountType(data.account_type),
            tenant_id=data.tenant_id,
            tenant_slug=data.tenant_slug,
        )
```

Import `AccountType` in `session.py`.

- [ ] **Step 5: Thread `account_type` through tenant login**

In `api/v1/auth.py`:
- Add `account_type: str` to `_PasswordCreds`.
- In `_load_password_creds`, add `AppUser.account_type` to the `select(...)` and set `account_type=row.account_type` in the returned `_PasswordCreds`.
- In the tenant login mint (`base = SessionData(...)`, ~line 273) add `account_type=creds.account_type,`.
- In `/me` (~line 436), read `account_type` from `identity` (now `identity.account_type.value`) and drop `AppUser.account_type` from the `select` (still fetch `name`). Set `account_type=identity.account_type.value` on `MeResponse`.

- [ ] **Step 6: Update existing fixtures/helpers**

Run: `grep -rn "SessionData(\|VerifiedIdentity(" apps/control_plane/tests apps/control_plane/src | grep -v "_ABS_SENTINEL"`
For every constructor hit, add `account_type="tenant"` (or `AccountType.TENANT` for `VerifiedIdentity`) unless the test is specifically about a platform operator. These are existing tenant-flow fixtures.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest apps/control_plane/tests/test_session_data.py -v && just check`
Expected: PASS; `just check` green (the new required field is threaded everywhere).

- [ ] **Step 8: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/session.py \
        apps/control_plane/src/control_plane/auth/identity.py \
        apps/control_plane/src/control_plane/api/v1/auth.py \
        apps/control_plane/tests
git commit -m "feat(auth): carry account_type through SessionData + VerifiedIdentity"
```

---

## Task 3: nullable-tenant `user_identity` + `PlatformLoginProvider` model

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/auth.py`
- Modify: `packages/vera_core/src/vera_core/models/__init__.py`
- Test: `packages/vera_core/tests/test_models_platform_login.py` (create)

**Interfaces:**
- Produces: `UserIdentity` with `tenant_id: UUID | None`; new `PlatformLoginProvider` model (`__tablename__ = "platform_login_provider"`) with `provider_type: str`, `enabled: bool`, `enforce_mfa: bool`, nullable `tenant_id` (always NULL — global config).

- [ ] **Step 1: Write the failing test**

Create `packages/vera_core/tests/test_models_platform_login.py`:

```python
from vera_core.models import PlatformLoginProvider, UserIdentity


def test_user_identity_tenant_id_is_nullable():
    assert UserIdentity.__table__.c.tenant_id.nullable is True


def test_platform_login_provider_columns():
    cols = PlatformLoginProvider.__table__.c
    assert cols.tenant_id.nullable is True
    assert "provider_type" in cols
    assert "enabled" in cols
    assert "enforce_mfa" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/vera_core/tests/test_models_platform_login.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlatformLoginProvider'`.

- [ ] **Step 3: Make `UserIdentity` nullable-tenant**

In `models/auth.py`, change `UserIdentity`'s base from `TenantScopedMixin` to the explicit composition (because `TenantScopedMixin` bundles `TenantColumnMixin` (NOT NULL); we need the nullable sibling while keeping id + timestamps):

```python
class UserIdentity(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
```

Update the imports at the top of `auth.py`:

```python
from vera_core.db.base import (
    Base,
    CreatedAtMixin,
    NullableTenantColumnMixin,
    TimestampMixin,
    UUIDv7PKMixin,
)
```

(Drop `TenantScopedMixin` from the import if `SsoProvider` no longer needs it — `SsoProvider` still uses `TenantScopedMixin`, so KEEP it imported.) Update the `UserIdentity` docstring line about `tenant_id` denormalization to note it is now nullable (a platform operator's identity row has `tenant_id IS NULL`).

- [ ] **Step 4: Add the `PlatformLoginProvider` model**

In `models/auth.py`, add (after `SsoProvider`):

```python
class PlatformLoginProvider(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """The single GLOBAL login-provider config for platform operators (no tenant).

    The platform-plane analog of the per-tenant `sso_provider`: one row per
    provider kind for Vera's own operators (not per customer). `password` is the
    only enabled provider today (`enforce_mfa` always True for break-glass
    accounts); GCIP/OIDC become additional rows behind the same resolver. The
    `tenant_id` column is inherited from the platform-tier mixin and is ALWAYS
    NULL here — it exists only so the platform-readable RLS policy applies
    uniformly. Issuer/client-id columns for federated providers are added when
    GCIP lands; not modeled yet (YAGNI)."""

    __tablename__ = "platform_login_provider"
    __table_args__ = (
        check_in("provider_type", ProviderKind),
        UniqueConstraint("provider_type", name="uq_platform_login_provider_provider_type"),
    )

    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enforce_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
```

`UniqueConstraint`, `Boolean`, `String`, `check_in`, `ProviderKind` are already imported in `auth.py` (verify the imports list at the top includes `UniqueConstraint` and `Boolean` — they are used by existing models in the file).

- [ ] **Step 5: Export the model**

In `models/__init__.py`, add `PlatformLoginProvider` to the `from .auth import (...)` line and to the `__all__` tuple (alphabetical position near `SsoProvider`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest packages/vera_core/tests/test_models_platform_login.py -v && uv run mypy --strict packages/vera_core/src/vera_core/models/auth.py`
Expected: PASS; mypy clean.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/models/auth.py \
        packages/vera_core/src/vera_core/models/__init__.py \
        packages/vera_core/tests/test_models_platform_login.py
git commit -m "feat(models): nullable-tenant user_identity + PlatformLoginProvider"
```

---

## Task 4: migration — RLS policies + seed the password provider

**Files:**
- Create: `migrations/versions/0009_platform_login.py`
- Test: integration test in Task 7 exercises the policies end-to-end; this task is verified by `just up && just migrate` applying cleanly.

**Interfaces:**
- Consumes: `PlatformLoginProvider` model + nullable `user_identity` (Task 3); `platform_readable_rls_policy_ddl`, `drop_rls_policy_ddl` (`vera_core.db.rls`).
- Produces: `user_identity` carries the platform-readable policy; `platform_login_provider` is RLS-protected and has one seeded enabled `password` row (`tenant_id NULL`, `enforce_mfa=true`).

> **Why a policy-only migration:** migration `0001` builds every table (incl. `platform_login_provider` and the now-nullable `user_identity`) from `Base.metadata.create_all`. So this migration must NOT `create_table` — it only (a) ensures `user_identity.tenant_id` is nullable for already-migrated DBs, (b) swaps `user_identity`'s strict policy for the platform-readable one, (c) enables + adds a platform-readable policy on `platform_login_provider` (a fresh table gets no policy from `0001`), and (d) seeds the global `password` row.

- [ ] **Step 1: Write the migration**

Create `migrations/versions/0009_platform_login.py`:

```python
"""platform login — nullable user_identity + platform_login_provider RLS & seed

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-19

Platform-operator password login (ADR-0006 §D, interim). `user_identity` becomes
nullable-tenant and swaps to the platform-readable policy so a platform session
resolves an operator's NULL-tenant credential row (tenant sessions stay strict).
`platform_login_provider` (built by 0001's create_all) gets the same policy and a
single enabled `password` row — the global provider config the login resolver reads.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.db.rls import drop_rls_policy_ddl, platform_readable_rls_policy_ddl

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLATFORM_READABLE = ("user_identity", "platform_login_provider")


def upgrade() -> None:
    # Already-migrated DBs: relax the NOT NULL that 0001 created. Fresh DBs built
    # from the current model are already nullable; DROP NOT NULL is idempotent.
    op.execute("ALTER TABLE user_identity ALTER COLUMN tenant_id DROP NOT NULL")

    # Swap user_identity's strict policy (0001) for the platform-readable one, and
    # give the brand-new platform_login_provider table the same policy.
    for table in _PLATFORM_READABLE:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        for stmt in platform_readable_rls_policy_ddl(table):
            op.execute(stmt)

    # Seed the single global password provider (NULL tenant, MFA mandatory). This
    # migration runs as the migration role (bypasses RLS), so the NULL-tenant insert
    # is permitted despite the strict WITH CHECK.
    op.execute(
        "INSERT INTO platform_login_provider "
        "(id, tenant_id, provider_type, display_name, enabled, enforce_mfa,"
        " created_at, updated_at) "
        "VALUES (gen_random_uuid(), NULL, 'password', 'Password', true, true,"
        " now(), now()) "
        "ON CONFLICT (provider_type) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM platform_login_provider WHERE provider_type = 'password'")
    for table in _PLATFORM_READABLE:
        for stmt in drop_rls_policy_ddl(table):
            op.execute(stmt)
    # Restore user_identity's strict tenant-isolation policy (0001 default).
    from vera_core.db.rls import rls_policy_ddl

    for stmt in rls_policy_ddl("user_identity"):
        op.execute(stmt)
    op.execute("ALTER TABLE user_identity ALTER COLUMN tenant_id SET NOT NULL")
```

- [ ] **Step 2: Verify it's the latest head**

Run: `grep -rn "down_revision" migrations/versions/0008_tenant_slug.py` (confirms `0008` revises `0007`, so `0009` revising `0008` is the new head). Then check no other file already claims `down_revision = "0008"`: `grep -rln "\"0008\"" migrations/versions`. Expected: only `0009` references `"0008"` as `down_revision` (and `0008` itself as `revision`).

- [ ] **Step 3: Apply against a fresh DB**

Run: `just up && just migrate`
Expected: migrations apply through `0009` with no error; output ends at head `0009`.

- [ ] **Step 4: Verify the seed + nullable column**

Run:
```bash
just psql -c "SELECT provider_type, enabled, enforce_mfa, tenant_id FROM platform_login_provider;"
just psql -c "SELECT is_nullable FROM information_schema.columns WHERE table_name='user_identity' AND column_name='tenant_id';"
```
Expected: one `password / t / t / (null)` row; `is_nullable = YES`. (If `just psql` is not a recipe, use `docker compose exec -T postgres psql -U vera -d vera -c "..."`.)

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0009_platform_login.py
git commit -m "feat(db): platform_login_provider RLS + seed; nullable user_identity"
```

---

## Task 5: `resolve_platform_login_provider`

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/providers.py`
- Test: `apps/control_plane/tests/test_platform_provider.py` (create)

**Interfaces:**
- Consumes: `PlatformLoginProvider` model; existing `LoginProvider` dataclass.
- Produces: `async def resolve_platform_login_provider(session: AsyncSession, provider_type: str) -> LoginProvider | None`.

- [ ] **Step 1: Write the failing test**

Create `apps/control_plane/tests/test_platform_provider.py` (uses the integration DB fixture — model it on existing `apps/control_plane/tests` integration tests that use a `platform_session`/sessionmaker fixture; reuse whatever fixture name those tests use, e.g. `sessionmaker`):

```python
import pytest

from control_plane.auth.providers import resolve_platform_login_provider
from vera_core.db import platform_session

pytestmark = pytest.mark.integration


async def test_resolves_seeded_password_provider(sessionmaker):
    async with platform_session(sessionmaker) as session:
        provider = await resolve_platform_login_provider(session, "password")
    assert provider is not None
    assert provider.provider_type == "password"
    assert provider.enforce_mfa is True


async def test_unknown_provider_returns_none(sessionmaker):
    async with platform_session(sessionmaker) as session:
        provider = await resolve_platform_login_provider(session, "saml")
    assert provider is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/control_plane/tests/test_platform_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_platform_login_provider'`.

- [ ] **Step 3: Implement the resolver**

In `auth/providers.py`, add (import `PlatformLoginProvider` from `vera_core.models`):

```python
async def resolve_platform_login_provider(
    session: AsyncSession, provider_type: str
) -> LoginProvider | None:
    """The enabled GLOBAL platform login provider of `provider_type`, or None if
    not enabled (login must then be refused). The platform-plane analog of
    `resolve_login_provider` — no tenant: it reads the single global config row
    inside a `platform_session`. GCIP later is a new enabled row, not a new path."""
    row = (
        await session.execute(
            select(PlatformLoginProvider).where(
                PlatformLoginProvider.provider_type == provider_type,
                PlatformLoginProvider.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return LoginProvider(provider_type=row.provider_type, enforce_mfa=row.enforce_mfa)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest apps/control_plane/tests/test_platform_provider.py -v`
Expected: PASS (requires `just up && just migrate` first so the seed exists).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/providers.py \
        apps/control_plane/tests/test_platform_provider.py
git commit -m "feat(auth): resolve_platform_login_provider (global provider seam)"
```

---

## Task 6: `platform_mfa_secret_ref`

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/mfa.py`
- Test: `apps/control_plane/tests/test_mfa_ref.py` (create)

**Interfaces:**
- Produces: `def platform_mfa_secret_ref(user_id: UUID) -> str` → `"mfa/platform/{user_id}"`.

- [ ] **Step 1: Write the failing test**

Create `apps/control_plane/tests/test_mfa_ref.py`:

```python
from uuid import UUID

from control_plane.auth.mfa import mfa_secret_ref, platform_mfa_secret_ref

_UID = UUID("00000000-0000-0000-0000-0000000000aa")
_TID = UUID("00000000-0000-0000-0000-0000000000bb")


def test_platform_ref_is_tenant_less_and_distinct():
    assert platform_mfa_secret_ref(_UID) == f"mfa/platform/{_UID}"
    assert platform_mfa_secret_ref(_UID) != mfa_secret_ref(_TID, _UID)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/control_plane/tests/test_mfa_ref.py -v`
Expected: FAIL — `cannot import name 'platform_mfa_secret_ref'`.

- [ ] **Step 3: Implement it**

In `auth/mfa.py`, add next to `mfa_secret_ref`:

```python
def platform_mfa_secret_ref(user_id: UUID) -> str:
    """MFA material key for a platform operator (no tenant). Distinct keyspace
    from the tenant ref so a platform identity never collides with a tenant one."""
    return f"mfa/platform/{user_id}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/control_plane/tests/test_mfa_ref.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/mfa.py apps/control_plane/tests/test_mfa_ref.py
git commit -m "feat(auth): platform_mfa_secret_ref (tenant-less MFA keyspace)"
```

---

## Task 7: `/platform/auth/*` router — login + mfa/verify

**Files:**
- Create: `apps/control_plane/src/control_plane/api/v1/platform_auth.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/__init__.py` (mount the router)
- Test: `apps/control_plane/tests/test_platform_login.py` (create)

**Interfaces:**
- Consumes: `resolve_platform_login_provider` (Task 5), `platform_mfa_secret_ref` (Task 6), `SessionData.account_type` (Task 2), `platform_session` (`vera_core.db`), `Store`/`Secrets`/`AuthAudit`/`AppSettings` dependency aliases, `mfa.verify`, `verify_password`, `MAX_PASSWORD_BYTES`, `emit_auth_event`.
- Produces: `POST /api/v1/platform/auth/login` and `POST /api/v1/platform/auth/mfa/verify`; helper `_load_platform_password_creds(session, email) -> _PasswordCreds | None`.

> **Routing:** the existing `platform.py` router has `prefix="/platform"` and owns `/platform/elevations`. To keep elevation and auth concerns in separate files, this new router uses `prefix="/platform/auth"` and is mounted alongside it. Both resolve to `/api/v1/platform/...`.

> **MFA is mandatory:** the seeded provider has `enforce_mfa=True`, so login ALWAYS returns a challenge (never a session directly). The `mfa_required=False` branch from the tenant flow is intentionally omitted here.

> **No `last_login_at` stamping:** the platform-readable policy's `WITH CHECK` is strict equality, so the RLS-bound app role cannot UPDATE a NULL-tenant `app_user` row. Operator login time is recorded via the `LOGIN_SUCCESS` auth-audit row instead. (Tracked in Task 9's devops-todo note.)

- [ ] **Step 1: Write the failing integration test**

Create `apps/control_plane/tests/test_platform_login.py`. Model fixtures on existing integration tests in `apps/control_plane/tests` (reuse the app/client + DB fixtures; seed a platform operator via the bootstrap helper from Task 8 OR inline inserts as the superuser sessionmaker). Minimum coverage:

```python
import pyotp
import pytest

pytestmark = pytest.mark.integration


async def test_login_requires_mfa_then_succeeds(platform_operator, client):
    # platform_operator fixture: seeds a platform app_user + password identity
    # (mfa_enabled) + SUPER_ADMIN grant + stores a known TOTP seed; yields
    # (email, password, totp_secret).
    email, password, totp_secret = platform_operator
    r = await client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["mfa_required"] is True
    challenge = body["challenge_token"]
    assert body["session_token"] is None

    code = pyotp.TOTP(totp_secret).now()
    r2 = await client.post(
        "/api/v1/platform/auth/mfa/verify",
        json={"challenge_token": challenge, "code": code},
    )
    assert r2.status_code == 200
    token = r2.json()["data"]["session_token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    md = me.json()["data"]
    assert md["account_type"] == "platform"
    assert md["tenant_id"] is None
    assert "SUPER_ADMIN" in md["roles"]


async def test_bad_password_is_uniform_401(platform_operator, client):
    email, _password, _ = platform_operator
    r = await client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": "wrong"}
    )
    assert r.status_code == 401


async def test_unknown_email_is_uniform_401(client):
    r = await client.post(
        "/api/v1/platform/auth/login",
        json={"email": "nobody@vera.example", "password": "whatever"},
    )
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/control_plane/tests/test_platform_login.py -v`
Expected: FAIL — 404 on `/api/v1/platform/auth/login` (router not mounted yet).

- [ ] **Step 3: Implement the router**

Create `apps/control_plane/src/control_plane/api/v1/platform_auth.py`:

```python
"""Platform-operator login (ADR-0006 §D, interim password+MFA).

Tenant-less sibling of `auth.py`'s tenant login: no `{tenant_slug}` — the
operator belongs to no tenant. Credentials + provider config resolve inside a
`platform_session` (app.platform='on', no tenant GUC), which is exactly the RLS
context that exposes the NULL-tenant `app_user`/`user_identity`/
`platform_login_provider` rows and nothing else (zero PHI). MFA is mandatory:
login always returns a challenge, completed at `/mfa/verify`, which mints the
`account_type='platform'`, `tenant_id=None` session. Failures return a uniform
401 (no operator/provider enumeration); outcomes are audited to auth_audit_log
with `tenant_id=NULL`.
"""

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.auth import (
    LoginRequest,
    LoginResponse,
    MfaVerifyRequest,
    SessionResponse,
    _PasswordCreds,
    _unauthorized,
)
from control_plane.api.v1.common import AppSettings, AuthAudit, emit_auth_event
from control_plane.auth import mfa
from control_plane.auth.password import MAX_PASSWORD_BYTES, verify_password
from control_plane.auth.providers import resolve_platform_login_provider
from control_plane.auth.session import MFA_NS, SessionData
from control_plane.deps import client_ip, get_secret_provider, get_session_store, get_sessionmaker
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.config import SecretNotFoundError, WritableSecretProvider
from vera_core.db import platform_session
from vera_core.models import AppUser, UserIdentity
from vera_core.models.enums import AccountType, AuthEvent, ProviderKind

from sqlalchemy.ext.asyncio import async_sessionmaker
from control_plane.auth.session import SessionStore
from fastapi import Depends

router = APIRouter(prefix="/platform/auth", tags=["platform-auth"])

Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Store = Annotated[SessionStore, Depends(get_session_store)]
Secrets = Annotated[WritableSecretProvider, Depends(get_secret_provider)]


async def _load_platform_password_creds(
    session: AsyncSession, email: str
) -> _PasswordCreds | None:
    """An active PLATFORM operator's password credentials, resolved inside the
    platform session (NULL-tenant rows visible via the platform-readable policy)."""
    row = (
        await session.execute(
            select(
                AppUser.id,
                AppUser.email,
                AppUser.account_type,
                UserIdentity.hashed_password,
                UserIdentity.mfa_enabled,
            )
            .join(AppUser, AppUser.id == UserIdentity.app_user_id)
            .where(
                UserIdentity.provider_type == ProviderKind.PASSWORD.value,
                UserIdentity.email == email,
                AppUser.account_type == AccountType.PLATFORM.value,
                AppUser.status == "active",
            )
        )
    ).first()
    if row is None:
        return None
    return _PasswordCreds(
        user_id=row.id,
        email=row.email,
        account_type=row.account_type,
        hashed_password=row.hashed_password,
        mfa_enabled=row.mfa_enabled,
    )


@router.post(
    "/login",
    response_model=ResponseModel[LoginResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def platform_login(
    body: LoginRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[LoginResponse]:
    ip = client_ip(request)
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        await emit_auth_event(audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip)
        raise _unauthorized()

    async with platform_session(sessionmaker) as session:
        provider = await resolve_platform_login_provider(session, ProviderKind.PASSWORD.value)
        creds = (
            await _load_platform_password_creds(session, body.email)
            if provider is not None
            else None
        )

    if provider is None:
        # Password login disabled globally — uniform 401, un-audited (no operator).
        raise _unauthorized()
    if (
        creds is None
        or creds.hashed_password is None
        or not verify_password(body.password, creds.hashed_password)
    ):
        user_id = creds.user_id if creds is not None else None
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id
        )
        raise _unauthorized()

    base = SessionData(
        user_id=creds.user_id,
        tenant_id=None,
        email=creds.email,
        subject=creds.email,
        provider_type=ProviderKind.PASSWORD.value,
        mfa_passed=False,
        account_type=AccountType.PLATFORM.value,
        tenant_slug=None,
    )
    # MFA is mandatory for platform operators (enforce_mfa is always True).
    challenge = await store.put(MFA_NS, base, settings.mfa_challenge_ttl_seconds)
    await emit_auth_event(
        audit, tenant_id=None, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
    )
    return ok(LoginResponse(mfa_required=True, challenge_token=challenge))


@router.post(
    "/mfa/verify",
    response_model=ResponseModel[SessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def platform_mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    store: Store,
    secret_provider: Secrets,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[SessionResponse]:
    ip = client_ip(request)
    challenge = await store.get(MFA_NS, body.challenge_token)
    if challenge is None or challenge.account_type != AccountType.PLATFORM.value:
        raise _unauthorized()

    secret_ref = mfa.platform_mfa_secret_ref(challenge.user_id)
    try:
        verified = mfa.verify(secret_provider, secret_ref=secret_ref, code=body.code)
    except SecretNotFoundError:
        verified = False
    if not verified:
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=challenge.user_id
        )
        raise _unauthorized()

    await store.delete(MFA_NS, body.challenge_token)
    token = await store.mint_session(
        replace(challenge, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await emit_auth_event(
        audit, tenant_id=None, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=challenge.user_id
    )
    return ok(SessionResponse(session_token=token))
```

> Clean up the import block after writing (group the `fastapi`/`sqlalchemy` imports at the top properly — they are split above for readability; ruff will flag ordering, so run `ruff check --fix`).

- [ ] **Step 4: Mount the router**

In `api/v1/__init__.py`, add the import and include:

```python
from control_plane.api.v1.platform_auth import router as platform_auth_router
...
router.include_router(platform_auth_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `ruff check --fix apps/control_plane/src/control_plane/api/v1/platform_auth.py && uv run pytest apps/control_plane/tests/test_platform_login.py -v`
Expected: PASS (after the Task 8 `platform_operator` fixture exists — if implementing 7 before 8, inline the seed in the fixture using the superuser sessionmaker + `mfa.enroll` to store a known seed; otherwise reorder 7/8).

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/platform_auth.py \
        apps/control_plane/src/control_plane/api/v1/__init__.py \
        apps/control_plane/tests/test_platform_login.py
git commit -m "feat(auth): platform operator login + mfa/verify (/platform/auth)"
```

---

## Task 8: bootstrap script for operator #1

**Files:**
- Create: `scripts/bootstrap_platform_admin.py`
- Modify: `justfile` (add a `bootstrap-platform` recipe) — optional, match existing recipe style
- Test: `apps/control_plane/tests/test_bootstrap_platform_admin.py` (create) — verifies idempotency + created rows

**Interfaces:**
- Consumes: superuser `create_engine`/`create_sessionmaker` (RLS-bypassing, like `seed.py`), `hash_password`, `mfa.enroll`, `mfa.platform_mfa_secret_ref`, `SYSTEM_ROLES` / `Role` lookup, models.
- Produces: an idempotent `async def bootstrap(...) -> None` that creates exactly one platform operator (`app_user` + `user_identity` + `SUPER_ADMIN` grant), enrolls MFA, prints the `otpauth://` URI once; no-op if any platform operator already exists.

- [ ] **Step 1: Write the failing test**

Create `apps/control_plane/tests/test_bootstrap_platform_admin.py`:

```python
import pytest
from sqlalchemy import func, select

from scripts.bootstrap_platform_admin import bootstrap
from vera_core.models import AppUser, UserIdentity, UserRole
from vera_core.models.enums import AccountType

pytestmark = pytest.mark.integration


async def _platform_user_count(sessionmaker):
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(AppUser)
                .where(AppUser.account_type == AccountType.PLATFORM.value)
            )
        ).scalar_one()


async def test_bootstrap_creates_one_operator_and_is_idempotent(superuser_sessionmaker, secret_provider):
    email, password = "ops1@vera.example", "bootstrap-pw-123456"
    uri1 = await bootstrap(superuser_sessionmaker, secret_provider, email=email, password=password)
    assert uri1 is not None and uri1.startswith("otpauth://")
    assert await _platform_user_count(superuser_sessionmaker) == 1

    # Re-run: no-op, no second operator, returns None (nothing created).
    uri2 = await bootstrap(superuser_sessionmaker, secret_provider, email=email, password=password)
    assert uri2 is None
    assert await _platform_user_count(superuser_sessionmaker) == 1
```

(Use the existing integration fixtures for a superuser sessionmaker + an in-memory/secret provider; reuse fixture names from neighboring integration tests.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/control_plane/tests/test_bootstrap_platform_admin.py -v`
Expected: FAIL — `ModuleNotFoundError`/`cannot import name 'bootstrap'`.

- [ ] **Step 3: Implement the script**

Create `scripts/bootstrap_platform_admin.py`:

```python
"""Idempotent bootstrap for platform operator #1 (ADR-0006 §D).

Platform endpoints require an existing SUPER_ADMIN, but none exists out of the
box (`seed.py` makes only a TENANT_ADMIN). This run-once script seeds exactly the
FIRST operator — a platform `app_user` (account_type='platform', tenant_id=NULL)
+ password `user_identity` + a grant of the global SUPER_ADMIN role — and enrolls
MFA, printing the otpauth:// URI ONCE so it can be scanned. From then on,
operators add each other via the platform invite flow (separate plan).

Runs as the DB user from VERA_DATABASE_URL (locally the superuser → bypasses RLS),
exactly like seed.py — so the NULL-tenant inserts are permitted. NO-OP if any
platform operator already exists.

    just bootstrap-platform   (env: BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD)
"""

import asyncio
import os
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth import mfa
from control_plane.auth.password import hash_password
from vera_core.config import WritableSecretProvider, get_secret_provider, get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.models import AppUser, Role, UserIdentity, UserRole
from vera_core.models.enums import AccountType, ProviderKind


async def _platform_operator_exists(session: AsyncSession) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(AppUser)
            .where(AppUser.account_type == AccountType.PLATFORM.value)
        )
    ).scalar_one()
    return count > 0


async def bootstrap(
    sessionmaker: async_sessionmaker[AsyncSession],
    secret_provider: WritableSecretProvider,
    *,
    email: str,
    password: str,
) -> str | None:
    """Create platform operator #1 and return its otpauth:// URI, or None if a
    platform operator already exists (no-op)."""
    async with sessionmaker() as session, session.begin():
        if await _platform_operator_exists(session):
            return None

        super_admin = (
            await session.execute(
                select(Role).where(Role.tenant_id.is_(None), Role.name == "SUPER_ADMIN")
            )
        ).scalar_one()  # seeded by seed.py / migration; must exist

        user = AppUser(
            tenant_id=None,
            account_type=AccountType.PLATFORM.value,
            gcip_uid=None,
            email=email,
            name="Platform Operator",
            status="active",
        )
        session.add(user)
        await session.flush()

        secret_ref = mfa.platform_mfa_secret_ref(user.id)
        provisioning_uri = mfa.enroll(secret_provider, secret_ref=secret_ref, account_email=email)

        session.add(
            UserIdentity(
                tenant_id=None,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=email,
                email=email,
                hashed_password=hash_password(password),
                mfa_enabled=True,
                mfa_secret_ref=secret_ref,
            )
        )
        session.add(UserRole(tenant_id=None, app_user_id=user.id, role_id=super_admin.id))

    return provisioning_uri


async def main() -> None:
    email = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
    password = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]
    settings = get_settings()
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    secret_provider = get_secret_provider(settings)
    try:
        uri = await bootstrap(sessionmaker, secret_provider, email=email, password=password)
        if uri is None:
            print("platform operator already exists — no-op")
        else:
            print(f"created platform operator {email!r} (SUPER_ADMIN)")
            print("Scan this in an authenticator app (shown once):")
            print(uri)
            print("Login: POST /api/v1/platform/auth/login then /platform/auth/mfa/verify")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

> **Verify these helpers exist with these names** before relying on them: `get_secret_provider(settings)` in `vera_core.config` (the production secret-provider factory — check `vera_core/config/__init__.py`; if the factory has a different name, use that). `UserRole`'s `tenant_id` is nullable for a global grant — confirm in `models/rbac.py`; if `user_role.tenant_id` is NOT NULL, this is a real gap: the SUPER_ADMIN grant to a platform operator needs a nullable `user_role.tenant_id` + platform-readable policy (it IS in `PLATFORM_READABLE_TABLES`, so the policy is right; check the column nullability and, if NOT NULL, add it to Task 3/4). **Resolve this check before completing the task.**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest apps/control_plane/tests/test_bootstrap_platform_admin.py -v`
Expected: PASS.

- [ ] **Step 5: Add the `just` recipe (optional, match existing style)**

In `justfile`, near `seed`:

```make
bootstrap-platform:
    uv run python scripts/bootstrap_platform_admin.py
```

- [ ] **Step 6: Manual smoke (one operator, then login)**

```bash
just up && just migrate
BOOTSTRAP_ADMIN_EMAIL=ops@vera.example BOOTSTRAP_ADMIN_PASSWORD=change-me-please just bootstrap-platform
```
Expected: prints an `otpauth://` URI. Re-running prints "already exists — no-op".

- [ ] **Step 7: Commit**

```bash
git add scripts/bootstrap_platform_admin.py apps/control_plane/tests/test_bootstrap_platform_admin.py justfile
git commit -m "feat(auth): idempotent bootstrap for platform operator #1"
```

---

## Task 9: ADR + devops-todo updates

**Files:**
- Modify: `adr/0006-platform-runtime-and-elevation.md` (§D)
- Modify: `adr/devops-todo.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Amend ADR-0006 §D**

Open `adr/0006-platform-runtime-and-elevation.md`, find §D (the "no platform login yet / GCIP deferred" section). Replace the "no first-class platform login" language with a record that:
- Interim platform login is **local password + mandatory TOTP MFA**, behind a provider seam (`resolve_platform_login_provider` + the single-row `platform_login_provider` config).
- The login surface is `POST /api/v1/platform/auth/login` + `/mfa/verify`; sessions carry `account_type='platform'`, `tenant_id=None`.
- GCIP SSO remains the future direction — it lands as an additional enabled `platform_login_provider` row + a verify branch, not a parallel path.
- Operator #1 is seeded by `scripts/bootstrap_platform_admin.py`; ongoing operators via the platform invite flow (separate plan, not yet implemented).

Keep the section's existing wording/structure; edit in place rather than rewriting the file.

- [ ] **Step 2: Add devops-todo rows**

Append to `adr/devops-todo.md` (match the existing table/row format):
- `last_login_at` is NOT stamped for platform operators (the platform-readable RLS `WITH CHECK` forbids the RLS-bound app role from updating a NULL-tenant row); operator login time is sourced from `auth_audit_log` `LOGIN_SUCCESS`. Revisit with a SECURITY DEFINER stamp fn if a denormalized field is later required.
- Production secret store must hold platform MFA material under the `mfa/platform/{user_id}` keyspace (distinct from `mfa/{tenant_id}/{user_id}`).
- Bootstrap (`bootstrap_platform_admin.py`) is a privileged run-once operation requiring DB-superuser (RLS-bypass) access; gate its execution in production deploys.

- [ ] **Step 3: Commit**

```bash
git add adr/0006-platform-runtime-and-elevation.md adr/devops-todo.md
git commit -m "docs(auth): record interim platform password login + infra obligations"
```

---

## Task 10: full-suite verification + simplify

**Files:** whole diff.

- [ ] **Step 1: Run the full gate**

Run: `just check`
Expected: ruff + mypy --strict + pytest all green.

- [ ] **Step 2: End-to-end smoke**

```bash
just up && just migrate
BOOTSTRAP_ADMIN_EMAIL=ops@vera.example BOOTSTRAP_ADMIN_PASSWORD=change-me-please just bootstrap-platform
# scan the printed otpauth URI, then exercise login → mfa/verify → /auth/me with a real TOTP code
```
Expected: `/auth/me` returns `account_type: "platform"`, `tenant_id: null`, roles include `SUPER_ADMIN`.

- [ ] **Step 3: Run `/simplify` on the diff**

Per repo `CLAUDE.md`, run the `/simplify` skill over the change (quality/altitude cleanup), then re-run `just check`.

- [ ] **Step 4: Final commit (if simplify changed anything)**

```bash
git add -A
git commit -m "refactor(auth): simplify platform-login implementation"
```

---

## Self-Review (completed during authoring)

**Spec coverage:**
- §2 provider seam → Tasks 3, 4, 5. ✅
- §3/§4 URL convention + endpoints → Task 7 (login + mfa/verify under `/platform/auth`; shared `/auth/me` reused; standalone enroll dropped per spec). ✅
- §3 schema/RLS (nullable `user_identity`, `platform_login_provider`, MFA ref) → Tasks 3, 4, 6. ✅
- §5 bootstrap operator #1 → Task 8. ✅
- §6 ongoing-operator invite flow → **explicitly deferred** to a follow-on plan (documented in Scope). ✅
- §7 audit (null-tenant) → Task 7 (`emit_auth_event(tenant_id=None, ...)`). ✅
- §8 ADR amendment → Task 9. ✅
- Cross-worktree `account_type` dependency → Tasks 1–2 (matched to the refactor branch, skippable if already merged). ✅

**Open verification items flagged inside tasks (resolve during execution, not assumptions):**
- Task 8: `user_role.tenant_id` nullability for a global SUPER_ADMIN grant to a platform operator — confirm the column is nullable; if not, extend Tasks 3/4.
- Task 8: the production secret-provider factory name in `vera_core.config`.
- Task 7: ordering vs Task 8's `platform_operator` fixture (inline-seed alternative given).

**Placeholder scan:** no TBD/TODO; every code step shows real code. ✅
**Type consistency:** `LoginProvider`, `_PasswordCreds` (with added `account_type`), `SessionData` (with `account_type`), `resolve_platform_login_provider`, `platform_mfa_secret_ref`, `bootstrap(...)` signatures match across tasks. ✅
