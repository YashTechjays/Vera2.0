"""Display + dispute-resolution endpoints (session-auth, real RLS):
`GET /api/v1/patient-forms`, `GET /api/v1/patient-forms/{id}`, and
`POST /api/v1/patient-forms/{id}/disputes:resolve`. Skips without Postgres."""

from collections.abc import AsyncGenerator
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.patient_forms import _unresolved_dispute_count
from control_plane.dispatch import drain_pending
from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld, seed_outbound_trunk
from vera_core.config.kms import LocalDevKMS
from vera_core.db.rls import tenant_session
from vera_core.models import (
    DisputeAction,
    FieldAnswer,
    FormSchema,
    InsuranceProvider,
    PatientForm,
    SchemaVersion,
)
from vera_core.models.enums import AnswerSource, FormStatus, InsuranceType, VersionStatus
from vera_core.services.queue_dispatcher import _resolve_provider

HEALTH_PLAN = "insurance_information.health_plan"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _assert_human_current_and_actions(
    sm: async_sessionmaker[AsyncSession],
    *,
    form_id: UUID,
    field_path: str,
    value: object,
    actions: list[str],
) -> None:
    """The current answer for `field_path` is a HUMAN checkpoint holding `value`, and the
    dispute_actions recorded for this form (scoped via field_answer) match `actions`."""
    async with sm() as s:
        current = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.field_path == field_path,
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert current.source == AnswerSource.HUMAN.value
        assert current.value == {"value": value}
        recorded = (
            (
                await s.execute(
                    select(DisputeAction.action)
                    .join(FieldAnswer, FieldAnswer.id == DisputeAction.answer_id)
                    .where(FieldAnswer.form_id == form_id)
                )
            )
            .scalars()
            .all()
        )
        assert recorded == actions


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
    # Enqueueing schedules a detached dispatch task — let it finish before purging,
    # or its call insert races the deletes below.
    await drain_pending()
    async with admin_sessionmaker() as s, s.begin():
        # Enqueueing a form fires the dispatcher, which creates call rows that
        # FK-reference the form — clear them (and their events) first.
        params = {"a": rbac_world.tenant_id, "b": rbac_world.other_tenant_id}
        await s.execute(
            text(
                "DELETE FROM call_event WHERE call_id IN "
                "(SELECT id FROM call WHERE tenant_id IN (:a, :b))"
            ).bindparams(**params)
        )
        await s.execute(text("DELETE FROM call WHERE tenant_id IN (:a, :b)").bindparams(**params))
        await s.execute(
            text("DELETE FROM patient_form WHERE tenant_id IN (:a, :b)").bindparams(**params)
        )


async def _make_form_with_dispute(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
    status: FormStatus = FormStatus.EXCEPTION_REVIEW,
    appointment_date: date | None = None,
) -> UUID:
    """Create a form with one undisputed INTAKE field, plus a disputed field: an INTAKE
    baseline answer + a current AI_CALL answer that diverges from it (the dispute signal
    is derived from field_answer history — no field_evaluation involved). Returns the
    form id."""
    async with sm() as s, s.begin():
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            status=status.value,
            intake_payload={"patient_information": {"patient_name": "Jane Doe"}},
            patient_name="jane doe",
            appointment_date=appointment_date,
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
        # Intake baseline for the disputed field (superseded by the AI capture).
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
        # Current AI capture that diverges from the baseline → disputed.
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path=HEALTH_PLAN,
                value={"value": "Blue Cross"},
                source=AnswerSource.AI_CALL.value,
                confidence=95,
                evidence="rep said so",
                is_current=True,
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


INSURANCE_PROVIDER_NAME = "sections.insurance_reference_information.insurance_provider_name"
INSURANCE_PHONE = "sections.insurance_reference_information.insurance_phone_number"


async def _make_form_with_promoted_field(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
) -> UUID:
    """A form whose current insurance_provider_name answer disagrees with the
    already-promoted patient_form.insurance_provider column — the bug this task fixes."""
    async with sm() as s, s.begin():
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            status=FormStatus.EXCEPTION_REVIEW.value,
            intake_payload={"patient_information": {"patient_name": "Jane Doe"}},
            patient_name="jane doe",
            insurance_provider="Stale Provider",
            completion_pct=0,
            retry_count=0,
        )
        s.add(form)
        await s.flush()
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path=INSURANCE_PROVIDER_NAME,
                value={"value": "Stale Provider"},
                source=AnswerSource.INTAKE.value,
                is_current=True,
            )
        )
        # A current answer for the other promoted field (patient_name) — resolve's
        # promotion re-derives EVERY promoted column from current_values, so without
        # this a resolve call would silently wipe form.patient_name to None.
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form.id,
                field_path="sections.patient_information.patient_name",
                value={"value": "Jane Doe"},
                source=AnswerSource.INTAKE.value,
                is_current=True,
            )
        )
        return form.id


