"""GET /call-history — the tenant-wide, flat call-history list (cross-form counterpart
to GET /patient-forms/{id}/calls). Covers the calls:read gate, patient search, status
filter, newest-first ordering, and the caller-aware `recording_available` gate
(AVAILABLE recording AND call visible AND caller holds recordings:read)."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
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

# A patient name unique to this module so `q` isolates our rows from any other
# calls the shared test tenant accumulated in earlier suites.
_PATIENT = "ZzCallHist Patient"
_MEMBER = "ZZ-CH-0001"


@dataclass
class _Seeded:
    form_id: UUID
    # call ids in insertion order: [owned+available, published+available, pending]
    call_ids: list[UUID]


@pytest.fixture
async def seeded(database_url: str, rbac_world: RBACWorld) -> AsyncGenerator[_Seeded]:
    """One form (unique patient name) with three supervisor-owned calls:
    - call1: completed, unpublished, AVAILABLE recording → owner-only playback
    - call2: canceled, published, AVAILABLE recording   → tenant-visible playback
    - call3: busy, unpublished, PENDING recording        → playable by nobody
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
                    name="Call History Test Schema",
                )
                session.add(fs)
                await session.flush()
                schema_id = fs.id
                schema_id_to_delete = fs.id
            else:
                schema_id = existing.id
            session.add(
                SchemaVersion(
                    id=schema_version_id, schema_id=schema_id, version=993, schema_json={}
                )
            )
            session.add(
                PatientForm(
                    id=form_id,
                    tenant_id=tenant_id,
                    schema_version_id=schema_version_id,
                    status=FormStatus.EXCEPTION_REVIEW.value,
                    patient_name=_PATIENT,
                    member_id=_MEMBER,
                    insurance_provider="UHC",
                )
            )
            await session.flush()
            rows = [
                (CallStatus.COMPLETED.value, "full", False),
                (CallStatus.CANCELED.value, "full", True),
                (CallStatus.BUSY.value, "retry", False),
            ]
            for call_id, (call_status, mode, published) in zip(call_ids, rows, strict=True):
                session.add(
                    Call(
                        id=call_id,
                        tenant_id=tenant_id,
                        form_id=form_id,
                        mode=mode,
                        current_status=call_status,
                        initiated_by_id=rbac_world.supervisor_id,
                        published=published,
                    )
                )
            await session.flush()
            rec_statuses = [
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.PENDING.value,
            ]
            for call_id, rec_status in zip(call_ids, rec_statuses, strict=True):
                session.add(
                    Recording(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        call_id=call_id,
                        gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call_id}.ogg",
                        status=rec_status,
                    )
                )
        yield _Seeded(form_id=form_id, call_ids=call_ids)
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


async def _list(client: httpx.AsyncClient, token: str, **params: str) -> httpx.Response:
    return await client.get(
        "/api/v1/call-history",
        params={"q": _PATIENT, **params},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_requires_calls_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    resp = await _list(client, rbac_world.norole_token)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_lists_matching_calls_newest_first(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    resp = await _list(client, rbac_world.supervisor_token)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    items = data["items"]
    assert {i["id"] for i in items} == {str(c) for c in seeded.call_ids}
    # newest-first: created_at is non-increasing down the page.
    times = [i["created_at"] for i in items]
    assert times == sorted(times, reverse=True)
    # patient identifiers are carried for display.
    assert all(i["patient_name"] == _PATIENT and i["member_id"] == _MEMBER for i in items)


@pytest.mark.asyncio
async def test_status_filter(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    resp = await _list(client, rbac_world.supervisor_token, status=CallStatus.CANCELED.value)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert [i["id"] for i in items] == [str(seeded.call_ids[1])]


@pytest.mark.asyncio
async def test_search_miss_returns_empty(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    resp = await client.get(
        "/api/v1/call-history",
        params={"q": "no-such-patient-zzzz"},
        headers={"Authorization": f"Bearer {rbac_world.supervisor_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"items": [], "page": 1, "page_size": 20, "total": 0}


@pytest.mark.asyncio
async def test_recording_gate_owner_vs_non_owner(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    owner = (await _list(client, rbac_world.supervisor_token)).json()["data"]["items"]
    by_id = {i["id"]: i["recording_available"] for i in owner}
    # owner: AVAILABLE on owned (call1) and published (call2); never on PENDING (call3).
    assert by_id[str(seeded.call_ids[0])] is True
    assert by_id[str(seeded.call_ids[1])] is True
    assert by_id[str(seeded.call_ids[2])] is False
    # non-owner: every call here is terminal, so its content is tenant-visible
    # (VR2-177) — AVAILABLE recordings show regardless of owner/published.
    admin = (await _list(client, rbac_world.admin_token)).json()["data"]["items"]
    admin_by_id = {i["id"]: i["recording_available"] for i in admin}
    assert admin_by_id[str(seeded.call_ids[0])] is True
    assert admin_by_id[str(seeded.call_ids[1])] is True
    assert admin_by_id[str(seeded.call_ids[2])] is False


@pytest.mark.asyncio
async def test_recording_hidden_without_recordings_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    # listener holds calls:read only — sees the rows but no playable recordings.
    items = (await _list(client, rbac_world.listener_token)).json()["data"]["items"]
    assert len(items) == 3
    assert all(i["recording_available"] is False for i in items)


@pytest.mark.asyncio
async def test_date_to_in_the_past_excludes_recent_calls(
    client: httpx.AsyncClient, rbac_world: RBACWorld, seeded: _Seeded
) -> None:
    resp = await _list(client, rbac_world.supervisor_token, date_to="2000-01-01T00:00:00Z")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 0
