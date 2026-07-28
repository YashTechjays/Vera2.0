# Per-VA Concurrent-Agent Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tenant admins set how many agent calls each VA may run concurrently; the limit is enforced with a clear 409 at the enqueue endpoint, a separate tenant-wide ceiling governs the dispatcher, and both knobs are admin-editable, tenant-scoped, and audited.

**Architecture:** `Tenant.max_agents_per_va` (existing column) becomes a true per-VA in-flight cap enforced synchronously at `PUT /patient-forms/{id}/status → in_queue` via a new `ensure_va_capacity` gate (advisory-lock + indexed count). A new `Tenant.max_concurrent_calls` column takes over the dispatcher's tenant-wide slot math (backfilled from the old value so live tenants keep today's capacity). Two new endpoints in `tenant_config.py` (GET/PATCH `/tenant/config/concurrency`) expose both knobs behind `tenant:config:manage` with auth-audit. Frontend gets a Settings card.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + pytest (backend, mypy --strict, ruff); React + Vite + TS + vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-07-28-per-va-concurrency-limits-design.md`

## Global Constraints

- Work on branch `feat/per-va-concurrency-limits` (already created; spec committed).
- Backend gate: `just check` (ruff check + format --check + mypy --strict + pytest) — run verbatim, never a subset. Integration tests need `just up` + `just migrate` (docker infra is usually already running).
- Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` — all four.
- Type hints everywhere; PEP 695 generics only; `asyncio` only (never `anyio`); comments only for non-obvious constraints, one line, no narration.
- Timestamps: DB clock (`func.now()`), never Python `datetime.now()`.
- Audit: never construct `AuditRecord`/`AuthAuditRecord` inline — use `emit_auth_event` / `emit_phi_read_audit`.
- Bounds (verbatim everywhere): `max_agents_per_va` ge=1 le=20; `max_concurrent_calls` ge=1 le=100.
- 409 message copy (verbatim): `You are at your concurrent-agent limit ({limit}). Wait for a call to finish or ask your admin to raise the limit.`
- No PHI in error messages, logs, or audit meta (these are config ints and status names — fine).
- Migration must be idempotent against BOTH DB shapes (fresh CI DB where `0001`'s `create_all` already created the column; provisioned dev DB where it doesn't exist).
- Backend cwd for all commands: `vera-backend/`. Frontend cwd: `vera-frontend/`.

---

### Task 1: `max_concurrent_calls` column + migration + in-flight index

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/tenant.py:40`
- Create: `vera-backend/migrations/versions/<generated>_tenant_concurrency_knobs.py`

**Interfaces:**
- Produces: `Tenant.max_concurrent_calls: Mapped[int]` (NOT NULL, default 25) — read by Task 2 (dispatcher) and Task 5 (config API). Partial index `ix_patient_form_in_flight` — used by Task 3's count and the dispatcher's active count.

- [ ] **Step 1: Add the column to the model**

In `tenant.py`, directly below the `max_agents_per_va` line (keep the two knobs adjacent):

```python
    max_agents_per_va: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # Tenant-wide dial ceiling (dispatcher slot math). Distinct from the per-VA
    # in-flight cap above, which gates each VA at enqueue time.
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
```

Also update the class docstring line that enumerates the runtime knobs (it lists
`max_agents_per_va, retry_fill_threshold, persona_tweak`) to include
`max_concurrent_calls`.

- [ ] **Step 2: Generate the migration file**

```bash
cd vera-backend && uv run alembic revision -m "tenant concurrency knobs"
```

Alembic auto-generates the random-hex revision ID and chains `down_revision` to the current head. Never hand-number the ID.

- [ ] **Step 3: Write the migration body**

Replace the generated stubs with (keep the generated `revision`/`down_revision` lines):

```python
#: Statements run by `upgrade()` (repo convention: exposed for migration tests).
UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Idempotent: a fresh DB already has the column via 0001's create_all off the
    # live models; only an already-provisioned DB needs the ADD (repo migration rule).
    "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS max_concurrent_calls INTEGER",
    # Behavior-preserving backfill: the dispatcher's tenant-wide cap used to read
    # max_agents_per_va, so existing tenants keep exactly their current capacity.
    "UPDATE tenant SET max_concurrent_calls = max_agents_per_va WHERE max_concurrent_calls IS NULL",
    "ALTER TABLE tenant ALTER COLUMN max_concurrent_calls SET NOT NULL",
    "ALTER TABLE tenant ALTER COLUMN max_concurrent_calls SET DEFAULT 25",
    # Serves the per-VA in-flight count (enqueue gate) and the dispatcher's active
    # count; stays small because terminal forms fall out of the predicate.
    """
    CREATE INDEX IF NOT EXISTS ix_patient_form_in_flight
    ON patient_form (tenant_id, enqueued_by_id)
    WHERE status IN ('in_queue', 'in_call', 'ai_processing')
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_patient_form_in_flight")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS max_concurrent_calls")
```

Add `from alembic import op` and the `collections.abc.Sequence` import block exactly as in `migrations/versions/20260717_1754_8c10a6182907_call_health_columns.py` (the style template for this migration).

- [ ] **Step 4: Run the migration and verify both shapes**

```bash
cd vera-backend && just migrate
docker compose exec -T postgres psql -U vera -d vera -c \
  "SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_name='tenant' AND column_name='max_concurrent_calls';"
docker compose exec -T postgres psql -U vera -d vera -c \
  "SELECT max_agents_per_va, max_concurrent_calls FROM tenant LIMIT 5;"
```

Expected: column exists, NOT NULL, default 25; every existing row has `max_concurrent_calls = max_agents_per_va`. Re-run `just migrate` once more — must be a no-op (idempotency). (Adjust the psql user/db names to `docker-compose.yml` if they differ.)

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/tenant.py vera-backend/migrations/versions/
git commit -m "feat(tenant): max_concurrent_calls ceiling column + in-flight partial index"
```

---

### Task 2: Dispatcher reads the tenant ceiling, not the per-VA knob

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py:192`
- Test: `vera-backend/tests/unit/services/test_queue_dispatcher.py`
- Modify: `vera-backend/tests/integration/control_plane/test_call_queue.py` (dispatcher-slot pinning in the two head-of-line tests)
- Modify: `vera-backend/tests/integration/test_post_call_eval.py:846` (comment only)

**Interfaces:**
- Consumes: `Tenant.max_concurrent_calls` (Task 1).
- Produces: dispatcher behavior relied on by Task 4's integration tests (per-VA gate + tenant ceiling are independent knobs).

- [ ] **Step 1: Write the failing regression test**

In `tests/unit/services/test_queue_dispatcher.py`, next to `test_dials_are_paced_one_second_apart` (same fixtures and helpers):

```python
async def test_slots_come_from_tenant_ceiling_not_per_va_knob(
    _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's tenant-wide slot math reads max_concurrent_calls; the
    per-VA knob (max_agents_per_va) is enforced at enqueue time, not here."""
    tenant = _tenant(max_agents_per_va=1, max_concurrent_calls=2)
    form_a = _form(tenant.id)
    form_b = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form_a, form_b])
    livekit = FakeLiveKit()

    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _fake_sleep)  # type: ignore[attr-defined]

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 2  # per-VA knob of 1 must NOT cap the tenant-wide pass
```

Also add `"max_concurrent_calls": 3,` to the `_tenant` defaults dict (line ~258) so every existing test constructs a complete tenant.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd vera-backend && uv run pytest tests/unit/services/test_queue_dispatcher.py::test_slots_come_from_tenant_ceiling_not_per_va_knob -q
```

