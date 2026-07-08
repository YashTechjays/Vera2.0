# Publish-to-others — Backend Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a concurrent call private to its initiating VA by default, add an owner-only one-way `publish` that makes it visible tenant-wide, gate + audit non-owner joins, and let the owner revoke an intervener — all on the persistent `/calls` path.

**Architecture:** Add an owner (`initiated_by_id`, already a column — just start writing it) and a one-way visibility flag (`published`/`published_at`) to `Call`. Scope `GET /calls` to `owner OR published`, gate `join-token` for non-owners, and record publish / non-owner-join / revoke in the compliance `audit_log`. A new `calls:publish` permission (Supervisor/Admin) gates publish + revoke. This plan is the **backend contract only**; the frontend wiring is a separate follow-on plan and is fully testable via integration tests first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, Postgres (RLS), pytest-asyncio, self-hosted LiveKit OSS.

**Spec:** `docs/superpowers/specs/2026-07-02-publish-call-to-others-design.md`

## Global Constraints

- **Style:** PEP 695 type params only (`class Foo[T]`); ruff rejects `Generic[T]`/`TypeVar`.
- **Async:** `asyncio` only — `asyncio.TaskGroup`/`asyncio.timeout`, never `anyio`.
- **HTTP contract:** every endpoint returns `ResponseModel[T]` via `ok(...)`; errors `raise CustomAPIException` / its subclasses (never `HTTPException`). Declare `response_model=` + `responses=CustomAPIResponse.custom(...)`.
- **Timestamps:** DB clock only — `func.now()` / server defaults, never Python `datetime.now()`.
- **Audit:** every disclosure/authz event writes an `AuditRecord` with **field names / ids only**, never PHI values; timestamps DB-minted; `audit_log` is append-only (never UPDATE/DELETE).
- **Migrations:** new migrations are **date-prefixed with an alembic-random hex id** (`just makemigration`) — never hand-numbered sequential ids. Use idempotent `ADD COLUMN IF NOT EXISTS` (migration 0001 materializes DDL from `Base.metadata`, so a fresh DB already has the column).
- **RLS:** all queries run tenant-scoped; publishing never crosses a tenant boundary.
- **CI gate:** `just check` (ruff + mypy --strict + pytest) must pass before any commit. Integration tests need `just up && just migrate` first.
- **After implementation:** run the `/simplify` skill on the change, then re-run `just check` before the final commit (per `vera-backend/CLAUDE.md`).

---

### Task 1: Add `calls:publish` permission + audit event types

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Modify: `packages/vera_core/src/vera_core/models/audit_log.py` (add 3 `AuditEvent` members)
- Test: `tests/unit/test_rbac_defaults.py` (create)

**Interfaces:**
- Produces: permission key `"calls:publish"`; `AuditEvent.CALL_PUBLISH` (`"call.publish"`), `AuditEvent.CALL_INTERVENE_JOIN` (`"call.intervene.join"`), `AuditEvent.CALL_INTERVENE_REVOKE` (`"call.intervene.revoke"`).

Note: `audit_log.event_type` is a free `String(64)` (no CHECK), so adding enum members needs **no migration**. `calls:publish` is a new row in the `permission` catalog, seeded by `_seed_permissions` (already re-run by the `rbac_world` fixture and `scripts/seed.py`), so it also needs no migration.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_rbac_defaults.py
from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import DEFAULT_PERMISSIONS, SYSTEM_ROLES


def test_calls_publish_permission_is_catalogued_and_granted() -> None:
    assert "calls:publish" in DEFAULT_PERMISSIONS
    # Supervisor + Tenant Admin can publish; Tenant Admin holds all DEFAULT_PERMISSIONS.
    assert "calls:publish" in SYSTEM_ROLES["SUPERVISOR"]
    assert "calls:publish" in SYSTEM_ROLES["TENANT_ADMIN"] or \
        SYSTEM_ROLES["TENANT_ADMIN"] == frozenset(DEFAULT_PERMISSIONS)


