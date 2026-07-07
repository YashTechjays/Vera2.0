# Login/logout security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close a login user-enumeration timing side-channel and add logout events to the auth audit trail.

**Architecture:** Two independent backend changes in the control plane. (1) A constant-work bcrypt verify (`verify_password_or_dummy`) so the unknown-email path costs the same as the wrong-password path. (2) A new `AuthEvent.LOGOUT`, a CHECK-constraint-widening migration, and an audit emit in the `/auth/logout` endpoint (routing null-tenant platform operators through the existing SECURITY DEFINER path).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, bcrypt, pytest (`pytest-asyncio`), Postgres w/ RLS. `just` runs everything.

## Global Constraints

- **Async runtime:** `asyncio` only — never `import anyio`; use stdlib `asyncio` primitives.
- **Type params:** PEP 695 (`def f[T]`, `class C[T]`) — ruff rejects `TypeVar`/`Generic[T]`.
- **Typecheck:** mypy `--strict` must pass (`just typecheck`).
- **Migrations:** revision IDs are alembic's **random hex** with a **date+time-prefixed filename** — generate with `just makemigration`, never hand-number (`0023`). `down_revision` for the new migration is `"0022"` (current single head). Legacy `0001`–`0022` keep their sequential names.
- **CHECK-constraint migration precedent:** copy `migrations/versions/0017_persona_tweak_event.py` exactly (drop-and-recreate the named constraint; `DROP ... IF EXISTS` so it's a no-op on a fresh DB).
- **Commit messages:** do NOT add a `Co-Authored-By` trailer.
- **Definition of done (repo rule):** after implementation run the `/simplify` skill on the change, then `just check` (ruff + mypy --strict + pytest), before committing final.
- **Integration tests need a live DB:** `just up && just migrate` first; they skip otherwise.
- All backend paths below are relative to `vera-backend/`.

---

## File Structure

- `apps/control_plane/src/control_plane/auth/password.py` — **modify.** Add `_DUMMY_HASH` (import-time) + `verify_password_or_dummy`.
- `tests/unit/auth/test_password.py` — **modify.** Unit tests for the new primitive.
- `apps/control_plane/src/control_plane/api/v1/auth.py` — **modify.** Login failure check (Task 2) + logout emit (Task 4).
- `packages/vera_core/src/vera_core/models/enums.py` — **modify.** Add `LOGOUT` to `AuthEvent`.
- `migrations/versions/<generated>_widen_auth_event_check_for_logout.py` — **create** (via `just makemigration`). Widen the `event_type` CHECK.
- `tests/integration/control_plane/test_login_flow.py` — **modify.** Expose admin sessionmaker on `LoginWorld`; add a logout-audit assertion test.

Task order: **1 → 2 → 3 → 4**. Task 4 depends on Task 3 (the DB CHECK must accept `'logout'` before the endpoint can insert it).

---

### Task 1: Constant-work bcrypt verify primitive

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/password.py`
- Test: `tests/unit/auth/test_password.py`

**Interfaces:**
- Consumes: existing `hash_password(str) -> str`, `verify_password(str, str) -> bool`, `_ROUNDS = 12` (all in `password.py`).
- Produces: `verify_password_or_dummy(password: str, hashed: str | None) -> bool` — runs one full bcrypt op even when `hashed is None`, returning `False` in that case.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/auth/test_password.py`:

```python
from control_plane.auth.password import (
    MAX_PASSWORD_BYTES,
    hash_password,
    verify_password,
    verify_password_or_dummy,
)


def test_verify_or_dummy_matches_real_hash() -> None:
    assert verify_password_or_dummy("hunter2", hash_password("hunter2")) is True


def test_verify_or_dummy_rejects_wrong_password() -> None:
    assert verify_password_or_dummy("nope", hash_password("hunter2")) is False


def test_verify_or_dummy_returns_false_for_missing_hash() -> None:
    # Unknown email / user with no password identity: no stored hash, still a
    # non-match — but a full bcrypt comparison ran against the dummy hash.
    assert verify_password_or_dummy("anything", None) is False
```

Update the existing top-of-file import line to include the new symbol (replace the current `from control_plane.auth.password import MAX_PASSWORD_BYTES, hash_password, verify_password`). If you appended the import block above, delete the old single-line import so there's exactly one import from `control_plane.auth.password`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/auth/test_password.py -v`
Expected: FAIL — `ImportError: cannot import name 'verify_password_or_dummy'`.

- [ ] **Step 3: Implement the primitive**

In `apps/control_plane/src/control_plane/auth/password.py`, after the `verify_password` function, add:

```python
# A dummy hash generated once at import from the SAME _ROUNDS as real hashes, so a
# verify against it costs exactly what a real verify costs (the bcrypt cost factor
# drives timing). The password is a throwaway, not a secret.
_DUMMY_HASH = hash_password("vera-timing-equalizer")


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Constant-work verify: when there is no stored hash (unknown email, or a user
    with no password identity), still run a full bcrypt comparison against a dummy
    hash and return False. Keeps the unknown-email path the same latency as the
    wrong-password path — closes the login user-enumeration timing side-channel."""
    return verify_password(password, hashed if hashed is not None else _DUMMY_HASH)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/auth/test_password.py -v`
Expected: PASS (all, including the three new tests).

- [ ] **Step 5: Typecheck + lint the touched files**

Run: `uv run mypy apps/control_plane/src/control_plane/auth/password.py && uv run ruff check apps/control_plane/src/control_plane/auth/password.py tests/unit/auth/test_password.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/password.py tests/unit/auth/test_password.py
git commit -m "feat(auth): add constant-work verify_password_or_dummy primitive"
```

---

### Task 2: Wire the primitive into the login failure check

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py`

**Interfaces:**
- Consumes: `verify_password_or_dummy` from Task 1; existing `_PasswordCreds` (`.user_id`, `.hashed_password`), `_audit`, `_unauthorized`, `AuthEvent.LOGIN_FAILURE`.
- Produces: behavior-identical login endpoint (same uniform 401s) with constant timing across all failure branches. No new public symbols.

This is a behavior-preserving refactor. Its regression test is the **existing** integration suite (`test_unknown_email_is_401`, `test_wrong_password_is_401`, `test_login_success_issues_usable_session`), which must all still pass.

- [ ] **Step 1: Update the import**

In `apps/control_plane/src/control_plane/api/v1/auth.py`, change the password import line:

```python
from control_plane.auth.password import MAX_PASSWORD_BYTES, hash_password, verify_password
```

to:

```python
from control_plane.auth.password import (
    MAX_PASSWORD_BYTES,
    hash_password,
    verify_password_or_dummy,
)
```

Note: `verify_password` is no longer referenced directly in this module after Step 2 — dropping it from the import is correct. (If ruff reports it still used elsewhere, keep it; grep first: `grep -n "verify_password\b" apps/control_plane/src/control_plane/api/v1/auth.py`.)

- [ ] **Step 2: Replace the failure check**

Find this block in the `login` function (currently the `if (...)` guard after `if provider is None:`):

```python
    if (
        creds is None
        or creds.hashed_password is None
        or not verify_password(body.password, creds.hashed_password)
    ):
        user_id = creds.user_id if creds is not None else None
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id
        )
        raise _unauthorized()