Expected: FAIL — `dispatched == 1` (dispatcher still reads `max_agents_per_va=1`).

- [ ] **Step 3: Switch the dispatcher to the ceiling**

`queue_dispatcher.py` line 192:

```python
    slots = tenant.max_concurrent_calls - active_count
```

Update the comment block above `_ACTIVE_FORM_STATUSES` (line ~80) from "the tenant's concurrency cap" wording if it names the old column; it should say the statuses count toward `max_concurrent_calls`.

- [ ] **Step 4: Update the tests that used the old knob to pinch dispatcher slots**

- `tests/unit/services/test_queue_dispatcher.py`: change `_tenant(max_agents_per_va=5)` → `_tenant(max_concurrent_calls=5)` at the three call sites (lines ~422, ~444, ~652 — `grep -n "max_agents_per_va=5"` to find them).
- `tests/integration/control_plane/test_call_queue.py`: the two head-of-line tests pin slots to 1 by selecting/updating/restoring `Tenant.max_agents_per_va` (lines ~569-576, ~605-610, and the same pattern in `test_open_provider_form_dispatches_through_the_sql_hours_gate`). Switch every one of those reads/writes to `Tenant.max_concurrent_calls` / `values(max_concurrent_calls=...)` — `grep -n "max_agents_per_va" tests/integration/control_plane/test_call_queue.py` and convert all hits.
- `tests/integration/test_post_call_eval.py:846`: the comment "Because max_agents_per_va=3 and no forms are active" → "Because max_concurrent_calls leaves free slots and no forms are active".

- [ ] **Step 5: Run the affected suites**

```bash
cd vera-backend && uv run pytest tests/unit/services/test_queue_dispatcher.py -q
uv run pytest tests/integration/control_plane/test_call_queue.py tests/integration/test_post_call_eval.py -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py vera-backend/tests/
git commit -m "feat(dispatch): tenant-wide slot math reads max_concurrent_calls"
```

---

### Task 3: `ensure_va_capacity` gate helper

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/queueability.py`
- Test: `vera-backend/tests/unit/control_plane/test_queueability.py`

**Interfaces:**
- Consumes: `Tenant.max_agents_per_va`, `PatientForm.enqueued_by_id`, `FormStatus`.
- Produces: `async def ensure_va_capacity(session: AsyncSession, tenant: Tenant, caller_user_id: UUID) -> None` (raises `CustomAPIException(DefaultExceptionCode.CONFLICT)` at limit) and `IN_FLIGHT_FORM_STATUSES: tuple[str, ...]` — both imported by Task 4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control_plane/test_queueability.py` (reuse its existing import style):

