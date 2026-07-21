"""Integration tests for call_provenance service — validates SQL against real Postgres."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.db import tenant_session, uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call, CallLineage
from vera_core.models.enums import (
    AnswerSource,
    CallStatus,
    FormStatus,
    InsuranceType,
    RecordingStatus,
)
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.transcript import Recording
from vera_core.models.tenant import Tenant
from vera_core.services.call_provenance import load_call_attempts, load_field_provenance

pytestmark = pytest.mark.integration


@dataclass
class _TwoCallFormCtx:
    tenant_id: UUID
    form_id: UUID
    session: AsyncSession


@pytest.fixture
async def two_call_form_ctx(
    database_url: str,
) -> AsyncGenerator[_TwoCallFormCtx]:
    """Seed: Tenant → FormSchema (find-or-create) → SchemaVersion(version=996) →
    PatientForm → call1 (full, completed) → call2 (retry, completed) →
    CallLineage(call2→call1) → CallFormSnapshot for each call →
    FieldAnswer(cov.b, ai_call, call2, is_current=True) →
    FieldEvaluation(confidence=88, supported=True, evidence='said y').

    Snapshots:
      call1: before={}, after={'cov.a': 'x'}   → changed_paths=['cov.a']
      call2: before={'cov.a': 'x'}, after={'cov.a': 'x', 'cov.b': 'y'}
             → changed_paths=['cov.b']

    Tears down in FK order. Uses superuser engine (bypasses RLS); yields a
    tenant-pinned session so provenance queries run under the correct RLS context.
    """
    tenant_id = uuid7()
    form_id = uuid7()
    call1_id = uuid7()
    call2_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    # None means "we didn't create it, skip teardown delete".
    schema_id_to_delete: UUID | None = None
    schema_id: UUID

    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=str(tenant_id),
                name="CallProvenance Test Tenant",
                status="active",
            )
        )

        # find-or-create: FormSchema has UNIQUE(insurance_type).
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
                name="CallProvenance Test Schema",
            )
            session.add(fs)
            await session.flush()  # need fs.id before SchemaVersion
            schema_id = fs.id
            schema_id_to_delete = fs.id
        else:
            schema_id = existing.id

        session.add(
            SchemaVersion(
                id=schema_version_id,
                schema_id=schema_id,
                version=996,
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
        await session.flush()  # Call FK (form_id → patient_form) requires PatientForm to exist

        # call1: full, completed; call2: retry, completed (both flush together)
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
        await session.flush()  # Lineage/Snapshot FKs (call_id → call) require both Calls to exist

        # Lineage: call2 → call1
        session.add(
            CallLineage(
                tenant_id=tenant_id,
                retry_call_id=call2_id,
                parent_call_id=call1_id,
            )
        )
        # Snapshots
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
        # Recordings: call1 PENDING (not playable), call2 AVAILABLE (playable)
        session.add(
            Recording(
                id=uuid7(),
                tenant_id=tenant_id,
                call_id=call1_id,
                gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call1_id}.ogg",
                status=RecordingStatus.PENDING.value,
            )
        )
        session.add(
            Recording(
                id=uuid7(),
                tenant_id=tenant_id,
                call_id=call2_id,
                gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call2_id}.ogg",
                status=RecordingStatus.AVAILABLE.value,
            )
        )
        # FieldAnswer: cov.b from call2, current ai_call answer
        answer_id = uuid7()
        session.add(
            FieldAnswer(
                id=answer_id,
                tenant_id=tenant_id,
                form_id=form_id,
                field_path="cov.b",
                value={"text": "y"},
                source=AnswerSource.AI_CALL.value,
                call_id=call2_id,
                is_current=True,
            )
        )
        await session.flush()  # FieldEvaluation FK (answer_id) requires FieldAnswer to exist first

        # FieldEvaluation for the cov.b answer
        session.add(
            FieldEvaluation(
                tenant_id=tenant_id,
                answer_id=answer_id,
                confidence=88,
                supported=True,
                evidence="said y",
            )
        )

    async with tenant_session(sm, tenant_id) as test_session:
        yield _TwoCallFormCtx(
            tenant_id=tenant_id,
            form_id=form_id,
            session=test_session,
        )

    # teardown in FK order
    try:
        async with sm() as session, session.begin():
            for table in (
                "field_evaluation",
                "field_answer",
                "call_form_snapshot",
                "call_lineage",
                "recording",
                "call",
                "patient_form",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tid").bindparams(tid=tenant_id)
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
            await session.execute(
                text("DELETE FROM tenant WHERE id = :tid").bindparams(tid=tenant_id)
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attempts_lineage_and_diffs(two_call_form_ctx: _TwoCallFormCtx) -> None:
    """Fixture seeds: call1 (full, completed, snapshot {} -> {'cov.a': 'x'}) then
    call2 (retry, completed, snapshot {'cov.a': 'x'} -> {'cov.a': 'x', 'cov.b': 'y'},
    lineage call2->call1); current ai_call answer for cov.b from call2 with one
    FieldEvaluation(confidence=88, supported=True, evidence='said y')."""
    ctx = two_call_form_ctx
    attempts = await load_call_attempts(ctx.session, ctx.form_id)
    assert [a.attempt for a in attempts] == [1, 2]
    assert attempts[0].mode == "full" and attempts[1].mode == "retry"
    assert attempts[1].retry_of == attempts[0].id
    assert attempts[0].changed_paths == ["cov.a"]
    assert attempts[1].changed_paths == ["cov.b"]
    assert attempts[0].recording_available is False  # PENDING is not playable
    assert attempts[1].recording_available is True
    assert attempts[0].published is False
    assert attempts[0].initiated_by_id is None

    prov = await load_field_provenance(
        ctx.session, ctx.form_id, {a.id: (a.attempt, a.mode) for a in attempts}
    )
    assert prov["cov.b"].attempt == 2 and prov["cov.b"].mode == "retry"
    assert prov["cov.b"].judge is not None
    assert prov["cov.b"].judge.confidence == 88 and prov["cov.b"].judge.supported is True
