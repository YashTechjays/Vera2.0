"""Integration tests for /api/v1/analytics — counts only, real RLS + RBAC."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models import PatientForm, Tenant
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType

QUEUE_STATUS_PATH = "/api/v1/analytics/queue-status"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_forms(sessionmaker: async_sessionmaker[AsyncSession], *form_ids: UUID) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(PatientForm).where(PatientForm.id.in_(form_ids)))


async def _seed_form(session: AsyncSession, tenant_id: UUID) -> UUID:
    """A minimal PatientForm satisfying `schema_version_id` NOT NULL, for queue-status
    counts. Mirrors `test_call_queue._seed_ready_form`'s schema find-or-create (the
    INFERTILITY_TREATMENT catalog schema is shared/seeded, so this rarely creates it)."""
    schema = (
        await session.execute(
            select(FormSchema).where(
                FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
            )
        )
    ).scalar_one_or_none()
    if schema is None:
        schema = FormSchema(
            id=uuid7(),
            insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
            name="Analytics Test Schema",
        )
        session.add(schema)
        await session.flush()
        schema_version_id = uuid7()
        session.add(
            SchemaVersion(id=schema_version_id, schema_id=schema.id, version=1, schema_json={})
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
    form_id = uuid7()
    session.add(
        PatientForm(
            id=form_id,
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            patient_name="Analytics Test Patient",
        )
    )
    await session.flush()
    return form_id


@pytest.mark.asyncio
async def test_queue_status_mirrors_dispatcher_math(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Seed one form per dispatcher-relevant status; the card must count forms in
    the dispatcher's active set as 'active', in_queue forms as 'in_queue', and
    ready_for_processing in neither — exact before/after deltas prove all three."""
    headers = _auth(rbac_world.virtual_assistant_token)
    before = (await client.get(QUEUE_STATUS_PATH, headers=headers)).json()["data"]

    form_ids: list[UUID] = []
    async with admin_sessionmaker() as session, session.begin():
        for status in ("in_queue", "in_call", "ai_processing", "ready_for_processing"):
            form_id = await _seed_form(session, rbac_world.tenant_id)
            form_ids.append(form_id)
            await session.execute(
                update(PatientForm).where(PatientForm.id == form_id).values(status=status)
            )
        limit = (
            await session.execute(
                select(Tenant.max_concurrent_calls).where(Tenant.id == rbac_world.tenant_id)
            )
        ).scalar_one()

    try:
        resp = await client.get(QUEUE_STATUS_PATH, headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        data = resp.json()["data"]
        assert data["limit"] == limit
        assert data["active"] == before["active"] + 2  # in_call + ai_processing only
        assert data["in_queue"] == before["in_queue"] + 1  # ready_for_processing counts nowhere
    finally:
        await _delete_forms(admin_sessionmaker, *form_ids)


@pytest.mark.asyncio
async def test_queue_status_denied_without_calls_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_queue_status_is_tenant_isolated(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A queued form in ANOTHER tenant never shows in this tenant's counts."""
    before = (await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ]
    async with admin_sessionmaker() as session, session.begin():
        other_form = await _seed_form(session, rbac_world.other_tenant_id)
        await session.execute(
            update(PatientForm).where(PatientForm.id == other_form).values(status="in_queue")
        )
    try:
        after = (await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))).json()[
            "data"
        ]
        assert after == before
    finally:
        await _delete_forms(admin_sessionmaker, other_form)