```python
from uuid import uuid4

from control_plane.queueability import IN_FLIGHT_FORM_STATUSES, ensure_va_capacity
from vera_core.models import Tenant


class _CountResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CapacitySession:
    """First execute() is the advisory lock, second is the in-flight count."""

    def __init__(self, in_flight: int) -> None:
        self._in_flight = in_flight
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _CountResult:
        self.executed.append(stmt)
        return _CountResult(self._in_flight)


def _capacity_tenant(limit: int) -> Tenant:
    return cast(Tenant, SimpleNamespace(id=uuid4(), max_agents_per_va=limit))


@pytest.mark.asyncio
async def test_below_limit_passes() -> None:
    session = _CapacitySession(in_flight=2)
    await ensure_va_capacity(
        cast(AsyncSession, session), _capacity_tenant(3), uuid4()
    )  # no raise
    assert len(session.executed) == 2  # advisory lock, then count


@pytest.mark.asyncio
async def test_at_limit_raises_conflict_with_actionable_data() -> None:
    session = _CapacitySession(in_flight=3)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_va_capacity(cast(AsyncSession, session), _capacity_tenant(3), uuid4())
    assert "concurrent-agent limit (3)" in str(exc.value.message)
    assert exc.value.data == {"limit": 3, "in_flight": 3}


@pytest.mark.asyncio
async def test_over_limit_raises() -> None:
    # Defensive: a count already past the limit (e.g. limit was lowered) still gates.
    session = _CapacitySession(in_flight=5)
    with pytest.raises(CustomAPIException):
        await ensure_va_capacity(cast(AsyncSession, session), _capacity_tenant(3), uuid4())


def test_in_flight_statuses_are_queue_plus_active() -> None:
    assert set(IN_FLIGHT_FORM_STATUSES) == {"in_queue", "in_call", "ai_processing"}
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/unit/control_plane/test_queueability.py -q
```

Expected: FAIL — `ImportError: cannot import name 'ensure_va_capacity'`.

- [ ] **Step 3: Implement the helper**

In `queueability.py`:

1. Update the module docstring's last paragraph — concurrency IS now checked here (per-VA capacity), while working hours remain dispatcher-only:

```python
"""Enqueue-time gates for `PUT /patient-forms/{id}/status` → IN_QUEUE, run BEFORE the
state-machine transition: `ensure_queueable` rejects a form that could never be dialed
(no payer phone, no outbound trunk); `ensure_va_capacity` rejects an enqueue that would
put the caller past the tenant's per-VA in-flight limit. Working hours stay dial-time
concerns the dispatcher handles.
"""
```

2. Add imports: `from uuid import UUID`, `from sqlalchemy import func, select`, `from vera_core.models import PatientForm` (move `PatientForm` out of the `TYPE_CHECKING` block — it's a runtime query target now), `from vera_core.models.enums import FormStatus`; add `Tenant` to the `TYPE_CHECKING` imports.

3. Add below `ensure_queueable`:

```python
# Distinct namespace from the dispatcher's _DISPATCH_LOCK_CLASS (0x76455241 "vERA").
_ENQUEUE_LOCK_CLASS = 0x76455251  # "vERQ"

# The per-VA in-flight set: a queued form is a claimed agent slot, not just a live call.
IN_FLIGHT_FORM_STATUSES: tuple[str, ...] = (
    FormStatus.IN_QUEUE.value,
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)


async def ensure_va_capacity(
    session: "AsyncSession", tenant: "Tenant", caller_user_id: UUID
) -> None:
    """Raise CONFLICT if the caller is already at the tenant's per-VA in-flight limit."""
    # Transaction-scoped advisory lock serializes same-VA concurrent enqueues (the
    # double-click race) so two counts can't both pass at limit-1; releases on
    # commit/rollback. Different VAs hash to different locks — no cross-VA contention.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _ENQUEUE_LOCK_CLASS, func.hashtext(f"{tenant.id}:{caller_user_id}")
            )
        )
    )
    in_flight: int = (
        await session.execute(
            select(func.count())
            .select_from(PatientForm)
            .where(
                PatientForm.tenant_id == tenant.id,
                PatientForm.enqueued_by_id == caller_user_id,
                PatientForm.status.in_(IN_FLIGHT_FORM_STATUSES),
            )
        )
    ).scalar_one()
    if in_flight >= tenant.max_agents_per_va:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=(
                f"You are at your concurrent-agent limit ({tenant.max_agents_per_va}). "
                "Wait for a call to finish or ask your admin to raise the limit."
            ),
            data={"limit": tenant.max_agents_per_va, "in_flight": in_flight},
        )
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd vera-backend && uv run pytest tests/unit/control_plane/test_queueability.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/queueability.py vera-backend/tests/unit/control_plane/test_queueability.py
git commit -m "feat(queueability): ensure_va_capacity per-VA in-flight gate"
```

---

### Task 4: Wire the gate into the enqueue endpoint + integration tests

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:1232-1268`
- Test: `vera-backend/tests/integration/control_plane/test_call_queue.py`

**Interfaces:**
- Consumes: `ensure_va_capacity(session, tenant, caller.user_id)` (Task 3).
- Produces: the 409 HTTP contract (`code=CONFLICT`, `data={"limit", "in_flight"}`) the frontend surfaces in Task 7.

- [ ] **Step 1: Wire the endpoint**

In `update_patient_form_status`:

1. Move the tenant load (currently below the gates, line ~1267-1268: `# Load tenant for state machine guard (retry cap).` + the `select(Tenant)` line) UP to directly above the `# Hard dialability gate` comment (line ~1232), keeping the comment but generalizing it: `# Load tenant for the capacity gate and the state-machine retry cap.`
2. In the `if target == FormStatus.IN_QUEUE:` gate block, call the capacity gate right after `ensure_queueable`:

```python
    if target == FormStatus.IN_QUEUE:
        await ensure_queueable(session, kms, form)
        await ensure_va_capacity(session, tenant, caller.user_id)
```

3. Import: extend the existing `from control_plane.queueability import ensure_queueable` line with `ensure_va_capacity`.
4. Add `DefaultExceptionCode.CONFLICT` to the route's `responses=CustomAPIResponse.custom(...)` if not already listed (it already is — verify, don't duplicate).

