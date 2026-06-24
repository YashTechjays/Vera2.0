"""Display + dispute-resolution endpoints (session-auth, real RLS):
`GET /api/v1/patient-forms`, `GET /api/v1/patient-forms/{id}`, and
`POST /api/v1/patient-forms/{id}/disputes:resolve`. Skips without Postgres."""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

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
    # Resolving disputes never changes status — completion is a separate manual
    # step via PUT .../status (which now passes the no-open-disputes gate).
    assert detail.json()["data"]["status"] == "exception_review"


async def test_resolve_reask_does_not_change_status(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    # Re-ask is recorded for the worker, but resolve never drives the lifecycle:
    # status stays put and retry_count is untouched (re-queue is a manual status PUT).
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {}, "reasked_fields": [HEALTH_PLAN]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "exception_review"
    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == dispute_form))
        ).scalar_one()
        assert form.retry_count == 0


async def test_resolve_requires_forms_write(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.norole_token),
        json={"form_data": {}},
    )
    assert resp.status_code == 403, resp.text


# ---- status (PUT /patient-forms/{id}/status) --------------------------------


async def _make_plain_form(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
    status: FormStatus,
) -> UUID:
    """A bare form in `status` with no field answers (so no disputes)."""
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
        return form.id


async def _status(s: async_sessionmaker[AsyncSession], form_id: UUID) -> str:
    async with s() as sess:
        return (
            await sess.execute(select(PatientForm.status).where(PatientForm.id == form_id))
        ).scalar_one()


async def test_status_queues_a_ready_form(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "in_queue"
    assert await _status(admin_sessionmaker, form_id) == "in_queue"


async def test_status_rejects_illegal_transition(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # ready_for_processing → completed is not a permitted manual edge.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 422, resp.text
    assert await _status(admin_sessionmaker, form_id) == "ready_for_processing"


async def test_status_rejects_unknown_value(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "not_a_status"},
    )
    assert resp.status_code == 422, resp.text


async def test_complete_blocked_while_disputes_unresolved(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    # dispute_form is EXCEPTION_REVIEW with one unresolved dispute.
    resp = await client.put(
        f"/api/v1/patient-forms/{dispute_form}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["data"] == {"unresolved_disputes": 1}
    assert await _status(admin_sessionmaker, dispute_form) == "exception_review"


async def test_complete_succeeds_with_no_open_disputes(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 200, resp.text
    assert await _status(admin_sessionmaker, form_id) == "completed"


async def test_status_requires_forms_write(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.norole_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 403, resp.text


async def test_status_unknown_form_returns_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.put(
        f"/api/v1/patient-forms/{uuid4()}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 404, resp.text


async def test_status_same_status_is_idempotent_noop(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # Setting the current status is a no-op success — even for a worker-owned state
    # (no real transition happens), so a double-click can't error.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.IN_CALL,
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_call"},
    )
    assert resp.status_code == 200, resp.text
    assert await _status(admin_sessionmaker, form_id) == "in_call"
