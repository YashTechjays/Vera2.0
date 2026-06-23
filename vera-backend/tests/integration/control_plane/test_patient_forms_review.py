"""Display + dispute-resolution endpoints (session-auth, real RLS):
`GET /api/v1/patient-forms`, `GET /api/v1/patient-forms/{id}`, and
`POST /api/v1/patient-forms/{id}/disputes:resolve`. Skips without Postgres."""

from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.models import (
    DisputeAction,
    FieldAnswer,
    FieldEvaluation,
    FormSchema,
    PatientForm,
    SchemaVersion,
)
from vera_core.models.enums import AnswerSource, FormStatus, InsuranceType, VersionStatus

HEALTH_PLAN = "insurance_information.health_plan"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def schema_version_id(admin_sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
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
async def cleanup_forms(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncGenerator[None]:
    yield
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("DELETE FROM patient_form WHERE tenant_id IN (:a, :b)").bindparams(
                a=rbac_world.tenant_id, b=rbac_world.other_tenant_id
            )
        )


async def _make_form_with_dispute(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
    status: FormStatus = FormStatus.EXCEPTION_REVIEW,
) -> UUID:
    """Create a form with one undisputed INTAKE field, plus a disputed field: a
    superseded INTAKE answer (prior) + a current AI_CALL answer carrying a
    field_evaluation(supported=false). Returns the form id."""
    async with sm() as s, s.begin():
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            status=status.value,
            intake_payload={"patient_information": {"patient_name": "Jane Doe"}},
            patient_name="jane doe",
            completion_pct=0,
            retry_count=0,
        )
        s.add(form)
        await s.flush()
        # Undisputed current field.
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path="patient_information.patient_name",
                value={"value": "Jane Doe"},
                source=AnswerSource.INTAKE.value,
                is_current=True,
            )
        )
        # Superseded prior (intake) for the disputed field.
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path=HEALTH_PLAN,
                value={"value": "BCBS TX"},
                source=AnswerSource.INTAKE.value,
                is_current=False,
            )
        )
        # Current AI capture for the disputed field.
        ai = FieldAnswer(
            tenant_id=tenant_id,
            form_id=form.id,
            field_path=HEALTH_PLAN,
            value={"value": "Blue Cross"},
            source=AnswerSource.AI_CALL.value,
            confidence=95,
            evidence="rep said so",
            is_current=True,
        )
        s.add(ai)
        await s.flush()
        s.add(
            FieldEvaluation(
                tenant_id=tenant_id,
                answer_id=ai.id,
                supported=False,
                confidence=72,
                evidence="judge: disagrees",
            )
        )
        return form.id


@pytest.fixture
async def dispute_form(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> UUID:
    return await _make_form_with_dispute(
        admin_sessionmaker, tenant_id=rbac_world.tenant_id, schema_version_id=schema_version_id
    )


# ---- list -------------------------------------------------------------------


async def test_list_returns_paginated_summaries_with_dispute_count(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    resp = await client.get("/api/v1/patient-forms", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert {"items", "page", "page_size", "total"} <= body.keys()
    row = next(r for r in body["items"] if r["id"] == str(dispute_form))
    assert row["status"] == "exception_review"
    assert row["patient_name"] == "jane doe"
    assert row["dispute_count"] == 1


async def test_list_requires_forms_read(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get("/api/v1/patient-forms", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403, resp.text


# ---- detail -----------------------------------------------------------------


async def test_detail_returns_fields_and_dispute(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    resp = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    data = resp.json()["data"]
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    fields = {f["field_path"]: f for f in data["fields"]}
    assert fields["patient_information.patient_name"]["dispute"] is None
    d = fields[HEALTH_PLAN]["dispute"]
    assert d == {
        "previous_value": "BCBS TX",
        "current_value": "Blue Cross",
        "confidence": 72,
        "evidence": "rep said so",
        "reasoning": "judge: disagrees",
    }
    # PHI-access audit written (field names only, no values).
    async with admin_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT event_type, detail::text FROM audit_log "
                    "WHERE resource_id = :rid AND event_type = 'phi.access'"
                ).bindparams(rid=str(dispute_form))
            )
        ).all()
    assert rows, "expected a phi.access audit row"
    assert "Blue Cross" not in rows[0][1]  # never the value


async def test_detail_cross_tenant_is_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    other = await _make_form_with_dispute(
        admin_sessionmaker,
        tenant_id=rbac_world.other_tenant_id,
        schema_version_id=schema_version_id,
    )
    resp = await client.get(f"/api/v1/patient-forms/{other}", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 404, resp.text


# ---- resolve ----------------------------------------------------------------


async def test_resolve_correct_emits_human_answer(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: "Aetna"}, "dispute_fields": [], "reasked_fields": []},
    )
    assert resp.status_code == 200, resp.text
    async with admin_sessionmaker() as s:
        current = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == dispute_form,
                    FieldAnswer.field_path == HEALTH_PLAN,
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert current.source == AnswerSource.HUMAN.value
        assert current.value == {"value": "Aetna"}
        actions = (
            (
                await s.execute(
                    select(DisputeAction).where(DisputeAction.tenant_id == rbac_world.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert any(a.action == "correct" for a in actions)


async def test_resolve_accept_records_action_and_clears_dispute(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: "Blue Cross"}, "dispute_fields": [HEALTH_PLAN]},
    )
    assert resp.status_code == 200, resp.text
    # Dispute is now resolved (adjudicated), so detail no longer flags it.
    detail = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    fields = {f["field_path"]: f for f in detail.json()["data"]["fields"]}
    assert fields[HEALTH_PLAN]["dispute"] is None
    assert detail.json()["data"]["status"] == "completed"  # all disputes resolved


async def test_resolve_reask_requeues_form(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {}, "reasked_fields": [HEALTH_PLAN]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "in_queue"
    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == dispute_form))
        ).scalar_one()
        assert form.retry_count == 1


async def test_resolve_requires_forms_write(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.norole_token),
        json={"form_data": {}},
    )
    assert resp.status_code == 403, resp.text