- [ ] **Step 2: Add a two-form seeding fixture**

In `tests/integration/control_plane/test_call_queue.py`, next to `queue_form_id`:

```python
@pytest.fixture
async def queue_form_pair(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[tuple[UUID, UUID]]:
    """Two dialable queue-test forms for the per-VA capacity tests."""
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async for form_a in _seed_ready_form(
            sessionmaker, rbac_world.tenant_id, phone=_DIALABLE_PHONE
        ):
            async for form_b in _seed_ready_form(
                sessionmaker, rbac_world.tenant_id, phone=_DIALABLE_PHONE
            ):
                yield (form_a, form_b)
    finally:
        await engine.dispose()
```

- [ ] **Step 3: Add a limit-pinning helper fixture**

```python
@pytest.fixture
async def va_limit_one(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncGenerator[None]:
    """Pin the tenant's per-VA limit to 1 for the duration of a test, then restore."""
    async with admin_sessionmaker() as session, session.begin():
        old = (
            await session.execute(
                select(Tenant.max_agents_per_va).where(Tenant.id == rbac_world.tenant_id)
            )
        ).scalar_one()
        await session.execute(
            update(Tenant).where(Tenant.id == rbac_world.tenant_id).values(max_agents_per_va=1)
        )
    yield
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            update(Tenant).where(Tenant.id == rbac_world.tenant_id).values(max_agents_per_va=old)
        )
```

- [ ] **Step 4: Write the integration tests**

```python
@pytest.mark.asyncio
async def test_enqueue_rejected_when_va_at_limit(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_pair: tuple[UUID, UUID],
    trunk_configured: None,
    va_limit_one: None,
) -> None:
    """Second enqueue by the same VA at limit 1 → 409 with actionable data; the
    form is left untouched in READY_FOR_PROCESSING."""
    form_a, form_b = queue_form_pair
    first = await client.put(
        f"/api/v1/patient-forms/{form_a}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        f"/api/v1/patient-forms/{form_b}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert second.status_code == 409, second.text
    envelope = second.json()
    assert "concurrent-agent limit (1)" in envelope["message"]
    assert envelope["data"] == {"limit": 1, "in_flight": 1}

    # Untouched: still manually re-queueable later.
    detail = await client.get(
        f"/api/v1/patient-forms/{form_b}",
        headers=_auth(rbac_world.admin_token),
    )
    assert detail.json()["data"]["status"] == "ready_for_processing"


@pytest.mark.asyncio
async def test_va_limit_does_not_block_other_vas(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_pair: tuple[UUID, UUID],
    trunk_configured: None,
    va_limit_one: None,
) -> None:
    """The limit is per VA: a different user with forms:write enqueues freely while
    the first VA is at their cap. Uses the supervisor persona (holds forms:write in
    the RBAC world — see conftest role grants)."""
    form_a, form_b = queue_form_pair
    first = await client.put(
        f"/api/v1/patient-forms/{form_a}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        f"/api/v1/patient-forms/{form_b}/status",
        headers=_auth(rbac_world.supervisor_token),
        json={"status": "in_queue"},
    )
    assert second.status_code == 200, second.text


@pytest.mark.asyncio
async def test_slot_frees_when_form_leaves_in_flight(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_pair: tuple[UUID, UUID],
    trunk_configured: None,
    va_limit_one: None,
) -> None:
    """A form leaving the in-flight set (here: parked in EXCEPTION_REVIEW) frees the
    VA's slot and the next enqueue succeeds."""
    form_a, form_b = queue_form_pair
    first = await client.put(
        f"/api/v1/patient-forms/{form_a}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert first.status_code == 200, first.text
    await drain_pending()  # let the post-commit dispatch task settle before mutating

    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE patient_form SET status = 'exception_review' WHERE id = :fid").bindparams(
                fid=form_a
            )
        )

    second = await client.put(
        f"/api/v1/patient-forms/{form_b}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert second.status_code == 200, second.text


@pytest.mark.asyncio
async def test_concurrent_enqueues_at_limit_admit_exactly_one(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_pair: tuple[UUID, UUID],
    trunk_configured: None,
    va_limit_one: None,
) -> None:
    """Two simultaneous enqueues by the same VA with one slot: the advisory lock
    serializes the count → exactly one 200 and one 409, never two 200s."""
    form_a, form_b = queue_form_pair
    responses = await asyncio.gather(
        client.put(
            f"/api/v1/patient-forms/{form_a}/status",
            headers=_auth(rbac_world.admin_token),
            json={"status": "in_queue"},
        ),
        client.put(
            f"/api/v1/patient-forms/{form_b}/status",
            headers=_auth(rbac_world.admin_token),
            json={"status": "in_queue"},
        ),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
```

Add `import asyncio` to the file's imports if absent. If the detail endpoint's response shape differs (`data.status` path), assert via `admin_sessionmaker` + `SELECT status FROM patient_form` instead, mirroring `test_enqueue_stamps_enqueued_by_id`.

- [ ] **Step 5: Run the integration tests**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_call_queue.py -q
```

Expected: all PASS (new and pre-existing).

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py vera-backend/tests/integration/control_plane/test_call_queue.py
git commit -m "feat(forms): enforce per-VA in-flight limit at enqueue with clear 409"
```

---