def test_call_audit_events_exist() -> None:
    assert AuditEvent.CALL_PUBLISH.value == "call.publish"
    assert AuditEvent.CALL_INTERVENE_JOIN.value == "call.intervene.join"
    assert AuditEvent.CALL_INTERVENE_REVOKE.value == "call.intervene.revoke"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_rbac_defaults.py -v`
Expected: FAIL — `KeyError`/`AssertionError` (permission absent) and `AttributeError` (enum member absent).

- [ ] **Step 3: Add the permission to the catalog and grant it**

In `rbac_defaults.py`, add to `DEFAULT_PERMISSIONS` (after `"calls:write"`):

```python
    "calls:publish": "Publish a call so other VAs in the tenant can view and intervene",
```

Add `"calls:publish"` to the `SUPERVISOR` frozenset in `SYSTEM_ROLES` (TENANT_ADMIN already holds all of `DEFAULT_PERMISSIONS`; SUPER_ADMIN holds `ALL_PERMISSIONS`):

```python
    "SUPERVISOR": frozenset(
        {
            "calls:read",
            "calls:write",
            "calls:publish",
            "forms:read",
            "forms:write",
            "users:read",
            "audit:read",
            "phi:detokenize",
        }
    ),
```

- [ ] **Step 4: Add the audit event members**

In `audit_log.py`, add to the `AuditEvent` enum (after `FORM_STATUS_CHANGE`):

```python
    # A VA published a call so other tenant VAs can view/intervene (visibility
    # widening — a disclosure-enabling decision). Ids only, never PHI.
    CALL_PUBLISH = "call.publish"
    # A non-owner VA minted a join token for a published call — the actual PHI
    # disclosure (they can now hear the live transcript). Ids only.
    CALL_INTERVENE_JOIN = "call.intervene.join"
    # The owner ejected an intervener from a published call. Ids only.
    CALL_INTERVENE_REVOKE = "call.intervene.revoke"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_rbac_defaults.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/models/rbac_defaults.py \
        packages/vera_core/src/vera_core/models/audit_log.py \
        tests/unit/test_rbac_defaults.py
git commit -m "feat(calls): add calls:publish permission and call audit events

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add a second permissioned user to the RBAC test world

**Files:**
- Modify: `tests/integration/control_plane/conftest.py` (`RBACWorld`, `rbac_world`)
- Test: `tests/integration/control_plane/test_calls.py` (add one smoke test)

**Interfaces:**
- Produces: `RBACWorld.supervisor_token` — a session token for a **second** user in the same tenant, holding the `SUPERVISOR` role (so it has `calls:read` + `calls:publish` but is a *different owner* than `admin`). Later tasks use it to exercise owner-vs-non-owner paths. `admin_token` remains the primary owner; `norole_token` remains the no-permission user.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py
@pytest.mark.asyncio
async def test_supervisor_token_can_list_calls(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    """The second permissioned persona (SUPERVISOR) is wired and authenticated."""
    resp = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_supervisor_token_can_list_calls -v`
Expected: FAIL — `AttributeError: 'RBACWorld' object has no attribute 'supervisor_token'`.

- [ ] **Step 3: Add the field and mint the token**

In `conftest.py`, add the attribute to `RBACWorld.__init__`:

```python
        self.supervisor_token = ""
```

In the `rbac_world` fixture, after the `admin_role` lookup, look up the SUPERVISOR role, create a supervisor user, and assign the role (inside the same `session.begin()` block that creates `admin`/`norole`):

```python
        supervisor_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPERVISOR'")
            )
        ).scalar_one()
        supervisor = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="supervisor@test.example",
            name="Supervisor",
            status="active",
        )
        session.add(supervisor)
        await session.flush()
        session.add(
            UserRole(tenant_id=tenant_id, app_user_id=supervisor.id, role_id=supervisor_role)
        )
        supervisor_id = supervisor.id
```

After the existing `world.norole_token = ...` mint, add:

```python
    world.supervisor_token = await _mint(
        session_store, user_id=supervisor_id, tenant_id=tenant_id, email="supervisor@test.example"
    )
```

(The `rbac_world` teardown already deletes `app_user`/`user_role` by tenant, so the new user is cleaned up with no change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_supervisor_token_can_list_calls -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/control_plane/conftest.py tests/integration/control_plane/test_calls.py
git commit -m "test(calls): add second permissioned (supervisor) persona to rbac world

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Add `published` / `published_at` columns + index + migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/call.py` (`Call`)
- Create: `migrations/versions/<date>_<hex>_call_published.py` (via `just makemigration`)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Produces: `Call.published: bool` (NOT NULL, default `False`), `Call.published_at: datetime | None`, index `ix_call_tenant_published` on `(tenant_id, published)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py  (add imports: from sqlalchemy import select; from vera_core.models import Call)
@pytest.mark.asyncio
async def test_new_call_is_private_by_default(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session,  # AsyncSession fixture from conftest
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = UUID(created.json()["data"]["id"])
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.published is False
    assert row.published_at is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_new_call_is_private_by_default -v`
