"""GET /patient-forms/{id}/calls `recording` enrichment: advertised only when an
AVAILABLE recording exists AND the call's content is visible to the caller (a
finished call is tenant-visible per VR2-177; a live one is owner-or-published —
the playback endpoint's exact gate), so the UI never renders a play button that
would 404."""

from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import CallStatus, FormStatus, InsuranceType, RecordingStatus
from vera_core.models.patient_form import PatientForm
from vera_core.models.transcript import Recording

pytestmark = pytest.mark.integration


@pytest.fixture
async def recording_form_id(database_url: str, rbac_world: RBACWorld) -> AsyncGenerator[UUID]:
    """Three completed calls on one form, owned by the supervisor:
    - call1: unpublished + AVAILABLE recording  → owner-only playback
    - call2: published + AVAILABLE recording    → tenant-visible playback
    - call3: unpublished + PENDING recording    → playable by nobody
    """
    tenant_id = rbac_world.tenant_id
    form_id = uuid7()
    call_ids = [uuid7(), uuid7(), uuid7()]
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    schema_id_to_delete: UUID | None = None
    try:
        async with sm() as session, session.begin():
            existing = (
                await session.execute(
                    select(FormSchema).where(
                        FormSchema.insurance_type == InsuranceType.DISEASE_ONLY.value
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                fs = FormSchema(
                    id=uuid7(),
                    insurance_type=InsuranceType.DISEASE_ONLY.value,
                    name="Recording Test Schema",
                )
                session.add(fs)
                await session.flush()
                schema_id = fs.id
                schema_id_to_delete = fs.id
            else:
                schema_id = existing.id
            session.add(
                SchemaVersion(
                    id=schema_version_id,
                    schema_id=schema_id,
                    version=994,
                    schema_json={},
                )
            )
            session.add(
                PatientForm(
                    id=form_id,
                    tenant_id=tenant_id,
                    schema_version_id=schema_version_id,
                    status=FormStatus.COMPLETED.value,
                )
            )
            await session.flush()
            published_flags = [False, True, False]
            for call_id, published in zip(call_ids, published_flags, strict=True):
                session.add(
                    Call(
                        id=call_id,
                        tenant_id=tenant_id,
                        form_id=form_id,
                        mode="full",
                        current_status=CallStatus.COMPLETED.value,
                        initiated_by_id=rbac_world.supervisor_id,
                        published=published,
                    )
                )
            await session.flush()
            statuses = [
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.PENDING.value,
            ]
            for call_id, status in zip(call_ids, statuses, strict=True):
                session.add(
                    Recording(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        call_id=call_id,
                        gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call_id}.ogg",
                        status=status,
                    )
                )
        yield form_id
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                text("DELETE FROM recording WHERE call_id = ANY(:ids)").bindparams(ids=call_ids)
            )
            await session.execute(text("DELETE FROM call WHERE form_id = :f").bindparams(f=form_id))
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :f").bindparams(f=form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sv").bindparams(sv=schema_version_id)
            )
            if schema_id_to_delete is not None:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fs").bindparams(
                        fs=schema_id_to_delete
                    )
                )
        await engine.dispose()


async def _recordings_by_attempt(
    client: httpx.AsyncClient, form_id: UUID, token: str
) -> list[bool]:
    resp = await client.get(
        f"/api/v1/patient-forms/{form_id}/calls",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return [c["recording_available"] for c in resp.json()["data"]]


@pytest.mark.asyncio
async def test_owner_sees_available_on_owned_and_published_calls(
    client: httpx.AsyncClient, rbac_world: RBACWorld, recording_form_id: UUID
) -> None:
    assert await _recordings_by_attempt(client, recording_form_id, rbac_world.supervisor_token) == [
        True,
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_non_owner_sees_available_on_all_finished_calls(
    client: httpx.AsyncClient, rbac_world: RBACWorld, recording_form_id: UUID
) -> None:
    # All three calls are terminal, so their content is tenant-visible (VR2-177);
    # only the PENDING recording stays hidden.
    assert await _recordings_by_attempt(client, recording_form_id, rbac_world.admin_token) == [
        True,
        True,
        False,
    ]