### Task 5: Concurrency config API (schemas, enum, endpoints, tests)

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/schemas/dto.py`
- Modify: `vera-backend/packages/vera_core/src/vera_core/schemas/__init__.py`
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py:216`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/tenant_config.py`
- Test: `vera-backend/tests/unit/http/test_concurrency_config.py` (create)
- Test: `vera-backend/tests/integration/control_plane/test_tenant_config.py`

**Interfaces:**
- Consumes: `Tenant.max_agents_per_va`, `Tenant.max_concurrent_calls` (Task 1).
- Produces: `GET/PATCH /api/v1/tenant/config/concurrency` returning `{"max_agents_per_va": int, "max_concurrent_calls": int}` inside the standard `ok()` envelope — consumed by Task 6's frontend module. `AuthEvent.CONCURRENCY_CONFIG_UPDATED = "concurrency_config_updated"`.

- [ ] **Step 1: Write the failing unit test file**

Create `tests/unit/http/test_concurrency_config.py` as a sibling of `test_retention_policy.py`, copying its app-factory scaffolding wholesale (fakes, `_build_app`, `_client`, fixtures) with these deltas — different fake tenant, path, and permission:

```python
"""Unit tests for GET/PATCH /api/v1/tenant/config/concurrency.

Same no-DB harness as test_retention_policy.py: heavyweight deps are stubbed via
dependency_overrides and app.state injection, so only tenant_config.py is under test.
"""

_PATH = "/api/v1/tenant/config/concurrency"
_MANAGE = frozenset({"tenant:config:manage"})


class _FakeTenant:
    """Minimal Tenant stand-in — only the columns the endpoints touch."""

    def __init__(self, max_agents_per_va: int = 3, max_concurrent_calls: int = 25) -> None:
        self.max_agents_per_va = max_agents_per_va
        self.max_concurrent_calls = max_concurrent_calls
```

(Reuse `_FakeResult`, `_FakeSession`, `_FakeResolver`, `_SpyAuthAudit`, `_NullAudit`, `_build_app`, `_client`, and the `tenant`/`spy`/`client` fixtures verbatim from `test_retention_policy.py`, with `permissions=_MANAGE`.)

Tests:

```python
async def test_get_returns_both_knobs(client: httpx.AsyncClient) -> None:
    resp = await client.get(_PATH)

    assert resp.status_code == 200
    assert resp.json()["data"] == {"max_agents_per_va": 3, "max_concurrent_calls": 25}


async def test_patch_one_knob_leaves_the_other(
    client: httpx.AsyncClient, tenant: _FakeTenant, spy: _SpyAuthAudit
) -> None:
    resp = await client.patch(_PATH, json={"max_agents_per_va": 5})

    assert resp.status_code == 200
    assert resp.json()["data"] == {"max_agents_per_va": 5, "max_concurrent_calls": 25}
    assert tenant.max_agents_per_va == 5
    assert tenant.max_concurrent_calls == 25
    assert any(
        r.event_type == "concurrency_config_updated"
        and r.meta
        == {
            "old": {"max_agents_per_va": 3, "max_concurrent_calls": 25},
            "new": {"max_agents_per_va": 5, "max_concurrent_calls": 25},
        }
        for r in spy.records
    )


async def test_patch_both_knobs(client: httpx.AsyncClient, tenant: _FakeTenant) -> None:
    resp = await client.patch(
        _PATH, json={"max_agents_per_va": 2, "max_concurrent_calls": 40}
    )

    assert resp.status_code == 200
    assert tenant.max_agents_per_va == 2
    assert tenant.max_concurrent_calls == 40


@pytest.mark.parametrize(
    "body",
    [
        {"max_agents_per_va": 0},
        {"max_agents_per_va": 21},
        {"max_concurrent_calls": 0},
        {"max_concurrent_calls": 101},
    ],
)
async def test_out_of_bounds_is_422(client: httpx.AsyncClient, body: dict[str, int]) -> None:
    resp = await client.patch(_PATH, json=body)

    assert resp.status_code == 422


async def test_caller_without_permission_gets_403(
    tenant: _FakeTenant, spy: _SpyAuthAudit
) -> None:
    app = _build_app(permissions=frozenset(), tenant=tenant, spy=spy)
    async with _client(app) as client:
        resp = await client.get(_PATH)

    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify failure**

```bash
cd vera-backend && uv run pytest tests/unit/http/test_concurrency_config.py -q
```

