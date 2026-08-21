"""Task 2: flag answers a non-authoritative call produced.

A call that never captured the rep's call reference number proved nothing — its
answers carry no proof and are always re-asked (spec D8). `GET /patient-forms/{id}/calls`
and `GET /patient-forms/{id}` must both surface that as `authoritative: false`, without
demoting or hiding the answer itself (Task 2 brief: FLAG, nothing else).
"""

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import (
    AnswerSource,
    CallStatus,
    FormStatus,
    InsuranceType,
    VersionStatus,
)
from vera_core.models.field_answer import FieldAnswer
from vera_core.models.patient_form import PatientForm

pytestmark = pytest.mark.integration

# The real IBV schema's rep_call_reference_number_field
# (data/form_schemas/ibv_form_standard_v2.json).
REF = "sections.insurance_representative.call_reference_number"
DEDUCTIBLE = "sections.deductibles.individual.total"
COPAY = "sections.infertility_treatment.ovulation_induction.copay"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ibv_schema_version_id(admin_sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
    """The published IBV schema version — the one with a real `rep_call_reference_number_field`,
    unlike the `schema_json={}` fixtures other provenance tests use."""
    async with admin_sessionmaker() as s, s.begin():
        await _seed_form_schemas(s)
    async with admin_sessionmaker() as s:
        return (
            await s.execute(
                select(SchemaVersion.id)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).scalar_one()


@pytest.fixture
async def two_call_form(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    ibv_schema_version_id: UUID,
) -> AsyncGenerator[tuple[UUID, UUID, UUID]]:
    """One form, two completed calls: `good` captured the rep's call reference number
    (authoritative), `bad` did not. `good` also wrote DEDUCTIBLE; `bad` wrote COPAY.

    Yields `(form_id, good_call_id, bad_call_id)`. Teardown in FK order; the tenant is
    owned by `rbac_world` and is not deleted here.
    """
    tenant_id = rbac_world.tenant_id
    form_id = uuid7()
    good_id = uuid7()
    bad_id = uuid7()

    async with admin_sessionmaker() as s, s.begin():
        s.add(
            PatientForm(
                id=form_id,
                tenant_id=tenant_id,
                schema_version_id=ibv_schema_version_id,
                patient_name="Authoritative Test Patient",
                status=FormStatus.AI_PROCESSING.value,
            )
        )
        await s.flush()
        s.add(
            Call(
                id=good_id,
                tenant_id=tenant_id,
                form_id=form_id,
                current_status=CallStatus.COMPLETED.value,
                mode="full",
            )
        )
        s.add(
            Call(
                id=bad_id,
                tenant_id=tenant_id,
                form_id=form_id,
                current_status=CallStatus.COMPLETED.value,
                mode="retry",
            )
        )
        await s.flush()
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form_id,
                field_path=REF,
                value={"value": "9310-KT-04"},
                source=AnswerSource.AI_CALL.value,
                call_id=good_id,
                is_current=True,
            )
        )
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form_id,
                field_path=DEDUCTIBLE,
                value={"value": "$3,000"},
                source=AnswerSource.AI_CALL.value,
                call_id=good_id,
                is_current=True,
            )
        )
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form_id,
                field_path=COPAY,
                value={"value": "$25"},
                source=AnswerSource.AI_CALL.value,
                call_id=bad_id,
                is_current=True,
            )
        )

    yield form_id, good_id, bad_id

    async with admin_sessionmaker() as s, s.begin():
        await s.execute(delete(FieldAnswer).where(FieldAnswer.form_id == form_id))
        await s.execute(delete(Call).where(Call.form_id == form_id))
        await s.execute(delete(PatientForm).where(PatientForm.id == form_id))


async def _get_calls(client: httpx.AsyncClient, token: str, form_id: UUID) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/v1/patient-forms/{form_id}/calls", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data: list[dict[str, Any]] = resp.json()["data"]
    return data


async def _get_detail(client: httpx.AsyncClient, token: str, form_id: UUID) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/patient-forms/{form_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


async def test_an_attempt_with_no_reference_number_is_flagged_unauthoritative(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    two_call_form: tuple[UUID, UUID, UUID],
) -> None:
    """A call that captured no reference number proved nothing: its answers are always
    re-asked (spec D8) and its verified contribution is zero (Plan D). The reviewer has
    to be able to SEE that, or the value looks as solid as any other."""
    form_id, good_id, bad_id = two_call_form

    calls = await _get_calls(client, rbac_world.admin_token, form_id)
    by_id = {c["id"]: c for c in calls}
    assert by_id[str(good_id)]["authoritative"] is True
    assert by_id[str(bad_id)]["authoritative"] is False

    detail = await _get_detail(client, rbac_world.admin_token, form_id)
    prov = {f["field_path"]: f["provenance"] for f in detail["fields"] if f["provenance"]}
    assert prov[DEDUCTIBLE]["authoritative"] is True
    assert prov[COPAY]["authoritative"] is False

    # Non-authoritative answers stay `is_current` — flagged, never demoted or hidden.
    fields_by_path = {f["field_path"]: f for f in detail["fields"]}
    assert fields_by_path[COPAY]["value"] is not None
