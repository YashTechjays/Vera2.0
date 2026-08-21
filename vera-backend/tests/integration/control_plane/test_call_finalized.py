"""Task 4: distinguish an unfinalized attempt from one that finalized with no diff.

`changed_paths == []` is returned by `snapshot_changed_paths` for two different
situations — the post-call eval never ran (`after_state == {}`) or it ran and found
nothing changed — and the two must stay distinguishable through
`GET /patient-forms/{id}/calls`, not just internally in `vera_core`."""

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import CallStatus, FormStatus, InsuranceType
from vera_core.models.field_answer import CallFormSnapshot
from vera_core.models.patient_form import PatientForm

pytestmark = pytest.mark.integration


async def _get_calls(client: httpx.AsyncClient, token: str, form_id: UUID) -> list[dict[str, Any]]:
    resp = await client.get(
        f"/api/v1/patient-forms/{form_id}/calls",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data: list[dict[str, Any]] = resp.json()["data"]
    return data


@pytest.fixture
async def unfinalized_vs_no_diff_form(
    database_url: str, rbac_world: RBACWorld
) -> AsyncGenerator[tuple[UUID, UUID, UUID]]:
    """One form, two completed calls, both with `changed_paths == []`:
    `unfinalized` has no `CallFormSnapshot` row at all (the post-call eval never ran);
    `no_diff` has one with `before_state == after_state` (the eval ran, nothing changed).

    Yields `(form_id, unfinalized_call_id, no_diff_call_id)`.
    """
    tenant_id = rbac_world.tenant_id
    form_id = uuid7()
    unfinalized_id = uuid7()
    no_diff_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    schema_id_to_delete: UUID | None = None

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
                name="Call Finalized Test Schema",
            )
            session.add(fs)
            await session.flush()
            schema_id = fs.id
            schema_id_to_delete = fs.id
        else:
            schema_id = existing.id
        session.add(
            SchemaVersion(id=schema_version_id, schema_id=schema_id, version=995, schema_json={})
        )
        session.add(
            PatientForm(
                id=form_id,
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                status=FormStatus.EXCEPTION_REVIEW.value,
                patient_name="Finalized Test Patient",
            )
        )
        await session.flush()
        session.add(
            Call(
                id=unfinalized_id,
                tenant_id=tenant_id,
                form_id=form_id,
                mode="full",
                current_status=CallStatus.COMPLETED.value,
                initiated_by_id=rbac_world.supervisor_id,
            )
        )
        session.add(
            Call(
                id=no_diff_id,
                tenant_id=tenant_id,
                form_id=form_id,
                mode="retry",
                current_status=CallStatus.COMPLETED.value,
                initiated_by_id=rbac_world.supervisor_id,
            )
        )
        await session.flush()
        # unfinalized_id gets NO CallFormSnapshot row — the observer never ran.
        session.add(
            CallFormSnapshot(
                tenant_id=tenant_id,
                call_id=no_diff_id,
                before_state={"cov.a": "x"},
                after_state={"cov.a": "x"},
            )
        )
    try:
        yield form_id, unfinalized_id, no_diff_id
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                text("DELETE FROM call_form_snapshot WHERE call_id = ANY(:ids)").bindparams(
                    ids=[unfinalized_id, no_diff_id]
                )
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


@pytest.mark.asyncio
async def test_api_distinguishes_unfinalized_from_no_diff(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    unfinalized_vs_no_diff_form: tuple[UUID, UUID, UUID],
) -> None:
    form_id, unfinalized_id, no_diff_id = unfinalized_vs_no_diff_form

    calls = await _get_calls(client, rbac_world.admin_token, form_id)
    by_id = {c["id"]: c for c in calls}

    # Both attempts report the same empty changed_paths...
    assert by_id[str(unfinalized_id)]["changed_paths"] == []
    assert by_id[str(no_diff_id)]["changed_paths"] == []
    # ...but `finalized` tells a reviewer whether that's a real answer or an unknown one.
    assert by_id[str(unfinalized_id)]["finalized"] is False
    assert by_id[str(no_diff_id)]["finalized"] is True