Expected: FAIL with 404s (route doesn't exist yet).

- [ ] **Step 3: Add the schemas**

In `packages/vera_core/src/vera_core/schemas/dto.py`, below `RetentionPolicyUpdate`:

```python
class ConcurrencyConfig(BaseModel):
    """Tenant concurrency knobs: the per-VA in-flight cap (enqueue gate) and the
    tenant-wide dial ceiling (dispatcher slot math)."""

    max_agents_per_va: int = Field(ge=1, le=20)
    max_concurrent_calls: int = Field(ge=1, le=100)


class ConcurrencyConfigUpdate(BaseModel):
    """PATCH body: omitted knobs stay unchanged."""

    max_agents_per_va: int | None = Field(default=None, ge=1, le=20)
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=100)
```

Export both from `schemas/__init__.py` (import line + `__all__`, alphabetical with the existing entries).

- [ ] **Step 4: Add the audit event**

In `models/enums.py`, below `RETENTION_POLICY_UPDATED`:

```python
    # Tenant concurrency knobs updated (old/new integer values, no PHI).
    CONCURRENCY_CONFIG_UPDATED = "concurrency_config_updated"
```

- [ ] **Step 5: Add the endpoints**

In `tenant_config.py`, below the retention endpoints (mirror their structure exactly):

```python
def _concurrency_config(tenant: Tenant) -> ConcurrencyConfig:
    return ConcurrencyConfig(
        max_agents_per_va=tenant.max_agents_per_va,
        max_concurrent_calls=tenant.max_concurrent_calls,
    )


@router.get(
    "/tenant/config/concurrency",
    response_model=ResponseModel[ConcurrencyConfig],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_concurrency_config(
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[ConcurrencyConfig]:
    tenant = await _load_tenant(session, tenant_id)
    return ok(_concurrency_config(tenant))


@router.patch(
    "/tenant/config/concurrency",
    response_model=ResponseModel[ConcurrencyConfig],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def patch_concurrency_config(
    body: ConcurrencyConfigUpdate,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[ConcurrencyConfig]:
    tenant = await _load_tenant(session, tenant_id)
    old = _concurrency_config(tenant)
    if body.max_agents_per_va is not None:
        tenant.max_agents_per_va = body.max_agents_per_va
    if body.max_concurrent_calls is not None:
        tenant.max_concurrent_calls = body.max_concurrent_calls
    new = _concurrency_config(tenant)
    # Policy-change before/after, same precedent as retention. Config ints, not PHI.
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.CONCURRENCY_CONFIG_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"old": old.model_dump(), "new": new.model_dump()},
    )
    return ok(new)
```

Extend the `from vera_core.schemas import ...` import with `ConcurrencyConfig, ConcurrencyConfigUpdate`.

- [ ] **Step 6: Run unit tests to verify they pass**

```bash
cd vera-backend && uv run pytest tests/unit/http/test_concurrency_config.py -q
```

Expected: all PASS.

- [ ] **Step 7: Add integration tests (RLS + real audit row)**

Append to `tests/integration/control_plane/test_tenant_config.py`:

```python
CONCURRENCY_PATH = "/api/v1/tenant/config/concurrency"


async def test_concurrency_get_patch_round_trip(
    client: httpx.AsyncClient, rbac_world: RBACWorld, admin_session: AsyncSession
) -> None:
    got = await client.get(CONCURRENCY_PATH, headers=_auth(rbac_world.admin_token))
    assert got.status_code == 200
    before = got.json()["data"]
    assert set(before) == {"max_agents_per_va", "max_concurrent_calls"}

    patched = await client.patch(
        CONCURRENCY_PATH,
        json={"max_agents_per_va": 4},
        headers=_auth(rbac_world.admin_token),
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["max_agents_per_va"] == 4

    # Auth-audit row with before/after values landed for this tenant.
    rows = (
        (
            await admin_session.execute(
                select(AuthAuditLog).where(
                    AuthAuditLog.tenant_id == rbac_world.tenant_id,
                    AuthAuditLog.event_type == "concurrency_config_updated",
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(r.meta["new"]["max_agents_per_va"] == 4 for r in rows)

    # Restore so other tests in the shared world see the original value.
    restore = await client.patch(
        CONCURRENCY_PATH,
        json={"max_agents_per_va": before["max_agents_per_va"]},
        headers=_auth(rbac_world.admin_token),
    )
    assert restore.status_code == 200


async def test_concurrency_denied_without_permission(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(CONCURRENCY_PATH, headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403
```

If `AuthAuditLog.meta` is named differently (check the model — it may be `detail` or `meta_json`), adjust the attribute access to match; `emit_auth_event`'s meta lands in that column.

- [ ] **Step 8: Run integration tests**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_tenant_config.py -q
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add -A vera-backend/packages/vera_core/src/vera_core/schemas/ vera-backend/packages/vera_core/src/vera_core/models/enums.py vera-backend/apps/control_plane/src/control_plane/api/v1/tenant_config.py vera-backend/tests/
git commit -m "feat(config): tenant concurrency knobs API with auth-audit"
```

---

### Task 6: Frontend API module

**Files:**
- Create: `vera-frontend/src/lib/api/tenantConfig.ts`
- Test: `vera-frontend/src/lib/api/tenantConfig.test.ts`

**Interfaces:**
- Consumes: Task 5's endpoints; `apiRequest` from `@/lib/api/client`.
- Produces: `ConcurrencyConfig` type, `getConcurrencyConfig(): Promise<ConcurrencyConfig>`, `patchConcurrencyConfig(patch: Partial<ConcurrencyConfig>): Promise<ConcurrencyConfig>` — consumed by Task 7.

- [ ] **Step 1: Write the failing test**

Mirror the mocking style of the sibling api tests (e.g. `calls.test.ts` — check whether it uses `vi.mock` of the client module or a fetch mock, and copy that). With client-module mocking:

```typescript
import { beforeEach, describe, expect, it, vi } from "vitest"

import { apiRequest } from "@/lib/api/client"
import { getConcurrencyConfig, patchConcurrencyConfig } from "@/lib/api/tenantConfig"

vi.mock("@/lib/api/client", () => ({
  apiRequest: vi.fn(),
}))

const mockedApiRequest = vi.mocked(apiRequest)

describe("tenantConfig api", () => {
  beforeEach(() => {
    mockedApiRequest.mockReset()
  })

  it("GETs the concurrency config", async () => {
    const config = { max_agents_per_va: 3, max_concurrent_calls: 25 }
    mockedApiRequest.mockResolvedValueOnce(config)

    await expect(getConcurrencyConfig()).resolves.toEqual(config)
    expect(mockedApiRequest).toHaveBeenCalledWith("/tenant/config/concurrency")
  })

  it("PATCHes only the provided knobs", async () => {
    const config = { max_agents_per_va: 5, max_concurrent_calls: 25 }
    mockedApiRequest.mockResolvedValueOnce(config)

    await expect(patchConcurrencyConfig({ max_agents_per_va: 5 })).resolves.toEqual(config)
    expect(mockedApiRequest).toHaveBeenCalledWith("/tenant/config/concurrency", {
      method: "PATCH",
      body: { max_agents_per_va: 5 },
    })
  })
})
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd vera-frontend && npm test -- tenantConfig
```

Expected: FAIL — module `@/lib/api/tenantConfig` not found.

- [ ] **Step 3: Implement the module**

```typescript
// Typed wrappers over the tenant concurrency-config endpoints (gated by
// tenant:config:manage server-side). Mirrors the backend contract (snake_case).

import { apiRequest } from "@/lib/api/client"

/** Both knobs: per-VA in-flight cap and the tenant-wide dial ceiling. */
export type ConcurrencyConfig = {
  max_agents_per_va: number
  max_concurrent_calls: number
}

export function getConcurrencyConfig(): Promise<ConcurrencyConfig> {
  return apiRequest<ConcurrencyConfig>("/tenant/config/concurrency")
}

/** PATCH semantics: omitted knobs stay unchanged. */
export function patchConcurrencyConfig(
  patch: Partial<ConcurrencyConfig>,
): Promise<ConcurrencyConfig> {
  return apiRequest<ConcurrencyConfig>("/tenant/config/concurrency", {
    method: "PATCH",
    body: patch,
  })
}
```

If sibling modules pass an `Idempotency-Key` header on mutations (see `roles.ts::updateRole` — it does NOT for PATCH-without-idempotency-dep endpoints; check whether the backend route added `require_idempotency_key`; ours did not), keep the call header-free to match the backend contract.

- [ ] **Step 4: Run to verify it passes**

```bash
cd vera-frontend && npm test -- tenantConfig
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/tenantConfig.ts vera-frontend/src/lib/api/tenantConfig.test.ts
git commit -m "feat(fe): tenant concurrency-config API module"
```

---

### Task 7: ConcurrencySection settings card + page wiring

**Files:**
- Create: `vera-frontend/src/components/settings/ConcurrencySection.tsx`
- Test: `vera-frontend/src/components/settings/ConcurrencySection.test.tsx`
- Modify: `vera-frontend/src/pages/Settings.tsx`

**Interfaces:**
- Consumes: Task 6's module; `SettingsCard`; `usePermission` from `@/lib/auth/permissions`; `ApiError` from `@/lib/api/client`.
- Produces: `<ConcurrencySection />` rendered on the Settings page for holders of `tenant:config:manage`.

- [ ] **Step 1: Write the failing component test**

Follow the file's sibling component tests for render/query utilities (check for an existing `renderWithProviders` helper or plain `@testing-library/react`):

```tsx
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConcurrencySection } from "@/components/settings/ConcurrencySection"
import { getConcurrencyConfig, patchConcurrencyConfig } from "@/lib/api/tenantConfig"