Expected: FAIL — `AttributeError: 'Call' object has no attribute 'published'` (or a DB `UndefinedColumn` after model add, before migrate).

- [ ] **Step 3: Add the columns and index to the model**

In `call.py`, add to `Call` (import `Boolean` from `sqlalchemy`):

```python
    # Visibility axis — orthogonal to current_status. One-way: once True it never
    # returns to False (spec §1 decision 4). False = private to initiated_by_id.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Add to `Call.__table_args__`:

```python
        Index("ix_call_tenant_published", "tenant_id", "published"),
```

- [ ] **Step 4: Generate and edit the migration**

Run: `just makemigration -m "call published visibility flag"`

Replace the generated `upgrade`/`downgrade` bodies with idempotent DDL (keep the auto-generated `revision`/`down_revision` — do **not** hand-number):

```python
def upgrade() -> None:
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS published_at timestamptz NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_call_tenant_published ON call (tenant_id, published)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_call_tenant_published")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS published_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS published")
```

- [ ] **Step 5: Apply the migration and run the test**

Run: `just migrate`
Then: `uv run pytest tests/integration/control_plane/test_calls.py::test_new_call_is_private_by_default -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/models/call.py migrations/versions/ \
        tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): add one-way published visibility flag to Call

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Set the owner on create + surface `published`/`is_owner` in `CallSummary`

**Files:**
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py` (`CallSummary`)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`_summary`, `start_call`)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `Call.published` (Task 3), `Call.initiated_by_id` (existing column).
- Produces: `CallSummary.published: bool`, `CallSummary.is_owner: bool`; `_summary(call, patient_name, caller_id)` — the third positional arg is the requesting user's id, used to compute `is_owner`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py
@pytest.mark.asyncio
async def test_create_call_summary_reports_owner_and_private(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["published"] is False
    assert data["is_owner"] is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_create_call_summary_reports_owner_and_private -v`
Expected: FAIL — `KeyError: 'published'` (field absent from the serialized summary).

- [ ] **Step 3: Add the fields to `CallSummary`**

In `dto.py`, add to `CallSummary`:

```python
    published: bool = False
    is_owner: bool = False
```

- [ ] **Step 4: Thread the caller into `_summary` and set the owner on create**

In `calls.py`, update `_summary` (add the `caller_id` param and populate the two fields):

```python
def _summary(call: Call, patient_name: str | None, caller_id: UUID) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name,
        started_at=call.started_at,
        created_at=call.created_at,
        published=call.published,
        is_owner=call.initiated_by_id == caller_id,
    )
```

In `start_call`, rename the `_caller` dep to `caller`, set the owner, and pass the id to `_summary`:

```python
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
    ...
    call = Call(
        tenant_id=tenant_id,
        form_id=form.id,
        current_status=CallStatus.INITIATED,
        initiated_by_id=caller.user_id,
    )
    ...
    return ok(_summary(call, form.patient_name, caller.user_id))
```

- [ ] **Step 5: Fix the other `_summary` call site**

In `list_calls`, the return currently reads `[_summary(c, name) for c, name in rows]`. Task 5 rewrites this handler; for now update it minimally so the module imports/type-checks — rename its `_caller` dep to `caller` and pass `caller.user_id`:

```python
    return ok([_summary(c, name, caller.user_id) for c, name in rows])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -k "owner or private or list_calls or join_token" -v`
