"""Integration tests for the call queue & dispatch lifecycle.

Exercises the full flow: enqueue form → dispatcher fires → call created →
call terminal status reported → auto-retry → dispatcher fires again.
Runs against live RLS-enforcing Postgres with FakeLiveKit.
"""

from collections.abc import AsyncGenerator
from datetime import time as dt_time
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Update, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.dispatch import drain_pending
from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from tests.integration.control_plane.test_patient_forms_intake import (
    INTAKE_PAYLOAD,
    _issue_key,
)
from tests.integration.control_plane.test_patient_forms_intake import (
    cleanup_forms as cleanup_forms,
)
from tests.integration.control_plane.test_patient_forms_intake import (
    ibv_schema as ibv_schema,
)
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.models import InsuranceProvider, PatientForm, Tenant
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import FormStatus, InsuranceType
from vera_core.services import queue_dispatcher
from vera_core.services.queue_dispatcher import try_dispatch

_DIALABLE_PHONE = "+15551234567"


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


async def _seed_ready_form(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    phone: str | None,
) -> AsyncGenerator[UUID]:
    """Seed a PatientForm in READY_FOR_PROCESSING for queue tests.

    `form_schema.insurance_type` is a globally UNIQUE catalog key and CI seeds the
    INFERTILITY_TREATMENT schema before pytest, so the schema is find-or-create;
    teardown only drops the schema chain this call actually created.
    """
    patient_form_id = uuid7()
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
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Queue Test Patient",
                insurance_provider_phone_number=phone,
            )
        )

    yield patient_form_id

    # An enqueue during the test schedules a detached dispatch task — let it
    # finish before purging, or its call insert races the deletes (FK errors,
    # leaked rows).
    await drain_pending()
    async with sessionmaker() as session, session.begin():
        await _purge_form(session, patient_form_id)
        if created_schema:
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            await session.execute(
                text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=form_schema_id)
            )


@pytest.fixture
async def queue_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """A queue-test form with a dialable payer phone (enqueue is gated on it)."""
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async for form_id in _seed_ready_form(
            sessionmaker, rbac_world.tenant_id, phone=_DIALABLE_PHONE
        ):
            yield form_id
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_form_without_phone(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """A queue-test form with no payer phone — must be rejected by the dialability gate."""
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async for form_id in _seed_ready_form(sessionmaker, rbac_world.tenant_id, phone=None):
            yield form_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_form_triggers_dispatch(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
    trunk_configured: None,
) -> None:
    """Enqueue a form → dispatcher fires → form moves to IN_CALL, a Call is created."""
    # Enqueue: READY_FOR_PROCESSING → IN_QUEUE
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    # Dispatch runs as a detached post-commit task — drain it before asserting
    # on its effects. Check via the calls list.
    await drain_pending()
    calls_resp = await client.get(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
    )
    assert calls_resp.status_code == 200, calls_resp.text
    # At least one call should exist for this tenant.
    calls = calls_resp.json()["data"]
    assert len(calls) >= 1


@pytest.mark.asyncio
async def test_enqueue_blocked_transition_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
    trunk_configured: None,
) -> None:
    """Cannot transition from READY_FOR_PROCESSING → COMPLETED directly."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_enqueue_stamps_enqueued_by_id(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
    trunk_configured: None,
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
    trunk_configured: None,
) -> None:
    """A call started by the queue dispatcher is owned by the user who queued
    the form — `initiated_by_id` must not be left null for queue-started calls."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    await drain_pending()  # dispatch is a detached post-commit task

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
    trunk_configured: None,
) -> None:
    """The queuer owns the queue-started call and can publish it — a queue-
    started call must not be stuck ownerless (unpublishable by anyone)."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    await drain_pending()  # dispatch is a detached post-commit task

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


# ---------------------------------------------------------------------------
# Queueability gate — a form that could never be dialed must not enter the queue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_rejected_without_payer_phone(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_without_phone: UUID,
    trunk_configured: None,
) -> None:
    resp = await client.put(
        f"/api/v1/patient-forms/{seeded_form_without_phone}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "phone" in body["message"].lower()


# ---------------------------------------------------------------------------
# `ivr_navigation_enabled` — the per-form voice-lab-style toggle (default True),
# persisted on the row so a later dispatch (freed slot / sweeper / retry) and any
# requeue keep the operator's choice.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_defaults_ivr_navigation_on(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """A freshly-intaken form defaults `ivr_navigation_enabled` to True (column
    default) — every real payer call navigates the IVR unless an operator opts out
    at enqueue time."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)
    resp = await client.post(
        "/api/v1/patient-forms",
        headers=_auth(token),
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
    )
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with admin_sessionmaker() as session:
        enabled = (
            await session.execute(
                text("SELECT ivr_navigation_enabled FROM patient_form WHERE id = :fid").bindparams(
                    fid=form_id
                )
            )
        ).scalar_one()
    assert enabled is True


