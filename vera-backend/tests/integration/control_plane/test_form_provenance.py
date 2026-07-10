"""Integration test for form-detail provenance (FieldView.provenance).

Asserts that GET /patient-forms/{id} returns per-field provenance for ai_call
answers and null for human answers.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call, CallLineage
from vera_core.models.enums import AnswerSource, CallStatus, FormStatus, InsuranceType
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm

pytestmark = pytest.mark.integration


@pytest.fixture
async def provenance_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """Seed under rbac_world.tenant_id so the authed client can see it through RLS.

    Seeded data:
    - SchemaVersion version=995
    - PatientForm (status=AI_PROCESSING)
    - call1: full, completed, snapshot {} -> {'cov.a': 'x'}
    - call2: retry, completed, snapshot {'cov.a': 'x'} -> {'cov.a': 'x', 'cov.b': 'y'}
    - CallLineage: call2 -> call1
    - FieldAnswer(cov.b, ai_call, call2, is_current=True) + FieldEvaluation(88, True)
    - FieldAnswer(cov.a, human, is_current=True)

    Teardown in FK order; does NOT delete the tenant (owned by rbac_world).
    """
    tenant_id = rbac_world.tenant_id
    form_id = uuid7()
    call1_id = uuid7()
    call2_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    schema_id: UUID
    schema_id_to_delete: UUID | None = None

    try:
        async with sm() as session, session.begin():
            # find-or-create FormSchema (UNIQUE on insurance_type)
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
                    name="Provenance Test Schema",
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
                    version=995,
                    schema_json={},
                )
            )
            session.add(
                PatientForm(
                    id=form_id,
                    tenant_id=tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="Provenance Patient",
                    status=FormStatus.AI_PROCESSING.value,
                )
            )
            await session.flush()

            session.add(
                Call(
                    id=call1_id,
                    tenant_id=tenant_id,
                    form_id=form_id,
                    current_status=CallStatus.COMPLETED.value,
                    mode="full",
                )
            )
            session.add(
                Call(
                    id=call2_id,
                    tenant_id=tenant_id,
                    form_id=form_id,
                    current_status=CallStatus.COMPLETED.value,
                    mode="retry",
                )
            )
            await session.flush()

            session.add(
                CallLineage(
                    tenant_id=tenant_id,
                    retry_call_id=call2_id,
                    parent_call_id=call1_id,
                )
            )
            session.add(
                CallFormSnapshot(
                    tenant_id=tenant_id,
                    call_id=call1_id,
                    before_state={},
                    after_state={"cov.a": "x"},
                )
            )
            session.add(
                CallFormSnapshot(
                    tenant_id=tenant_id,
                    call_id=call2_id,
                    before_state={"cov.a": "x"},
                    after_state={"cov.a": "x", "cov.b": "y"},
                )
            )

            answer_b_id = uuid7()
            session.add(
                FieldAnswer(
                    id=answer_b_id,
                    tenant_id=tenant_id,
                    form_id=form_id,
                    field_path="cov.b",
                    value={"value": "y"},
                    source=AnswerSource.AI_CALL.value,
                    call_id=call2_id,
                    is_current=True,
                )
            )
            await session.flush()

            session.add(
                FieldEvaluation(
                    tenant_id=tenant_id,
                    answer_id=answer_b_id,
                    confidence=88,
                    supported=True,
                )
            )

            # human answer for cov.a
            session.add(
                FieldAnswer(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    form_id=form_id,
                    field_path="cov.a",
                    value={"value": "x"},
                    source=AnswerSource.HUMAN.value,
                    call_id=None,
                    is_current=True,
                )
            )

        yield form_id

    finally:
        async with sm() as session, session.begin():
            # Teardown in FK order, scoped to this fixture's form_id only
            # (rbac_world owns the tenant; we must NOT delete other tenant rows).
            await session.execute(
                text(
                    "DELETE FROM field_evaluation WHERE answer_id IN "
                    "(SELECT id FROM field_answer WHERE form_id = :fid)"
                ).bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM field_answer WHERE form_id = :fid").bindparams(fid=form_id)
            )
            await session.execute(
                text(
                    "DELETE FROM call_form_snapshot WHERE call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=form_id)
            )
            await session.execute(
                text(
                    "DELETE FROM call_lineage WHERE retry_call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            if schema_id_to_delete is not None:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(
                        fsid=schema_id_to_delete
                    )
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_detail_carries_provenance_for_ai_fields(
    client, rbac_world, provenance_form_id
) -> None:
    """provenance_form_id: form with one ai_call answer (from a retry call, judged
    supported/88) and one human answer. AI field gets provenance; human gets null."""
    resp = await client.get(
        f"/api/v1/patient-forms/{provenance_form_id}",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    fields = {f["field_path"]: f for f in resp.json()["data"]["fields"]}
    ai = fields["cov.b"]
    assert ai["provenance"]["mode"] == "retry"
    assert ai["provenance"]["attempt"] == 2
    assert ai["provenance"]["judge"]["confidence"] == 88
    assert ai["provenance"]["judge"]["supported"] is True
    human = fields["cov.a"]
    assert human["provenance"] is None


@pytest.mark.asyncio
async def test_calls_timeline(client, rbac_world, provenance_form_id) -> None:
    resp = await client.get(
        f"/api/v1/patient-forms/{provenance_form_id}/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    calls = resp.json()["data"]
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[1]["mode"] == "retry"
    assert calls[1]["retry_of"] == calls[0]["id"]
    assert calls[1]["changed_paths"] == ["cov.b"]
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_calls_timeline_unknown_form_404(client, rbac_world) -> None:
    resp = await client.get(
        f"/api/v1/patient-forms/{uuid7()}/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 404