Expected: PASS (existing call tests still green; the new summary test passes).

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/schemas/dto.py \
        apps/control_plane/src/control_plane/api/v1/calls.py \
        tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): set call owner on create; surface published/is_owner

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Scope `GET /calls` to owner-or-published

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`list_calls`)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `Call.initiated_by_id`, `Call.published`, `RBACWorld.supervisor_token`.
- Produces: `GET /calls` returns only calls where `initiated_by_id == caller OR published is True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py  (import: from sqlalchemy import update)
@pytest.mark.asyncio
async def test_list_scopes_to_owner_or_published(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    call_id = created.json()["data"]["id"]

    # A non-owner (supervisor) does NOT see the admin's private call.
    before = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert all(c["id"] != call_id for c in before.json()["data"])

    # Flip published directly in the DB (publish endpoint is Task 6).
    await admin_session.execute(
        update(Call).where(Call.id == UUID(call_id)).values(published=True)
    )
    await admin_session.commit()

    after = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert any(c["id"] == call_id for c in after.json()["data"])
    # And the owner still sees their own call.
    owner = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert any(c["id"] == call_id for c in owner.json()["data"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_list_scopes_to_owner_or_published -v`
Expected: FAIL — the non-owner's `before` list still contains the private call (query returns all active calls).

- [ ] **Step 3: Add the scoping filter**

In `calls.py`, import `or_` from `sqlalchemy` and add the filter to the `list_calls` query (the `caller` dep was renamed in Task 4):

```python
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_(list(_ACTIVE_STATUSES)))
            .where(or_(Call.initiated_by_id == caller.user_id, Call.published.is_(True)))
            .order_by(Call.created_at.desc())
        )
    ).all()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -k "list" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): scope active-call list to owner or published

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `POST /calls/{call_id}/publish` — one-way, owner-only, audited

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (new route + imports)
- Modify: `apps/control_plane/src/control_plane/api/v1/common.py` (add `Audit` DI alias)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `require("calls:publish")`, `Call.initiated_by_id`, `Call.published`, `get_audit` (`deps.py`), `AuditEvent.CALL_PUBLISH` (Task 1).
- Produces: `POST /calls/{call_id}/publish` → `ResponseModel[CallSummary]`; sets `published=True` + `published_at=func.now()` once (idempotent); writes one `AuditRecord(event_type="call.publish")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py
@pytest.mark.asyncio
async def test_publish_is_owner_only_idempotent_and_audited(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    call_id = created.json()["data"]["id"]

    # Non-owner with calls:publish (supervisor) cannot publish someone else's call.
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.supervisor_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    # No-permission user is rejected too.
    norole = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.norole_token)
    )
    assert norole.status_code == 403, norole.text

    # Owner publishes.
    pub = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["published"] is True

    # One publish audit row exists.
    from vera_core.models import AuditLog
    from sqlalchemy import select
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.publish", AuditLog.resource_id == call_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1

    # Idempotent: a second publish is a no-op and adds no audit row.
    again = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert again.status_code == 200
    rows2 = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.publish", AuditLog.resource_id == call_id
            )
        )
    ).scalars().all()
    assert len(rows2) == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_publish_is_owner_only_idempotent_and_audited -v`
Expected: FAIL — `404`/`405` (route does not exist).

- [ ] **Step 3: Add the `Audit` DI alias**

In `api/v1/common.py`, add the import and alias (mirrors the existing `AuthAudit`):

```python
from control_plane.deps import (
    ...,
    get_audit,
)
from vera_core.audit import AuditSink, AuthAuditRecord, AuthAuditSink
...
Audit = Annotated[AuditSink, Depends(get_audit)]
```

- [ ] **Step 4: Add the publish route**

In `calls.py`, add imports (`Request`, `func`, `or_`, `AuditRecord`, `AuditSink`→via alias, `ActorType`, `AuditEvent`, `current_request_id`, `Audit`, `DefaultExceptionCode`, `CustomAPIException`) and the route:

```python
@router.post(
    "/calls/{call_id}/publish",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def publish_call(
    call_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[CallSummary]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS scopes to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can publish"
        )
    if not call.published:  # idempotent, one-way — no un-publish
        call.published = True
        call.published_at = func.now()
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_PUBLISH.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:publish",
                decision="allow",
                request_id=current_request_id(request),
            )
        )
    return ok(_summary(call, None, caller.user_id))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_publish_is_owner_only_idempotent_and_audited -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py \
        apps/control_plane/src/control_plane/api/v1/common.py \
        tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): add owner-only one-way publish endpoint with audit

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Gate + audit the non-owner join token

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`join_token`)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `Call.initiated_by_id`, `Call.published`, `get_audit`, `AuditEvent.CALL_INTERVENE_JOIN`.
- Produces: `join_token` returns `404` for a non-owner on a private call; mints a token **and** writes one `AuditRecord(event_type="call.intervene.join")` for a non-owner on a published call; owner path unchanged (no audit).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py
@pytest.mark.asyncio
async def test_join_token_gated_and_audited_for_non_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    call_id = created.json()["data"]["id"]

    # Non-owner on a PRIVATE call: 404 (existence not revealed).
    private = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert private.status_code == 404, private.text

    # Owner publishes.
    await client.post(f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token))

    # Non-owner on a PUBLISHED call: token + one join audit row.
    joined = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["data"]["token"].startswith("faketoken:")

    from vera_core.models import AuditLog
    from sqlalchemy import select
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.join", AuditLog.resource_id == call_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_join_token_gated_and_audited_for_non_owner -v`
Expected: FAIL — the private-call request returns `200` (no gate yet).