@pytest.fixture
async def promoted_field_form(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> UUID:
    return await _make_form_with_promoted_field(
        admin_sessionmaker, tenant_id=rbac_world.tenant_id, schema_version_id=schema_version_id
    )


# ---- list -------------------------------------------------------------------


async def test_list_returns_paginated_summaries(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    resp = await client.get("/api/v1/patient-forms", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert {"items", "page", "page_size", "total"} <= body.keys()
    row = next(r for r in body["items"] if r["id"] == str(dispute_form))
    assert row["status"] == "exception_review"
    assert row["patient_name"] == "jane doe"
    # The worklist no longer carries a dispute count.
    assert "dispute_count" not in row


async def test_list_out_of_range_page_returns_empty_items_and_correct_total(
    client: httpx.AsyncClient, rbac_world: RBACWorld, dispute_form: UUID
) -> None:
    """The merged count+page query reads `total` off a `count(*) OVER()` window
    column on the page rows; an out-of-range page has no rows to read it from,
    so it falls back to a bare count. `total` must still be correct."""
    resp = await client.get(
        "/api/v1/patient-forms",
        params={"page": 2, "page_size": 20},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["items"] == []
    assert body["total"] == 1


async def test_list_total_reflects_full_filtered_count_not_just_the_page(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    """`total` must reflect every row matching the filter, not the page_size-limited
    slice actually returned — this is what the window function replaces two
    round trips (bare count + page select) with."""
    for _ in range(2):
        await _make_form_with_dispute(
            admin_sessionmaker, tenant_id=rbac_world.tenant_id, schema_version_id=schema_version_id
        )
    resp = await client.get(
        "/api/v1/patient-forms",
        params={"page": 1, "page_size": 1},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert len(body["items"]) == 1
    assert body["total"] == 2


async def test_list_sorts_by_appointment_date_with_nulls_last(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    """Server-side sort drives the Data Management worklist: the chosen column
    orders the whole result set (not just one page), dateless forms sink to the
    end in either direction."""
    dated = {
        d: await _make_form_with_dispute(
            admin_sessionmaker,
            tenant_id=rbac_world.tenant_id,
            schema_version_id=schema_version_id,
            appointment_date=d,
        )
        for d in (date(2026, 7, 10), date(2026, 7, 20))
    }
    undated = await _make_form_with_dispute(
        admin_sessionmaker, tenant_id=rbac_world.tenant_id, schema_version_id=schema_version_id
    )

    async def _ids(sort_dir: str) -> list[str]:
        resp = await client.get(
            "/api/v1/patient-forms",
            params={"sort_by": "appointment_date", "sort_dir": sort_dir},
            headers=_auth(rbac_world.admin_token),
        )
        assert resp.status_code == 200, resp.text
        return [row["id"] for row in resp.json()["data"]["items"]]

    newest_first = [str(dated[date(2026, 7, 20)]), str(dated[date(2026, 7, 10)]), str(undated)]
    assert await _ids("desc") == newest_first
    assert await _ids("asc") == [newest_first[1], newest_first[0], newest_first[2]]


async def test_list_rejects_unknown_sort_column(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        "/api/v1/patient-forms",
        params={"sort_by": "intake_payload"},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text


async def test_list_requires_forms_read(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get("/api/v1/patient-forms", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403, resp.text


async def test_virtual_assistant_can_list_patient_forms(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """VA now holds forms:read/forms:write (Data Management page access)."""
    resp = await client.get(
        "/api/v1/patient-forms", headers=_auth(rbac_world.virtual_assistant_token)
    )
    assert resp.status_code == 200, resp.text


# ---- schema version lookup ----------------------------------------------------


async def test_schema_version_returns_document(
    client: httpx.AsyncClient, rbac_world: RBACWorld, schema_version_id: UUID
) -> None:
    resp = await client.get(
        f"/api/v1/schema-versions/{schema_version_id}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    data = resp.json()["data"]
    assert data["id"] == str(schema_version_id)
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    assert data["document"]["dsl_version"].startswith("2.")
    # JSON (not JSONB) storage keeps document order: dsl_version is the first key.
    assert next(iter(data["document"])) == "dsl_version"


async def test_schema_version_requires_forms_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld, schema_version_id: UUID
) -> None:
    resp = await client.get(
        f"/api/v1/schema-versions/{schema_version_id}", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403, resp.text
    resp = await client.get(f"/api/v1/schema-versions/{schema_version_id}")
    assert resp.status_code == 401, resp.text


async def test_schema_version_unknown_id_is_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        f"/api/v1/schema-versions/{uuid4()}", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404, resp.text


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
        "previous_value": "BCBS TX",  # intake baseline
        "current_value": "Blue Cross",  # diverging AI value
        "confidence": 95,  # the AI answer's own confidence
        "reasoning": None,  # field_evaluation never gates the dispute *decision*
    }
    # Evidence hangs off the field, not the dispute. This form has no call and so no
    # judge verdict, so the merge falls back to the extractor's field_answer.evidence.
    assert fields[HEALTH_PLAN]["evidence"] == "rep said so"
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
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: "Blue Cross"}, "dispute_fields": [HEALTH_PLAN]},
    )
    assert resp.status_code == 200, resp.text
    # Accept advances the baseline: a HUMAN checkpoint equal to the AI value is written,
    # so the dispute clears, and an ACCEPT action is recorded.
    await _assert_human_current_and_actions(
        admin_sessionmaker,
        form_id=dispute_form,
        field_path=HEALTH_PLAN,
        value="Blue Cross",
        actions=["accept"],
    )
    # Detail no longer flags it.
    detail = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    fields = {f["field_path"]: f for f in detail.json()["data"]["fields"]}
    assert fields[HEALTH_PLAN]["dispute"] is None
    # Resolving disputes never changes status — completion is a separate manual
    # step via PUT .../status (which now passes the no-open-disputes gate).
    assert detail.json()["data"]["status"] == "exception_review"


async def test_resolve_accept_via_case_whitespace_variant_is_an_accept(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    # The AI value is "Blue Cross". Posting a case/whitespace variant of it must be treated
    # as an ACCEPT (consistent with the dispute rule), not a CORRECT — and the written human
    # checkpoint is the canonical AI value, not the posted variant.
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: " blue cross "}, "dispute_fields": [HEALTH_PLAN]},
    )
    assert resp.status_code == 200, resp.text
    await _assert_human_current_and_actions(
        admin_sessionmaker,
        form_id=dispute_form,
        field_path=HEALTH_PLAN,
        value="Blue Cross",  # the AI value, not the posted " blue cross "
        actions=["accept"],  # accept, not correct
    )


async def test_resolve_persists_recomputed_completion(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    """The recomputed completion_pct must be FLUSHED before session.refresh() —
    refresh discards pending attribute changes, which silently dropped the update.
    With the seeded v2 document, even an answerless form recomputes above 0
    (required leaves with a declared default count as filled)."""
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    resp = await client.post(
        f"/api/v1/patient-forms/{form_id}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {}, "dispute_fields": [], "reasked_fields": []},
    )
    assert resp.status_code == 200, resp.text
    returned = resp.json()["data"]["completion_pct"]
    assert returned > 0
    async with admin_sessionmaker() as s:
        stored = float(
            (
                await s.execute(select(PatientForm.completion_pct).where(PatientForm.id == form_id))
            ).scalar_one()
        )
    assert stored == returned  # persisted, not just echoed from the in-memory object


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


async def test_resolve_promotes_the_patient_form_column(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PROVIDER_NAME: "Corrected Provider"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider == "Corrected Provider"
        # Re-derivation covers every promoted column, not just the one being edited —
        # the untouched patient_name promoted column must survive intact.
        assert form.patient_name == "jane doe"


async def test_resolve_leaves_promoted_columns_untouched_for_a_non_promoted_field(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_verification.is_insurance_active": "Yes"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider == "Stale Provider"  # unchanged
        assert form.patient_name == "jane doe"  # unchanged, not wiped


async def test_resolve_with_invalid_promoted_date_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    """A date matching neither ISO nor the leaf's declared date_format
    (ibv_standard's patient_dob is "M/D/YYYY") must surface as a clean 422 from
    promote_columns's InvalidIntakeValue — not an unhandled 500 — mirroring
    upload_patient_form's existing handling."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.patient_dob": "not-a-date"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"]["fields"] == ["sections.patient_information.patient_dob"]
    assert body["message"] == "Patient Date of Birth: must be in the format M/D/YYYY"


async def test_resolve_accepts_a_date_in_the_leafs_declared_format(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    """Regression test for a live bug: the review UI prompts for and submits
    patient_dob in the schema's declared display format ("M/D/YYYY" for
    ibv_standard), not ISO — resolve must accept that, not 422."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.patient_dob": "12/4/1999"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.patient_dob == date(1999, 12, 4)


async def test_resolve_reformats_a_non_promoted_date_leaf_to_its_declared_format(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    """spouse_partner_dob is a date leaf but NOT one of the eight promoted
    columns — proves the fix isn't scoped to patient_dob/appointment_date:
    resolve must reformat ANY date leaf's ISO-submitted value to the leaf's
    declared date_format ("M/D/YYYY" for ibv_standard), matching what the review
    UI's edit-form validator expects when it re-populates from the stored value."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.spouse_partner_dob": "1999-12-04"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        current = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == promoted_field_form,
                    FieldAnswer.field_path == "sections.patient_information.spouse_partner_dob",
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert current.value == {"value": "12/4/1999"}


async def test_resolve_with_invalid_non_promoted_date_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    """Same 422 contract as the promoted-column case
    (`test_resolve_with_invalid_promoted_date_returns_422`), now for a date leaf
    that isn't one of the eight promoted columns."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.patient_information.spouse_partner_dob": "not-a-date"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"]["fields"] == ["sections.patient_information.spouse_partner_dob"]
    assert body["message"] == "Spouse DOB: must be in the format M/D/YYYY"


async def test_resolve_with_invalid_plan_effective_date_names_the_field_and_format(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    """VR2-187 regression (the ticket's exact field): a badly-formatted date used to
    422 with a generic "invalid field value", giving the reviewer no clue what was
    wrong — the message must name the field and the format the review UI asks for."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {"sections.benefit_coverage.plan_effective_date": "31/13/2026"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"]["fields"] == ["sections.benefit_coverage.plan_effective_date"]
    assert body["message"] == "Plan Effective Date: must be in the format M/D/YYYY"


async def test_resolve_auto_formats_missing_plus_on_insurance_phone(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PHONE: "15550100"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15550100"

        answer = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == promoted_field_form,
                    FieldAnswer.field_path == INSURANCE_PHONE,
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15550100"}
        assert answer.source == "human"


async def test_resolve_leaves_already_prefixed_phone_unchanged(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    promoted_field_form: UUID,
) -> None:
    """A number that already has a leading '+' is left exactly as submitted — no
    reformatting is applied beyond adding a missing '+' (2026-07-15 design doc)."""
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PHONE: "+15559990000"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text

    async with admin_sessionmaker() as s:
        form = (
            await s.execute(select(PatientForm).where(PatientForm.id == promoted_field_form))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15559990000"

        answer = (
            await s.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == promoted_field_form,
                    FieldAnswer.field_path == INSURANCE_PHONE,
                    FieldAnswer.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15559990000"}
        assert answer.source == "human"


async def test_resolve_with_invalid_promoted_phone_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    promoted_field_form: UUID,
) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{promoted_field_form}/disputes:resolve",
        json={"form_data": {INSURANCE_PHONE: "555 000 1234"}},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"]["fields"] == [INSURANCE_PHONE]
    assert body["message"] == "Insurance Provider Phone: must be a valid ISO formatted phone number"


# ---- baseline-derived dispute behavior --------------------------------------


async def _add_answer(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    form_id: UUID,
    field_path: str,
    value: object,
    source: str,
    confidence: int | None = None,
) -> None:
    """Add a current field_answer, superseding any existing current row for the path
    (keeps the `fa_current_uq` one-current-per-field invariant)."""
    async with sm() as s, s.begin():
        await s.execute(
            update(FieldAnswer)
            .where(
                FieldAnswer.form_id == form_id,
                FieldAnswer.field_path == field_path,
                FieldAnswer.is_current.is_(True),
            )
            .values(is_current=False)
        )
        s.add(
            FieldAnswer(
                tenant_id=tenant_id,
                form_id=form_id,
                field_path=field_path,
                value={"value": value},
                source=source,
                confidence=confidence,
                is_current=True,
            )
        )


async def test_no_baseline_ai_value_is_disputed(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # An AI value with no intake/human baseline diverges from NULL → disputed.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Blue Cross",
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    d = {f["field_path"]: f for f in detail.json()["data"]["fields"]}[HEALTH_PLAN]["dispute"]
    assert d is not None
    assert d["previous_value"] is None
    assert d["current_value"] == "Blue Cross"
    # Completion is blocked while the (baseline-derived) dispute is open — the complete-gate
    # derives the dispute from the same Python rule as the detail.
    block = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert block.status_code == 409, block.text


async def test_resolve_baseline_edit_writes_no_dispute_action(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # Editing a field whose current value is intake (no diverging AI) advances the
    # baseline with a HUMAN answer but emits NO dispute_action (gated on an open dispute).
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="BCBS TX",
        source=AnswerSource.INTAKE.value,
    )
    resp = await client.post(
        f"/api/v1/patient-forms/{form_id}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: "Aetna"}},
    )
    assert resp.status_code == 200, resp.text
    # Baseline edit: a HUMAN answer is written but NO dispute_action is recorded.
    await _assert_human_current_and_actions(
        admin_sessionmaker,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Aetna",
        actions=[],
    )


async def test_redivergence_reopens_after_resolution(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    dispute_form: UUID,
) -> None:
    # Resolve via correction → the human baseline becomes "Aetna".
    resp = await client.post(
        f"/api/v1/patient-forms/{dispute_form}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json={"form_data": {HEALTH_PLAN: "Aetna"}},
    )
    assert resp.status_code == 200, resp.text

    cleared = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(cleared) is None

    # A later AI capture that diverges from the new baseline reopens the dispute.
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=dispute_form,
        field_path=HEALTH_PLAN,
        value="Cigna",
        source=AnswerSource.AI_CALL.value,
        confidence=70,
    )
    reopened = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    d = _dispute_for(reopened)
    assert d is not None
    assert d["previous_value"] == "Aetna"
    assert d["current_value"] == "Cigna"

    # An AI capture that re-confirms the baseline does not.
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=dispute_form,
        field_path=HEALTH_PLAN,
        value="Aetna",
        source=AnswerSource.AI_CALL.value,
        confidence=90,
    )
    reconfirmed = await client.get(
        f"/api/v1/patient-forms/{dispute_form}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(reconfirmed) is None


# ---- dispute scenario matrix (team-enumerated cases) ------------------------
# HEALTH_PLAN stands in for any single field (e.g. coordination-of-benefit) — the
# dispute rule is field-agnostic. Histories are applied in order; `_add_answer`
# supersedes the prior current row, so the baseline is the most recent intake/human
# answer and AI answers never become a baseline.


def _dispute_for(detail: httpx.Response, field_path: str = HEALTH_PLAN) -> dict[str, object] | None:
    fields = {f["field_path"]: f for f in detail.json()["data"]["fields"]}
    dispute: dict[str, object] | None = fields[field_path]["dispute"]
    return dispute


# Each case: an ordered (source, value) history → expected dispute. `None` means no
# dispute; a (previous_value, current_value) tuple means disputed with those values
# (either may itself be None for a JSON-null value).
_DISPUTE_SCENARIOS = [
    pytest.param(
        [("intake", None), ("ai_call", "Tertiary")],
        (None, "Tertiary"),
        id="1-1_intake_null_then_ai_value",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", None)],
        ("Primary", None),
        id="2-1_intake_value_then_ai_null",
    ),
    pytest.param(
        [("human", "Primary"), ("ai_call", "Tertiary")],
        ("Primary", "Tertiary"),
        id="3-1_human_baseline_then_ai_diverge",
    ),
    pytest.param(
        [("human", "Primary"), ("ai_call", "Primary")],
        None,
        id="3-2_human_baseline_then_ai_match",
    ),
    pytest.param(
        [("intake", "Primary"), ("human", "Secondary"), ("ai_call", "Tertiary")],
        ("Secondary", "Tertiary"),
        id="4-1_human_edit_then_ai_diverge",
    ),
    pytest.param(
        [("intake", "Primary"), ("human", "Secondary"), ("ai_call", "Primary")],
        ("Secondary", "Primary"),
        id="4-2_ai_matches_old_intake_not_human_edit",
    ),
    pytest.param(
        [("intake", "Primary"), ("human", "Secondary"), ("ai_call", "Secondary")],
        None,
        id="4-3_ai_matches_human_edit",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "Tertiary")],
        ("Primary", "Tertiary"),
        id="5-1_intake_then_ai_diverge",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "Primary")],
        None,
        id="5-2_intake_then_ai_match",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "Secondary"), ("ai_call", "Secondary")],
        ("Primary", "Secondary"),
        id="6-1_two_calls_both_diverge",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "Primary"), ("ai_call", "Secondary")],
        ("Primary", "Secondary"),
        id="6-2_first_call_matches_second_diverges",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "Secondary"), ("ai_call", "Primary")],
        None,
        id="6-3_second_call_back_to_intake",
    ),
    # Normalization: case/whitespace-only differences are not disputes.
    pytest.param(
        [("intake", "Primary"), ("ai_call", " primary ")],
        None,
        id="norm-1_case_and_whitespace_only",
    ),
    pytest.param(
        [("intake", "Primary"), ("ai_call", "PRIMARY")],
        None,
        id="norm-2_case_only",
    ),
]


@pytest.mark.parametrize("history, expected", _DISPUTE_SCENARIOS)
async def test_dispute_scenarios(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    history: list[tuple[str, object]],
    expected: tuple[object, object] | None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    for source, value in history:
        await _add_answer(
            admin_sessionmaker,
            tenant_id=rbac_world.tenant_id,
            form_id=form_id,
            field_path=HEALTH_PLAN,
            value=value,
            source=source,
        )
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    d = _dispute_for(detail)
    if expected is None:
        assert d is None
    else:
        previous_value, current_value = expected
        assert d is not None
        assert d["previous_value"] == previous_value
        assert d["current_value"] == current_value


# Group 7: a human resolves call 1's dispute via the :resolve endpoint (advancing
# the baseline), then a second AI call lands. Resolution payloads mirror the
# correct/override and accept patterns above.
_RESOLUTION_SCENARIOS = [
    pytest.param(
        {"form_data": {HEALTH_PLAN: "Primary"}},
        "Secondary",
        ("Primary", "Secondary"),
        id="7-1_resolved_to_primary_then_ai_secondary",
    ),
    pytest.param(
        {"form_data": {HEALTH_PLAN: "Secondary"}, "dispute_fields": [HEALTH_PLAN]},
        "Secondary",
        None,
        id="7-2_accepted_secondary_then_ai_secondary",
    ),
    pytest.param(
        {"form_data": {HEALTH_PLAN: "Secondary"}, "dispute_fields": [HEALTH_PLAN]},
        "Primary",
        ("Secondary", "Primary"),
        id="7-3_accepted_secondary_then_ai_primary",
    ),
    pytest.param(
        {"form_data": {HEALTH_PLAN: "Secondary"}, "dispute_fields": [HEALTH_PLAN]},
        "Tertiary",
        ("Secondary", "Tertiary"),
        id="7-4_accepted_secondary_then_ai_tertiary",
    ),
]


@pytest.mark.parametrize("resolution, call2, expected", _RESOLUTION_SCENARIOS)
async def test_dispute_after_human_resolution(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    resolution: dict[str, object],
    call2: object,
    expected: tuple[object, object] | None,
) -> None:
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    # Intake baseline + call 1 (diverging AI capture) → an open dispute.
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Primary",
        source=AnswerSource.INTAKE.value,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Secondary",
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    # Human resolves the dispute (advances the baseline).
    resolved = await client.post(
        f"/api/v1/patient-forms/{form_id}/disputes:resolve",
        headers=_auth(rbac_world.admin_token),
        json=resolution,
    )
    assert resolved.status_code == 200, resolved.text
    # Call 2 lands a new AI capture.
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value=call2,
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    d = _dispute_for(detail)
    if expected is None:
        assert d is None
    else:
        previous_value, current_value = expected
        assert d is not None
        assert d["previous_value"] == previous_value
        assert d["current_value"] == current_value


async def test_case_whitespace_variant_agrees_across_count_and_detail(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # An AI value differing from the baseline only by case + whitespace is not a dispute.
    # Asserts the gate count (_unresolved_dispute_count, which backs the complete-gate) and
    # the detail agree — both derive the dispute from the one Python rule.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Primary",
        source=AnswerSource.INTAKE.value,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value=" primary ",
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    async with admin_sessionmaker() as s:
        count = await _unresolved_dispute_count(s, form_id)
    assert count == 0  # gate count
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(detail) is None  # detail agrees


async def test_non_ascii_whitespace_variant_agrees_across_count_and_detail(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # A non-breaking space (U+00A0) is NOT ASCII whitespace, so it is not stripped: both the
    # gate count and the detail must treat it as a dispute — they share the one Python rule.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Primary",
        source=AnswerSource.INTAKE.value,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="\u00a0Primary",
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    async with admin_sessionmaker() as s:
        count = await _unresolved_dispute_count(s, form_id)
    assert count == 1  # gate count keeps the NBSP → dispute
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(detail) is not None  # detail agrees → dispute


async def test_json_null_ai_value_with_no_baseline_is_not_a_dispute(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # A current ai_call answer of {"value": null} with NO intake/human baseline must NOT be
    # a dispute (None != None → False). The gate count and the detail share the Python rule,
    # so they agree — and completion is not blocked by a phantom dispute.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value=None,
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    async with admin_sessionmaker() as s:
        count = await _unresolved_dispute_count(s, form_id)
    assert count == 0  # gate count
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(detail) is None  # detail agrees
    done = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert done.status_code == 200, done.text  # not blocked by a phantom dispute


async def test_json_null_ai_value_with_baseline_is_a_dispute(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    # ai_call cleared a field the baseline had set → null IS DISTINCT FROM value → dispute,
    # on both the gate count and the detail.
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value="Primary",
        source=AnswerSource.INTAKE.value,
    )
    await _add_answer(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        form_id=form_id,
        field_path=HEALTH_PLAN,
        value=None,
        source=AnswerSource.AI_CALL.value,
        confidence=80,
    )
    async with admin_sessionmaker() as s:
        count = await _unresolved_dispute_count(s, form_id)
    assert count == 1  # gate count
    detail = await client.get(
        f"/api/v1/patient-forms/{form_id}", headers=_auth(rbac_world.admin_token)
    )
    assert _dispute_for(detail) is not None  # detail agrees
    block = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert block.status_code == 409, block.text


# ---- status (PUT /patient-forms/{id}/status) --------------------------------


async def _make_plain_form(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
    status: FormStatus,
    phone: str | None = None,
    insurance_provider: str | None = None,
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
            insurance_provider_phone_number=phone,
            insurance_provider=insurance_provider,
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
    trunk_integration_type: None,
) -> None:
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
        phone="+15551234567",
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    # The response acknowledges the manual transition; the dispatcher then fires
    # as a detached post-commit task and (with free slots and FakeLiveKit)
    # dispatches the form — drain it before asserting on its effects.
    assert resp.json()["data"]["status"] == "in_queue"
    await drain_pending()
    assert await _status(admin_sessionmaker, form_id) == "in_call"


async def test_status_manual_requeue_bypasses_exhausted_retry_budget(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    trunk_integration_type: None,
) -> None:
    """The retry cap bounds the AUTOMATIC redial loop within one enqueue
    episode; an operator's manual requeue starts a fresh episode — it must
    succeed even at the cap, and reset the budget for the new episode."""
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.CALL_FAILED,
        phone="+15551234567",
    )
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as s:
        form = (await s.execute(select(PatientForm).where(PatientForm.id == form_id))).scalar_one()
        form.retry_count = 5  # tenant max_retries — auto-retry budget exhausted

    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    await drain_pending()
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as s:
        form = (await s.execute(select(PatientForm).where(PatientForm.id == form_id))).scalar_one()
        assert form.retry_count == 0  # fresh episode: full auto-retry allowance
        assert form.status in ("in_queue", "in_call")  # dispatcher may already have fired


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


# ---- review_reason ----------------------------------------------------------


@pytest.fixture
async def review_form_id(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> UUID:
    """A plain EXCEPTION_REVIEW form with no disputes — used for review_reason tests.
    Carries a valid E.164 provider phone: the requeue test drives it back to
    IN_QUEUE, which the queueability gate refuses without one."""
    return await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.EXCEPTION_REVIEW,
        phone="+15551234567",
    )


@pytest.mark.asyncio
async def test_requeue_clears_review_reason(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    review_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    trunk_integration_type: None,
) -> None:
    """Leaving EXCEPTION_REVIEW by hand nulls review_reason."""
    # Requeue runs the queueability gate — the tenant needs an outbound trunk.
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    # Seed the reason directly (the pipeline normally stamps it).
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE patient_form SET review_reason = 'llm_error' WHERE id = :fid").bindparams(
                fid=review_form_id
            )
        )
    resp = await client.put(
        f"/api/v1/patient-forms/{review_form_id}/status",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    async with admin_sessionmaker() as session:
        reason = (
            await session.execute(
                text("SELECT review_reason FROM patient_form WHERE id = :fid").bindparams(
                    fid=review_form_id
                )
            )
        ).scalar_one()
    assert reason is None


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


# ---- send-to-queue provider selection ---------------------------------------
# The picker canonicalizes the form's insurance_provider string to the chosen
# catalog name so the async dispatcher resolves the right provider + IVR playbook.


async def _seed_provider(
    sm: async_sessionmaker[AsyncSession], *, name: str, status: str = "active"
) -> UUID:
    """A GLOBAL insurance_provider row (the caller deletes it in a finally)."""
    provider_id = uuid4()
    async with sm() as s, s.begin():
        s.add(InsuranceProvider(id=provider_id, name=name, status=status))
    return provider_id


async def _delete_provider(sm: async_sessionmaker[AsyncSession], provider_id: UUID) -> None:
    async with sm() as s, s.begin():
        # A dispatched call FK-references the provider (cleanup_forms deletes the call
        # later); detach it first so the provider row is deletable now.
        await s.execute(
            text(
                "UPDATE call SET insurance_provider_id = NULL WHERE insurance_provider_id = :i"
            ).bindparams(i=provider_id)
        )
        await s.execute(
            text("DELETE FROM insurance_provider WHERE id = :i").bindparams(i=provider_id)
        )


async def _provider_string(sm: async_sessionmaker[AsyncSession], form_id: UUID) -> str | None:
    async with sm() as s:
        return (
            await s.execute(select(PatientForm.insurance_provider).where(PatientForm.id == form_id))
        ).scalar_one()


async def test_status_canonicalizes_provider_from_picked_id(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    trunk_integration_type: None,
) -> None:
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    name = f"Cigna {uuid4().hex[:8]}"
    provider_id = await _seed_provider(admin_sessionmaker, name=name)
    try:
        # Intake string deliberately disagrees with the catalog name.
        form_id = await _make_plain_form(
            admin_sessionmaker,
            tenant_id=rbac_world.tenant_id,
            schema_version_id=schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING,
            phone="+15551234567",
            insurance_provider="Stale Provider",
        )
        resp = await client.put(
            f"/api/v1/patient-forms/{form_id}/status",
            headers=_auth(rbac_world.admin_token),
            json={"status": "in_queue", "insurance_provider_id": str(provider_id)},
        )
        assert resp.status_code == 200, resp.text
        await drain_pending()
        # The string is now the exact catalog name — dispatch resolves the provider.
        assert await _provider_string(admin_sessionmaker, form_id) == name
    finally:
        await _delete_provider(admin_sessionmaker, provider_id)


async def test_status_rejects_unknown_provider_id(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    trunk_integration_type: None,
) -> None:
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
        phone="+15551234567",
        insurance_provider="Original",
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue", "insurance_provider_id": str(uuid4())},
    )
    assert resp.status_code == 422, resp.text
    # Rejected before the transition: status and the string both stand.
    assert await _status(admin_sessionmaker, form_id) == "ready_for_processing"
    assert await _provider_string(admin_sessionmaker, form_id) == "Original"


async def test_status_without_provider_id_leaves_string_untouched(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    schema_version_id: UUID,
    cleanup_forms: None,
    trunk_integration_type: None,
) -> None:
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
    form_id = await _make_plain_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        status=FormStatus.READY_FOR_PROCESSING,
        phone="+15551234567",
        insurance_provider="Original",
    )
    resp = await client.put(
        f"/api/v1/patient-forms/{form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    await drain_pending()
    assert await _provider_string(admin_sessionmaker, form_id) == "Original"


async def test_list_form_insurance_providers_returns_active_only(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    active_id = await _seed_provider(admin_sessionmaker, name=f"Active {uuid4().hex[:8]}")
    inactive_id = await _seed_provider(
        admin_sessionmaker, name=f"Inactive {uuid4().hex[:8]}", status="inactive"
    )
    try:
        # Literal path is matched (not captured as a {form_id} UUID param).
        resp = await client.get(
            "/api/v1/patient-forms/insurance-providers",
            headers=_auth(rbac_world.admin_token),
        )
        assert resp.status_code == 200, resp.text
        ids = {p["id"] for p in resp.json()["data"]}
        assert str(active_id) in ids
        assert str(inactive_id) not in ids
    finally:
        await _delete_provider(admin_sessionmaker, active_id)
        await _delete_provider(admin_sessionmaker, inactive_id)


async def test_list_form_insurance_providers_requires_forms_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        "/api/v1/patient-forms/insurance-providers",
        headers=_auth(rbac_world.norole_token),
    )
    assert resp.status_code == 403, resp.text


async def test_resolve_provider_matches_case_insensitively(
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    name = f"CignaCase{uuid4().hex[:8]}"
    provider_id = await _seed_provider(admin_sessionmaker, name=name)
    try:
        form = PatientForm(insurance_provider=f"  {name.lower()}  ")  # cased + padded
        async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as s:
            provider = await _resolve_provider(s, form)
        assert provider is not None
        assert provider.id == provider_id
    finally:
        await _delete_provider(admin_sessionmaker, provider_id)
