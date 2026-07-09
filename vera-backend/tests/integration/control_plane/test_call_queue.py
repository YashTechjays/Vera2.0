"""Integration tests for the call queue & dispatch lifecycle.

Exercises the full flow: enqueue form → dispatcher fires → call created →
call terminal status reported → auto-retry → dispatcher fires again.
Runs against live RLS-enforcing Postgres with FakeLiveKit.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
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
from vera_core.db import uuid7
from vera_core.models import PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType

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
async def test_completed_callback_moves_form_to_completed(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    queue_form_id: UUID,
    trunk_configured: None,
) -> None:
    """Worker reporting COMPLETED on an IN_CALL form succeeds (no 500) and the
    form reaches COMPLETED — the IN_CALL → COMPLETED edge the worker drives."""
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
    assert resp.json()["data"]["status"] == "completed"

    async with admin_sessionmaker() as session:
        status = (
            await session.execute(
                text("SELECT status FROM patient_form WHERE id = :fid").bindparams(
                    fid=queue_form_id
                )
            )
        ).scalar_one()
    assert status == "completed"


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
    """A voice-lab call's terminal callback must succeed (not 500) and complete
    the form: POST /calls puts the form IN_CALL, giving COMPLETED a legal edge."""
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
    trunk_configured: None,
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
                insurance_provider_phone_number=_DIALABLE_PHONE,
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