```

Replace it with:

```python
    # Constant-time: always run one bcrypt verify, even for an unknown email or a
    # user with no password hash (dummy verify → False). Every failure branch below
    # costs the same, so response time can't reveal whether the email is registered.
    password_ok = verify_password_or_dummy(
        body.password, creds.hashed_password if creds is not None else None
    )
    if creds is None or not password_ok:
        user_id = creds.user_id if creds is not None else None
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id
        )
        raise _unauthorized()
```

(The `creds.hashed_password is None` sub-case now folds into `not password_ok`: the dummy verify returns `False` for a `None` hash.)

- [ ] **Step 3: Typecheck + lint**

Run: `uv run mypy apps/control_plane/src/control_plane/api/v1/auth.py && uv run ruff check apps/control_plane/src/control_plane/api/v1/auth.py`
Expected: no errors (no unused-import warning for `verify_password`).

- [ ] **Step 4: Run the login integration tests**

Prereq (once): `just up && just migrate`
Run: `uv run pytest tests/integration/control_plane/test_login_flow.py -v`
Expected: PASS — in particular `test_unknown_email_is_401`, `test_wrong_password_is_401`, `test_login_success_issues_usable_session`.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/auth.py
git commit -m "fix(auth): constant-time login to close email-enumeration timing leak"
```

---