vi.mock("@/lib/api/tenantConfig", () => ({
  getConcurrencyConfig: vi.fn(),
  patchConcurrencyConfig: vi.fn(),
}))

const mockedGet = vi.mocked(getConcurrencyConfig)
const mockedPatch = vi.mocked(patchConcurrencyConfig)

describe("ConcurrencySection", () => {
  beforeEach(() => {
    mockedGet.mockReset()
    mockedPatch.mockReset()
    mockedGet.mockResolvedValue({ max_agents_per_va: 3, max_concurrent_calls: 25 })
  })

  it("loads and renders both knobs", async () => {
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity")) // expand the card

    await waitFor(() => {
      expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3)
      expect(screen.getByLabelText(/tenant call ceiling/i)).toHaveValue(25)
    })
  })

  it("saves changed knobs via PATCH", async () => {
    mockedPatch.mockResolvedValue({ max_agents_per_va: 5, max_concurrent_calls: 25 })
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity"))
    await waitFor(() => expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3))

    const perVa = screen.getByLabelText(/agents per va/i)
    await userEvent.clear(perVa)
    await userEvent.type(perVa, "5")
    await userEvent.click(screen.getByRole("button", { name: /save/i }))

    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith({
        max_agents_per_va: 5,
        max_concurrent_calls: 25,
      }),
    )
  })

  it("surfaces the API error message on a failed save", async () => {
    const { ApiError } = await import("@/lib/api/client")
    mockedPatch.mockRejectedValue(
      new ApiError("Validation failed.", 422, { code: "VALIDATION_ERROR" }),
    )
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity"))
    await waitFor(() => expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3))

    await userEvent.click(screen.getByRole("button", { name: /save/i }))

    await waitFor(() => expect(screen.getByText("Validation failed.")).toBeInTheDocument())
  })
})
```

Check `ApiError`'s actual constructor signature in `src/lib/api/client.ts` and match it in the test (message/status/envelope argument order).

- [ ] **Step 2: Run to verify it fails**

```bash
cd vera-frontend && npm test -- ConcurrencySection
```

Expected: FAIL — component module not found.

- [ ] **Step 3: Implement the component**

```tsx
import { useEffect, useState } from "react"