@pytest.mark.asyncio
async def test_enqueue_can_disable_ivr_navigation(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
    trunk_configured: None,
) -> None:
    """Setting `enable_ivr_navigation: false` at enqueue persists the opt-out on
    the form — the test-phase escape hatch."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue", "enable_ivr_navigation": False},
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as session:
        enabled = (
            await session.execute(
                text("SELECT ivr_navigation_enabled FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert enabled is False


@pytest.mark.asyncio
async def test_requeue_without_toggle_keeps_stored_choice(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
    trunk_configured: None,
) -> None:
    """A form already opted out of IVR navigation keeps that choice on a requeue
    that omits the field — dispatch can run long after this request (freed slot,
    sweeper tick, auto-retry), so the choice must survive on the row."""
    off = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue", "enable_ivr_navigation": False},
    )
    assert off.status_code == 200, off.text

    # Drop the form back to CALL_FAILED (a legal manual → IN_QUEUE source) without
    # going through the worker-driven path — only the precondition matters here.
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE patient_form SET status = 'call_failed' WHERE id = :fid").bindparams(
                fid=queue_form_id
            )
        )

    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as session:
        enabled = (
            await session.execute(
                text("SELECT ivr_navigation_enabled FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert enabled is False


@pytest.mark.asyncio
async def test_detail_exposes_ivr_toggle(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """`GET /patient-forms/{id}` mirrors the row's `ivr_navigation_enabled` so the
    UI's requeue toggle can pre-load from it."""
    resp = await client.get(
        f"/api/v1/patient-forms/{queue_form_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["ivr_navigation_enabled"] is True


# ---------------------------------------------------------------------------
# Head-of-line blocking: the working-hours gate lives in try_dispatch's raw SQL
# (the `provider_outside_hours` EXISTS subquery), where a quiet regression —
# status string, name-join, NULL semantics — would silently strand every form
# queued behind a closed provider. These tests run that SQL on real Postgres.
# ---------------------------------------------------------------------------

_HOL_CLOSED_PROVIDER = "HOL Test Closed Payer"
_HOL_OPEN_PROVIDER = "HOL Test Open Payer"
_HOL_CLOSED_PHONE = "+15550001111"
_HOL_OPEN_PHONE = "+15550002222"


def _requeue(form_id: UUID, *, provider: str | None, minutes_ago: int) -> Update:
    """Back-date a form into IN_QUEUE for a given provider (None exercises the NULL
    leg of the EXISTS gate). `minutes_ago` sets FIFO order via enqueued_at."""
    return (
        update(PatientForm)
        .where(PatientForm.id == form_id)
        .values(
            status=FormStatus.IN_QUEUE.value,
            enqueued_at=text(f"now() - interval '{minutes_ago} minutes'"),
            insurance_provider=provider,
        )
    )


@pytest.fixture
async def hol_providers(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Two ACTIVE global providers: one whose window excludes the patched noon
    clock (closed) and one whose window includes it (open). Delete-first setup
    so residue from an interrupted run can't trip the unique lower(name) index."""

    async def _purge(session: AsyncSession) -> None:
        await session.execute(
            delete(InsuranceProvider).where(
                InsuranceProvider.name.in_([_HOL_CLOSED_PROVIDER, _HOL_OPEN_PROVIDER])
            )
        )

    async with admin_sessionmaker() as session, session.begin():
        await _purge(session)
        session.add_all(
            [
                InsuranceProvider(
                    name=_HOL_CLOSED_PROVIDER,
                    working_hour_start=dt_time(8, 0),
                    working_hour_end=dt_time(9, 0),
                ),
                InsuranceProvider(
                    name=_HOL_OPEN_PROVIDER,
                    working_hour_start=dt_time(8, 0),
                    working_hour_end=dt_time(18, 0),
                ),
            ]
        )
    yield
    async with admin_sessionmaker() as session, session.begin():
        await _purge(session)


@pytest.fixture
async def hol_form_ids(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[tuple[UUID, UUID]]:
    """(closed_form_id, open_form_id) — two queue-test forms with distinct payer
    phones so the dial assertions can tell which one actually went out."""
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async for closed_id in _seed_ready_form(
            sessionmaker, rbac_world.tenant_id, phone=_HOL_CLOSED_PHONE
        ):
            async for open_id in _seed_ready_form(
                sessionmaker, rbac_world.tenant_id, phone=_HOL_OPEN_PHONE
            ):
                yield closed_id, open_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_provider_head_does_not_block_dispatchable_form_behind_it(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    trunk_configured: None,
    hol_providers: None,
    hol_form_ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale head-of-queue form for a closed provider must not consume the FIFO
    window: with exactly one concurrency slot, the SQL gate has to skip it so the
    younger open-provider form behind it dials. Before the gate was pushed into
    the WHERE clause, the closed form won the LIMIT(slots) fetch, the dial-time
    re-check dropped it, and everything behind it starved until expiry."""
    closed_form_id, open_form_id = hol_form_ids
    # Freeze the dispatcher's clock at noon Eastern: closed 08:00-09:00, open 08:00-18:00.
    monkeypatch.setattr(queue_dispatcher, "_now_eastern_time", lambda: dt_time(12, 0))

    async with admin_sessionmaker() as session, session.begin():
        # Closed-provider form at the head of the FIFO (older enqueued_at), open
        # form behind it.
        await session.execute(
            _requeue(closed_form_id, provider=_HOL_CLOSED_PROVIDER, minutes_ago=10)
        )
        await session.execute(_requeue(open_form_id, provider=_HOL_OPEN_PROVIDER, minutes_ago=5))
        # One slot: if the closed form wins the fetch, nothing dials this pass.
        old_max = (
            await session.execute(
                select(Tenant.max_concurrent_calls).where(Tenant.id == rbac_world.tenant_id)
            )
        ).scalar_one()
        await session.execute(
            update(Tenant).where(Tenant.id == rbac_world.tenant_id).values(max_concurrent_calls=1)
        )

    fake = FakeLiveKit()
    try:
        async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
            dispatched = await try_dispatch(
                session,
                rbac_world.tenant_id,
                fake,
                LocalDevKMS(master_key=b"a" * 32),
                dial_pacing_s=0,
            )
        assert dispatched == 1
        assert [phone for _room, phone, _trunk in fake.sip_calls] == [_HOL_OPEN_PHONE]

        async with admin_sessionmaker() as session:
            rows = (
                await session.execute(
                    select(PatientForm.id, PatientForm.status).where(
                        PatientForm.id.in_([closed_form_id, open_form_id])
                    )
                )
            ).tuples()
        statuses = dict(rows.all())
        assert statuses[open_form_id] == FormStatus.IN_CALL.value
        # The closed-provider form is skipped, not consumed: still queued for
        # a later pass inside its provider's window.
        assert statuses[closed_form_id] == FormStatus.IN_QUEUE.value
    finally:
        async with admin_sessionmaker() as session, session.begin():
            await session.execute(
                update(Tenant)
                .where(Tenant.id == rbac_world.tenant_id)
                .values(max_concurrent_calls=old_max)
            )


@pytest.mark.asyncio
async def test_open_provider_form_dispatches_through_the_sql_hours_gate(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    trunk_configured: None,
    hol_providers: None,
    hol_form_ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complement of the HOL test: inside the window (and for a form with no
    provider row at all) the EXISTS gate must not filter — both forms dial."""
    closed_form_id, open_form_id = hol_form_ids
    # 08:30 Eastern is inside BOTH windows.
    monkeypatch.setattr(queue_dispatcher, "_now_eastern_time", lambda: dt_time(8, 30))

    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            _requeue(closed_form_id, provider=_HOL_CLOSED_PROVIDER, minutes_ago=10)
        )
        # NULL-semantics leg: no provider name — the correlated EXISTS matches no
        # row, so the form must remain dispatchable.
        await session.execute(_requeue(open_form_id, provider=None, minutes_ago=5))

    fake = FakeLiveKit()
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        dispatched = await try_dispatch(
            session,
            rbac_world.tenant_id,
            fake,
            LocalDevKMS(master_key=b"a" * 32),
            dial_pacing_s=0,
        )
    assert dispatched == 2
    assert sorted(phone for _room, phone, _trunk in fake.sip_calls) == [
        _HOL_CLOSED_PHONE,
        _HOL_OPEN_PHONE,
    ]
