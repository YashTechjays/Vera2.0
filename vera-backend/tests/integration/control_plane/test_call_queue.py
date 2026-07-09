"""Integration tests for the call queue & dispatch lifecycle.

Exercises the full flow: enqueue form → dispatcher fires → call created →
call terminal status reported → auto-retry → dispatcher fires again.
Runs against live RLS-enforcing Postgres with FakeLiveKit.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import FakePostCallBus, RBACWorld
from vera_core.db import tenant_session, uuid7
from vera_core.models import Call, CallLineage, PatientForm, Tenant
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import CallStatus, FormStatus, InsuranceType


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _purge_form(session: AsyncSession, form_id: UUID) -> None:
    """Delete a form and its dispatch artifacts (call_event → call → patient_form),
    in FK order. Caller owns the transaction."""
    await session.execute(
        text(
            "DELETE FROM call_event WHERE call_id IN (SELECT id FROM call WHERE form_id = :fid)"
        ).bindparams(fid=form_id)
    )
    await session.execute(text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=form_id))
    await session.execute(text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=form_id))


@pytest.fixture
async def queue_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """Seed a PatientForm in READY_FOR_PROCESSING for queue tests.

    `form_schema.insurance_type` is a globally UNIQUE catalog key and CI seeds the
    INFERTILITY_TREATMENT schema before pytest, so the schema is find-or-create;
    teardown only drops the schema chain this fixture actually created.
    """
    patient_form_id = uuid7()

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session, session.begin():
            schema = (
                await session.execute(
                    select(FormSchema).where(
                        FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                    )
                )
            ).scalar_one_or_none()
            created_schema = schema is None
            if schema is None:
                schema = FormSchema(
                    id=uuid7(),
                    insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                    name="Queue Test Schema",
                )
                session.add(schema)
                await session.flush()
                schema_version_id = uuid7()
                session.add(
                    SchemaVersion(
                        id=schema_version_id,
                        schema_id=schema.id,
                        version=1,
                        schema_json={},
                    )
                )
            else:
                schema_version_id = (
                    await session.execute(
                        select(SchemaVersion.id)
                        .where(SchemaVersion.schema_id == schema.id)
                        .order_by(SchemaVersion.version.desc())
                        .limit(1)
                    )
                ).scalar_one()
            form_schema_id = schema.id
            session.add(
                PatientForm(
                    id=patient_form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="Queue Test Patient",
                )
            )

        yield patient_form_id

        async with sessionmaker() as session, session.begin():
            await _purge_form(session, patient_form_id)
            if created_schema:
                await session.execute(
                    text("DELETE FROM schema_version WHERE id = :sid").bindparams(
                        sid=schema_version_id
                    )
                )
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=form_schema_id)
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_form_triggers_dispatch(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """Enqueue a form → dispatcher fires → form moves to IN_CALL, a Call is created."""
    # Enqueue: READY_FOR_PROCESSING → IN_QUEUE
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    # The status endpoint returns the form status BEFORE dispatch runs
    # (dispatch runs after flush). Check via the calls list.
    calls_resp = await client.get(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
    )
    assert calls_resp.status_code == 200, calls_resp.text
    # At least one call should exist for this tenant.
    calls = calls_resp.json()["data"]
    assert len(calls) >= 1


@pytest.mark.asyncio
async def test_completed_callback_moves_form_to_ai_processing(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
) -> None:
    """Worker reporting COMPLETED on an IN_CALL form succeeds (no 500): the call
    status becomes completed, the form transitions to AI_PROCESSING (not COMPLETED),
    a CallFormSnapshot row is written, and a PostCallJob is emitted."""
    # Enqueue → dispatcher creates the call (form is now IN_CALL).
    await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    call_id = (await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ][0]["id"]

    resp = await client.post(
        f"/api/v1/calls/{call_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 200, resp.text
    # The *call* status is completed; the *form* goes to ai_processing (not completed).
    assert resp.json()["data"]["status"] == "completed"

    async with admin_sessionmaker() as session:
        form_status = (
            await session.execute(
                text("SELECT status FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
        snapshot_exists = (
            await session.execute(
                text("SELECT 1 FROM call_form_snapshot WHERE call_id = :cid").bindparams(
                    cid=UUID(call_id)
                )
            )
        ).scalar_one_or_none()
    assert form_status == "ai_processing"
    assert snapshot_exists is not None


@pytest.mark.asyncio
async def test_enqueue_blocked_transition_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """Cannot transition from READY_FOR_PROCESSING → COMPLETED directly."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Manual (voice-lab) call vs queue machinery interplay.
# POST /calls must put the form IN_CALL: the callback needs a legal edge to a
# terminal status, the dispatcher must not treat the form as queue-eligible,
# and the form must count against the tenant's concurrency slots.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_call_then_completed_callback(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
) -> None:
    """A voice-lab call's terminal callback must succeed (not 500): POST /calls
    puts the form IN_CALL, giving COMPLETED a legal edge; the call status becomes
    completed and the form transitions to AI_PROCESSING (post-call eval pipeline)."""
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(queue_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    async with admin_sessionmaker() as session:
        status = (
            await session.execute(
                text("SELECT status FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert status == "in_call"  # not "in_queue" — the call is live

    resp = await client.post(
        f"/api/v1/calls/{call_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "completed"

    async with admin_sessionmaker() as session:
        form_status = (
            await session.execute(
                text("SELECT status FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert form_status == "ai_processing"


@pytest.mark.asyncio
async def test_enqueue_stamps_enqueued_by_id(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
) -> None:
    """Moving a form to IN_QUEUE stamps `enqueued_by_id` with the acting user —
    the queuer must be persisted on the form so the dispatcher can attribute
    ownership even when the call is created later by a different actor."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as session:
        enqueued_by_id = (
            await session.execute(
                text("SELECT enqueued_by_id FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert enqueued_by_id == rbac_world.admin_id


@pytest.mark.asyncio
async def test_dispatched_call_carries_queuer_as_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
) -> None:
    """A call started by the queue dispatcher is owned by the user who queued
    the form — `initiated_by_id` must not be left null for queue-started calls."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as session:
        initiated_by_id = (
            await session.execute(
                text("SELECT initiated_by_id FROM call WHERE form_id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert initiated_by_id == rbac_world.admin_id


@pytest.mark.asyncio
async def test_queue_started_call_is_publishable_by_queuer(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """The queuer owns the queue-started call and can publish it — a queue-
    started call must not be stuck ownerless (unpublishable by anyone)."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text

    calls = (await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ]
    owned = [c["id"] for c in calls if c["is_owner"] is True]
    assert owned, f"expected an owned queue-started call, got: {calls}"
    call_id = owned[0]

    # A non-owner (supervisor) cannot publish a queue-started call either.
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.supervisor_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    # The queuer (owner) can publish it.
    pub = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["published"] is True


@pytest.mark.asyncio
async def test_manual_call_form_not_redispatched(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
) -> None:
    """A form with a live manual call must not be picked up by the dispatcher
    when another enqueue fires it — exactly one call per form."""
    # Manual call on form X.
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(queue_form_id)},
    )
    assert created.status_code == 200, created.text

    # Second form Y in the same tenant (same schema chain as X).
    form_y_id = uuid7()
    async with admin_sessionmaker() as session, session.begin():
        schema_version_id = (
            await session.execute(
                text("SELECT schema_version_id FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
        session.add(
            PatientForm(
                id=form_y_id,
                tenant_id=rbac_world.tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Second Queue Patient",
            )
        )
    try:
        # Enqueue Y — fires the dispatcher, which must skip X (IN_CALL, not queued).
        resp = await client.put(
            f"/api/v1/patient-forms/{form_y_id}/status",
            headers=_auth(rbac_world.admin_token),
            json={"status": "in_queue"},
        )
        assert resp.status_code == 200, resp.text

        async with admin_sessionmaker() as session:
            x_calls = (
                await session.execute(
                    text("SELECT count(*) FROM call WHERE form_id = :fid").bindparams(
                        fid=queue_form_id
                    )
                )
            ).scalar_one()
        assert x_calls == 1  # no second (dispatcher-created) call for X
    finally:
        async with admin_sessionmaker() as session, session.begin():
            await _purge_form(session, form_y_id)


@pytest.mark.asyncio
async def test_completed_callback_emits_post_call_job(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
    fake_post_call_bus: FakePostCallBus,
) -> None:
    """COMPLETED worker callback on an IN_CALL form:
    - form status → ai_processing (not completed)
    - a CallFormSnapshot row is written for the call
    - exactly one PostCallJob is emitted via the bus
    """
    # Enqueue → dispatcher creates the call (form is now IN_CALL).
    await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    call_id = (await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ][0]["id"]

    resp = await client.post(
        f"/api/v1/calls/{call_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "completed"  # call is completed

    async with admin_sessionmaker() as session:
        form_status = (
            await session.execute(
                text("SELECT status FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
        snapshot_row = (
            await session.execute(
                text("SELECT before_state FROM call_form_snapshot WHERE call_id = :cid").bindparams(
                    cid=UUID(call_id)
                )
            )
        ).one_or_none()

    assert form_status == "ai_processing"
    assert snapshot_row is not None, "CallFormSnapshot must be written at call end"

    assert len(fake_post_call_bus.emitted) == 1
    job = fake_post_call_bus.emitted[0]
    assert str(job.call_id) == call_id
    assert job.form_id == queue_form_id
    assert job.tenant_id == rbac_world.tenant_id


# ---------------------------------------------------------------------------
# RETRY dispatch — retry_fields metadata + call_lineage
# ---------------------------------------------------------------------------

# A v2 schema with one required ask field (unfilled → drives retry_fields label).
_RETRY_SCHEMA: dict[str, object] = {
    "dsl_version": "2.1",
    "name": "Retry Dispatch Test Schema",
    "insurance_type": "infertility_treatment",
    "sections": {
        "benefits": {
            "title": "Benefits",
            "role": "collect",
            "fields": {
                "lifetime_max": {
                    "type": "text",
                    "title": "Lifetime Maximum",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is the lifetime maximum benefit?"},
                },
            },
        }
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["benefits"]}],
}


class _CaptureLiveKit:
    """Fake LiveKit that records the last metadata passed to create_call_room."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        self.calls.append((room_name, metadata))

    @property
    def last_metadata(self) -> dict[str, object] | None:
        return self.calls[-1][1] if self.calls else None


@dataclass
class _RetryFormCtx:
    tenant_id: UUID
    form_id: UUID
    prior_call_id: UUID
    sessionmaker: async_sessionmaker[AsyncSession]

    async def get_lineage(self) -> CallLineage:
        """Return the CallLineage row for the new (retry) call on this form."""
        async with self.sessionmaker() as session:
            # Find the new call (not the prior one) for the form.
            new_call_id = (
                await session.execute(
                    select(Call.id)
                    .where(Call.form_id == self.form_id, Call.id != self.prior_call_id)
                    .order_by(Call.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            return (
                await session.execute(
                    select(CallLineage).where(CallLineage.retry_call_id == new_call_id)
                )
            ).scalar_one()


@pytest.fixture
async def retry_form_ctx(database_url: str) -> AsyncGenerator[_RetryFormCtx]:
    """Seed a tenant + form with retry_count=1 (IN_QUEUE) and one prior completed
    call, against a v2 schema with one unfilled required ask field.

    Tears down in FK order on exit. Uses the superuser engine so RLS is bypassed
    during seeding/teardown; try_dispatch runs inside a tenant_session."""
    tenant_id = uuid7()
    form_id = uuid7()
    prior_call_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    created_schema = False
    schema_id: UUID

    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=str(tenant_id),
                name="Retry Dispatch Test Tenant",
                status="active",
            )
        )
        await session.flush()

        # Find-or-create: FormSchema has UNIQUE(insurance_type).
        existing = (
            await session.execute(
                select(FormSchema).where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            fs = FormSchema(
                id=uuid7(),
                insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                name="Retry Dispatch Test Schema",
            )
            session.add(fs)
            await session.flush()
            schema_id = fs.id
            created_schema = True
        else:
            schema_id = existing.id

        session.add(
            SchemaVersion(
                id=schema_version_id,
                schema_id=schema_id,
                version=997,
                schema_json=_RETRY_SCHEMA,
            )
        )
        await session.flush()

        session.add(
            PatientForm(
                id=form_id,
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Retry Patient",
                status=FormStatus.IN_QUEUE.value,
                retry_count=1,
            )
        )
        await session.flush()

        # Prior completed call — provides the parent for CallLineage.
        session.add(
            Call(
                id=prior_call_id,
                tenant_id=tenant_id,
                form_id=form_id,
                current_status=CallStatus.COMPLETED.value,
                mode="full",
            )
        )

    try:
        yield _RetryFormCtx(
            tenant_id=tenant_id,
            form_id=form_id,
            prior_call_id=prior_call_id,
            sessionmaker=sm,
        )
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM call_lineage WHERE tenant_id = :tid"
                ).bindparams(tid=tenant_id)
            )
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE tenant_id = :tid)"
                ).bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(
                    sid=schema_version_id
                )
            )
            if created_schema:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=schema_id)
                )
            await session.execute(
                text("DELETE FROM tenant WHERE id = :tid").bindparams(tid=tenant_id)
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_retry_dispatch_attaches_retry_fields_and_lineage(
    retry_form_ctx: _RetryFormCtx,
    database_url: str,
) -> None:
    """RETRY dispatch embeds retry_fields labels in room metadata and creates a
    CallLineage row linking the new call to the prior completed call."""
    from vera_core.services.queue_dispatcher import try_dispatch

    capture = _CaptureLiveKit()

    async with tenant_session(retry_form_ctx.sessionmaker, retry_form_ctx.tenant_id) as session:
        dispatched = await try_dispatch(session, retry_form_ctx.tenant_id, capture)

    assert dispatched == 1, "expected exactly one call dispatched"

    md = capture.last_metadata
    assert md is not None, "create_call_room must receive metadata"
    assert "retry_fields" in md, f"retry_fields missing from metadata: {md}"
    assert md["retry_fields"], "retry_fields must be non-empty"

    lineage = await retry_form_ctx.get_lineage()
    assert lineage.parent_call_id == retry_form_ctx.prior_call_id


@pytest.mark.asyncio
async def test_full_dispatch_does_not_carry_retry_fields(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
    fake_livekit: object,
) -> None:
    """A FULL dispatch (retry_count=0) must NOT include retry_fields in metadata.

    Uses the session-scoped FakeLiveKit that is wired into the app; records the
    dispatch_metadata index before/after enqueueing to isolate the metadata for
    this particular call."""
    from tests.integration.control_plane.conftest import FakeLiveKit

    lk: FakeLiveKit = fake_livekit  # type: ignore[assignment]
    calls_before = len(lk.dispatch_metadata)

    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text

    new_metadata = lk.dispatch_metadata[calls_before:]
    assert len(new_metadata) >= 1, "expected at least one dispatch"
    for md in new_metadata:
        assert md is None or "retry_fields" not in md, (
            f"FULL dispatch must not carry retry_fields; got: {md}"
        )