import { SettingsCard } from "@/components/settings/SettingsCard"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import {
  getConcurrencyConfig,
  patchConcurrencyConfig,
  type ConcurrencyConfig,
} from "@/lib/api/tenantConfig"

/** Admin knobs for agent concurrency: per-VA in-flight cap + tenant dial ceiling. */
export function ConcurrencySection() {
  const [config, setConfig] = useState<ConcurrencyConfig | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getConcurrencyConfig()
      .then(setConfig)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load capacity settings."),
      )
  }, [])

  const save = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    try {
      setConfig(await patchConcurrencyConfig(config))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save capacity settings.")
    } finally {
      setSaving(false)
    }
  }

  const setKnob = (key: keyof ConcurrencyConfig, raw: string) => {
    const value = Number(raw)
    if (config && Number.isInteger(value)) setConfig({ ...config, [key]: value })
  }

  const ceilingBelowPerVa =
    config !== null && config.max_concurrent_calls < config.max_agents_per_va

  return (
    <SettingsCard
      title="Agent capacity"
      description="How many agent calls each VA may run at once, and the tenant-wide ceiling across all VAs."
    >
      {config && (
        <div className="space-y-4">
          <div className="grid gap-1.5">
            <Label htmlFor="max-agents-per-va">Agents per VA (1–20)</Label>
            <Input
              id="max-agents-per-va"
              type="number"
              min={1}
              max={20}
              value={config.max_agents_per_va}
              onChange={(e) => setKnob("max_agents_per_va", e.target.value)}
              className="max-w-32"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="max-concurrent-calls">Tenant call ceiling (1–100)</Label>
            <Input
              id="max-concurrent-calls"
              type="number"
              min={1}
              max={100}
              value={config.max_concurrent_calls}
              onChange={(e) => setKnob("max_concurrent_calls", e.target.value)}
              className="max-w-32"
            />
          </div>
          {ceilingBelowPerVa && (
            <p className="text-sm text-muted-foreground">
              The tenant ceiling is below the per-VA limit, so the ceiling will apply first.
            </p>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      )}
      {!config && error && <p className="text-sm text-destructive">{error}</p>}
    </SettingsCard>
  )
}
```

Adjust `Input`/`Label`/`Button` import paths to the actual `@/components/ui/*` exports used by sibling sections (open `ApiKeysSection.tsx` and copy its form-control imports).

- [ ] **Step 4: Wire into the Settings page**

In `src/pages/Settings.tsx`:

```tsx
import { ConcurrencySection } from "@/components/settings/ConcurrencySection"
// inside the component:
const canManageTenantConfig = usePermission("tenant:config:manage")
// in the JSX list of sections:
{canManageTenantConfig && <ConcurrencySection />}
```

- [ ] **Step 5: Run tests**

```bash
cd vera-frontend && npm test -- ConcurrencySection
```

Expected: PASS.

- [ ] **Step 6: Verify the VA-facing 409 surfaces (no code expected)**

The enqueue UI already renders `ApiError.message` on a failed status change (`src/components/ibv/IbvProvider.tsx` — `err instanceof ApiError ? err.message : "Could not change the status."`), so the backend's 409 copy surfaces as-is. Confirm by reading that catch block; only if some enqueue path swallows errors silently does this task grow a fix there.

- [ ] **Step 7: Commit**

```bash
git add vera-frontend/src/components/settings/ConcurrencySection.tsx vera-frontend/src/components/settings/ConcurrencySection.test.tsx vera-frontend/src/pages/Settings.tsx
git commit -m "feat(fe): agent-capacity settings card"
```

---

### Task 8: Full gates, simplify pass, final verification

**Files:** none new — verification and cleanup only.

- [ ] **Step 1: Backend full gate**

```bash
cd vera-backend && just check
```

Expected: ruff (check + format), mypy --strict, pytest all green. Fix anything red before proceeding.

- [ ] **Step 2: Frontend full gate**

```bash
cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Expected: all four green.

- [ ] **Step 3: Boot check (repo rule for changed background-loop inputs)**

The dispatcher input changed (new column), so boot and idle-watch:

```bash
cd vera-backend && just up && just migrate && just api
```

Watch a couple of sweep intervals of logs for dispatcher/sweeper errors, then stop. No telephony needed (`LOCAL_KMS_MASTER_KEY` + `VERA_LIVEKIT_URL` per README).

- [ ] **Step 4: Run the simplify pass (repo-mandated)**

Run the repo's mandated post-implementation simplify pass over the changed files (repo-root `CLAUDE.md`: trigger the code-simplifier / `/simplify` flow). Apply its refinements.

- [ ] **Step 5: Re-run both gates on the exact final tree**

```bash
cd vera-backend && just check
cd ../vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Expected: all green (mandatory after simplify touches anything).

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: per-VA concurrent-agent limits with tenant ceiling and admin config"
```

---

## Acceptance-criteria traceability

| AC | Where satisfied |
|----|-----------------|
| 1. Admin sets per-VA max; enforced at call initiation | Task 5 (PATCH endpoint), Tasks 3+4 (enqueue gate — the VA's call-initiation action), Task 2 (ceiling keeps dispatch bounded) |
| 2. Clear error, not silent failure | Task 3 (409 with message + `{limit, in_flight}`), Task 4 (HTTP contract tests), Task 7 step 6 (UI surfaces the message) |
| 3. Tenant-scoped + audited | Task 5 (RLS-scoped `TenantSession`; `emit_auth_event` old/new meta; integration test asserts the audit row) |