- [ ] **Step 3: Add the gate + audit**

In `calls.py`, add `request: Request` and `audit: Audit` params to `join_token`, then gate + audit before minting the token:

```python
    is_owner = call.initiated_by_id == caller.user_id
    if not is_owner and not call.published:
        raise NotFoundError(message="call not found")  # don't reveal a private call
    if not is_owner:
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_INTERVENE_JOIN.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:read",
                decision="allow",
                request_id=current_request_id(request),
                detail={"owner_id": str(call.initiated_by_id)},
            )
        )
    room_name = room_name_for_call(tenant_id, call.id)
    identity = f"supervisor-{caller.user_id}"
    token = livekit.mint_join_token(room_name=room_name, identity=identity)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -k "join_token" -v`
Expected: PASS (existing owner join-token test still green; new gate test passes).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): gate non-owner join tokens to published calls and audit the join

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `POST /calls/{call_id}/revoke-access` — owner ejects an intervener

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (add `remove_participant`)
- Modify: `tests/integration/control_plane/conftest.py` (`FakeLiveKit.remove_participant`)
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py` (`RevokeAccessRequest`)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (new route)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `require("calls:publish")`, `Call.initiated_by_id`, `Call.published`, `LiveKit` dep, `get_audit`, `AuditEvent.CALL_INTERVENE_REVOKE`.
- Produces: `LiveKitGateway.remove_participant(room_name: str, identity: str) -> None`; `RevokeAccessRequest{ target_user_id: UUID }`; `POST /calls/{call_id}/revoke-access` → `ResponseModel[None]`; owner-only; ejects `supervisor-{target_user_id}` and writes `AuditRecord(event_type="call.intervene.revoke")`. The call stays `published`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_calls.py
@pytest.mark.asyncio
async def test_owner_revokes_intervener_access(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit,  # FakeLiveKit session fixture
    admin_session,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    call_id = created.json()["data"]["id"]
    await client.post(f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token))

    # Non-owner cannot revoke.
    supervisor_uid = "<supervisor uuid>"  # not needed; test the owner path + a 403
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/revoke-access",
        headers=_auth(rbac_world.supervisor_token),
        json={"target_user_id": str(uuid4())},
    )
    assert forbidden.status_code == 403, forbidden.text

    # Owner revokes a target; LiveKit removal is invoked and audited.
    target = uuid4()
    revoked = await client.post(
        f"/api/v1/calls/{call_id}/revoke-access",
        headers=_auth(rbac_world.admin_token),
        json={"target_user_id": str(target)},
    )
    assert revoked.status_code == 200, revoked.text
    assert any(ident == f"supervisor-{target}" for _room, ident in fake_livekit.removed)

    from vera_core.models import AuditLog
    from sqlalchemy import select
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.revoke", AuditLog.resource_id == call_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1

    # The call is still published.
    from vera_core.models import Call
    row = (await admin_session.execute(select(Call).where(Call.id == UUID(call_id)))).scalar_one()
    assert row.published is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_owner_revokes_intervener_access -v`
Expected: FAIL — route missing (`404`/`405`) and `FakeLiveKit` has no `removed` attribute.

- [ ] **Step 3: Add `remove_participant` to the gateway + fake**

In `livekit_gateway.py`, add a method mirroring `create_sip_participant`'s connect/close pattern (verify the exact request/response type against the installed `livekit-api` at implementation — `RoomService.remove_participant` with a `RoomParticipantIdentity(room=..., identity=...)`):