### Task 3: Add `LOGOUT` auth event + widen the DB CHECK

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py`
- Create: `migrations/versions/<generated>_widen_auth_event_check_for_logout.py`

**Interfaces:**
- Produces: `AuthEvent.LOGOUT` (value `"logout"`); the `ck_auth_audit_log_event_type_valid` CHECK constraint now accepts `'logout'`. Task 4 consumes both.

- [ ] **Step 1: Add the enum value**

In `packages/vera_core/src/vera_core/models/enums.py`, in `class AuthEvent`, add `LOGOUT` as the last member (after `AUTHZ_DENY = "authz_deny"`):

```python
    AUTHZ_ALLOW = "authz_allow"
    AUTHZ_DENY = "authz_deny"
    # Token-scoped self-logout (/auth/logout). Tenant users write a tenant-scoped
    # row; platform operators (tenant_id IS NULL) go through log_auth_event.
    LOGOUT = "logout"
```

- [ ] **Step 2: Scaffold the migration**

Run: `just makemigration message="widen auth_audit_log event_type check for logout"`
This creates `migrations/versions/<YYYYMMDD_HHMM>_<hex>_widen_auth_audit_log_event_type_check_for_logout.py`.

Verify: open the file; confirm `down_revision` is `"0022"`. If `just makemigration` produced a different `down_revision` (e.g. because another migration merged in the meantime), run `uv run alembic heads` — if there are multiple heads, STOP and resolve with `just merge-heads` before continuing; otherwise set `down_revision` to the single reported head.

- [ ] **Step 3: Replace the migration body**

Replace the entire generated file body (keep the generated `revision` and `down_revision` values — substitute them into the placeholders below) with:

```python
"""widen auth_audit_log.event_type CHECK for logout

Adds the `logout` auth event. `auth_audit_log.event_type` is constrained by a CHECK
built from the `AuthEvent` StrEnum (`ck_auth_audit_log_event_type_valid`; see 0006/0017).
Drop-and-recreate the named constraint from the CURRENT enum: `DROP ... IF EXISTS` is a
no-op on a fresh DB (where 0001 already built it with the new value) and an in-place
widen on an existing one. The value list is derived from the enum so it can't drift.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

# Keep the generated revision / down_revision values from `just makemigration`.
revision: str = "<GENERATED_HEX>"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The value set before this migration (current enum minus `logout`), for downgrade.
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

- [ ] **Step 4: Apply and round-trip the migration**

Run:
```bash
just migrate
uv run alembic downgrade -1
just migrate
```
Expected: `upgrade` applies clean; `downgrade -1` restores the prior constraint; re-`upgrade` re-applies — all with no errors. (`just migrate` == `alembic upgrade head`.)

- [ ] **Step 5: Typecheck + lint**

Run: `uv run ruff check packages/vera_core/src/vera_core/models/enums.py migrations/versions/*widen_auth_audit_log_event_type_check_for_logout.py && uv run mypy packages/vera_core/src/vera_core/models/enums.py`
Expected: no errors. (Note: `migrations/` may be excluded from mypy — do not add it.)

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/models/enums.py migrations/versions/*widen_auth_audit_log_event_type_check_for_logout.py
git commit -m "feat(auth): add logout auth event and widen auth_audit_log CHECK"
```

---

### Task 4: Emit the logout event + integration test

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py`
- Test: `tests/integration/control_plane/test_login_flow.py`

**Interfaces:**
- Consumes: `AuthEvent.LOGOUT` (Task 3); existing `emit_auth_event(sink, *, tenant_id: UUID | None, event, ip, user_id=None, meta=None)`, `client_ip(request)`, `AuthAudit`, `VerifiedIdentity` (`.tenant_id: UUID | None`, `.user_id: UUID`).
- Produces: `/auth/logout` writes one `auth_audit_log` row per live-session logout.

- [ ] **Step 1: Expose the admin sessionmaker on `LoginWorld` (test fixture prep)**

In `tests/integration/control_plane/test_login_flow.py`:

Add `AuthEvent` to the enums import and `AsyncSession` to the sqlalchemy import:

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
```
```python
from vera_core.models.enums import AuthEvent, ProviderKind
```

Add a field to the `LoginWorld` dataclass (non-breaking — existing tests unpack `client, world` and ignore it):

```python
@dataclass
class LoginWorld:
    tenant_id: UUID
    slug: str
    email: str
    admin_sessionmaker: async_sessionmaker[AsyncSession]
```

In the `login_world` fixture, pass it when constructing `LoginWorld` (the fixture already builds `sessionmaker` on the superuser `admin_engine`):

```python
            yield client, LoginWorld(
                tenant_id=tenant_id,
                slug=slug,
                email=email,
                admin_sessionmaker=sessionmaker,
            )
```

- [ ] **Step 2: Write the failing test**

Add to `tests/integration/control_plane/test_login_flow.py`:

```python
async def test_logout_is_audited(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]

    resp = await client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200

    # A logout row landed in the tenant's auth trail, attributed to the user + IP.
    # Read as superuser (WORM RLS is SELECT-only for the app role).
    async with world.admin_sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT app_user_id, ip_address FROM auth_audit_log"
                    " WHERE tenant_id = :t AND event_type = :e"
                ).bindparams(t=world.tenant_id, e=AuthEvent.LOGOUT.value)
            )
        ).one()
        user_id = (
            await session.execute(
                text("SELECT id FROM app_user WHERE email = :em").bindparams(em=world.email)
            )
        ).scalar_one()
    assert row.app_user_id == user_id
    assert row.ip_address is not None
```

- [ ] **Step 3: Run the test to verify it fails**

Prereq (once): `just up && just migrate` (must include Task 3's migration).
Run: `uv run pytest tests/integration/control_plane/test_login_flow.py::test_logout_is_audited -v`
Expected: FAIL — `sqlalchemy...NoResultFound` (no `logout` row is written yet).

- [ ] **Step 4: Emit the event in the logout endpoint**

In `apps/control_plane/src/control_plane/api/v1/auth.py`, replace the `logout` function:

```python
async def logout(
    _identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Store,
) -> ResponseModel[None]:
    # Token-scoped self-op: `current_identity` proves a live session (expired → 401);
    # the slug is irrelevant. delete_session reaps both the `sess` and `sess_abs` keys.
    if credentials is not None:
        await store.delete_session(credentials.credentials)
    return ok(None, message="Logged out.")
```

with:

```python
async def logout(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    request: Request,
    store: Store,
    audit: AuthAudit,
) -> ResponseModel[None]:
    # Token-scoped self-op: `current_identity` proves a live session (expired → 401),
    # so only real logouts are audited; the slug is irrelevant. delete_session reaps
    # both the `sess` and `sess_abs` keys. A platform operator's tenant_id is None, so
    # emit via emit_auth_event (accepts None → the log_auth_event definer path), not the
    # UUID-only _audit helper.
    if credentials is not None:
        await store.delete_session(credentials.credentials)
    await emit_auth_event(
        audit,
        tenant_id=identity.tenant_id,
        event=AuthEvent.LOGOUT,
        ip=client_ip(request),
        user_id=identity.user_id,
    )
    return ok(None, message="Logged out.")
```

(`emit_auth_event`, `client_ip`, `AuthAudit`, and `Request` are already imported in this module.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/integration/control_plane/test_login_flow.py::test_logout_is_audited tests/integration/control_plane/test_login_flow.py::test_logout_invalidates_session -v`
Expected: PASS (both — the existing logout test still passes).

- [ ] **Step 6: Typecheck + lint**

Run: `uv run mypy apps/control_plane/src/control_plane/api/v1/auth.py && uv run ruff check apps/control_plane/src/control_plane/api/v1/auth.py tests/integration/control_plane/test_login_flow.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/auth.py tests/integration/control_plane/test_login_flow.py
git commit -m "feat(auth): audit-log logout events"
```

---

### Task 5: Simplify pass + full gate

**Files:** all changed files.

- [ ] **Step 1: Run the simplifier**

Invoke the `/simplify` skill on the change (reuse / simplification / efficiency / altitude cleanup only — not bug-hunting), targeting the recently modified files. Apply its refinements. This is the mandatory repo workflow rule.

- [ ] **Step 2: Full CI gate**

Prereq: `just up && just migrate`
Run: `just check`
Expected: PASS — `lint` (ruff) + `typecheck` (mypy --strict) + `test` (pytest, unit + integration).

- [ ] **Step 3: Commit any simplifier changes**

```bash
git add -A
git commit -m "refactor(auth): simplify login/logout security changes"
```
(Skip this commit if the simplifier made no changes.)

---

## Self-Review notes

- **Spec coverage:** Part 1 timing fix → Tasks 1–2. Part 2 logout audit → Tasks 3–4 (enum + migration + endpoint + test). Out-of-scope tenant-timing path explicitly not touched. `/simplify` + `just check` → Task 5.
- **Type consistency:** `verify_password_or_dummy(password: str, hashed: str | None) -> bool` defined in Task 1, consumed with matching signature in Task 2. `emit_auth_event(..., tenant_id: UUID | None, ...)` matches `identity.tenant_id: UUID | None` in Task 4. `AuthEvent.LOGOUT` defined Task 3, used Task 4.
- **Migration revision id** is generated by `just makemigration` (random hex) — the plan carries `<GENERATED_HEX>` as a substitution point, not a literal, per the repo convention.