```python
async def remove_participant(self, room_name: str, identity: str) -> None:
    """Eject a participant from a room (owner revoking an intervener's access)."""
    lk = api.LiveKitAPI(self._url, self._api_key, self._api_secret)
    try:
        await lk.room.remove_participant(
            api.RoomParticipantIdentity(room=room_name, identity=identity)
        )
    finally:
        await lk.aclose()
```

In `conftest.py`, add to `FakeLiveKit.__init__`: `self.removed: list[tuple[str, str]] = []`, and the method:

```python
async def remove_participant(self, room_name: str, identity: str) -> None:
    self.removed.append((room_name, identity))
```

- [ ] **Step 4: Add the request DTO**

In `dto.py`:

```python
class RevokeAccessRequest(BaseModel):
    target_user_id: UUID
```

- [ ] **Step 5: Add the revoke route**

In `calls.py` (import `RevokeAccessRequest` from `vera_core.schemas`):

```python
@router.post(
    "/calls/{call_id}/revoke-access",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def revoke_access(
    call_id: UUID,
    body: RevokeAccessRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[None]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can revoke access"
        )
    room_name = room_name_for_call(tenant_id, call.id)
    await livekit.remove_participant(room_name, f"supervisor-{body.target_user_id}")
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.CALL_INTERVENE_REVOKE.value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:publish",
            decision="allow",
            request_id=current_request_id(request),
            detail={"target_user_id": str(body.target_user_id)},
        )
    )
    return ok(None, message="Access revoked.")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/test_calls.py::test_owner_revokes_intervener_access -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/livekit_gateway.py \
        apps/control_plane/src/control_plane/api/v1/calls.py \
        packages/vera_core/src/vera_core/schemas/dto.py \
        tests/integration/control_plane/conftest.py \
        tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): add owner revoke-access endpoint (eject intervener) with audit

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Simplify pass + full CI gate

**Files:** any touched above.

- [ ] **Step 1: Run the `/simplify` skill** on the branch diff (reuse/simplification/altitude cleanup — quality only, no behavior change), per `vera-backend/CLAUDE.md`.

- [ ] **Step 2: Run the full gate**

Run: `just check`
Expected: ruff clean, `mypy --strict` clean, all pytest green (including the new call tests).

- [ ] **Step 3: Commit any simplifications**

```bash
git add -A
git commit -m "refactor(calls): simplify publish-to-others backend after review

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (spec §2 in-scope, backend items):
- Set `initiated_by_id` on create → Task 4. ✅
- `published`/`published_at` + index + migration → Task 3. ✅
- `POST /publish` owner-only, idempotent, one-way, `calls:publish` → Task 6. ✅
- Scope `GET /calls` owner-or-published → Task 5. ✅
- Gate `join-token` + audit non-owner join → Task 7. ✅
- `POST /revoke-access` + `remove_participant` + audit → Task 8. ✅
- `calls:publish` in catalog + grant → Task 1. ✅
- Use the PHI `AuditSink` in the calls router → Tasks 6–8 (via `Audit` alias). ✅
- Audit events `call.publish` / `call.intervene.join` / `call.intervene.revoke` → Task 1 (defined), 6–8 (emitted). ✅

**Out of scope confirmed absent:** no un-publish route; no `POST /interventions` typed-action endpoint; no cross-tenant path; no SSE. Frontend (spec §4.5) is deliberately a separate plan. ✅

**Type consistency:** `_summary(call, patient_name, caller_id)` signature is defined in Task 4 and used consistently in Tasks 4–6. `remove_participant(room_name, identity)`, `RevokeAccessRequest.target_user_id`, `FakeLiveKit.removed` all defined and used in Task 8. `AuditEvent.CALL_*` members defined in Task 1, referenced by value string in Tasks 6–8. ✅

**Note for the executor:** the LiveKit `remove_participant` request/response types (Step 3, Task 8) must be verified against the installed `livekit-api` version — the method name (`room.remove_participant`) and the `RoomParticipantIdentity` payload are the contract; adjust the exact symbol if the SDK differs. The integration tests use `FakeLiveKit`, so they pass regardless; a manual/worker check confirms the real call shape.

## Follow-on

A separate **frontend-wiring plan** (spec §4.5) covers: `callsSlice`, the one-way Publish button, Live Monitoring on real `listCalls()` with polling, and wiring View Live / Intervene to `join-token`. It depends on this backend contract landing first.
