"""The ai_call answer writer against REAL Postgres — the `fa_current_uq` invariant.

The consumer handler locks the form row FOR UPDATE around `record_answer`, which
serializes writers within one control-plane replica — but the partial-unique index
`fa_current_uq` (one current row per (form_id, field_path)) is the backstop that must
hold for ANY pair of writers the row lock doesn't cover (e.g. across replicas, or the
human resolve path racing the worker). These tests pin that backstop where fakes can't:
`record_answer` is exercised bare (no form lock, like a cross-replica race) on the real
index, under commit ordering and a genuine two-session race. Skips without Postgres
(`just up`)."""

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.models import FieldAnswer, FormSchema, PatientForm, SchemaVersion
from vera_core.models.enums import AnswerSource, FormStatus, InsuranceType, VersionStatus
from vera_core.services.field_answers import record_answer

FIELD = "insurance_information.health_plan"


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
async def form_id(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
) -> AsyncGenerator[UUID]:
    """A minimal form with one INTAKE current answer for FIELD (the row the
    writers will race to supersede)."""
    async with admin_sessionmaker() as s, s.begin():
        form = PatientForm(
            tenant_id=rbac_world.tenant_id,
            schema_version_id=schema_version_id,
            status=FormStatus.IN_CALL.value,
            intake_payload={},
            completion_pct=0,
            retry_count=0,
        )
        s.add(form)
        await s.flush()
        s.add(
            FieldAnswer(
                tenant_id=rbac_world.tenant_id,
                form_id=form.id,
                field_path=FIELD,
                value={"value": "BCBS TX"},
                source=AnswerSource.INTAKE.value,
                is_current=True,
            )
        )
        created = form.id
    yield created
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("DELETE FROM patient_form WHERE id = :f").bindparams(f=created)
        )  # field_answer rows cascade


async def _write(
    session: AsyncSession, *, tenant_id: UUID, form_id: UUID, value: str, source: str
) -> bool:
    return await record_answer(
        session,
        tenant_id=tenant_id,
        form_id=form_id,
        call_id=None,
        field_path=FIELD,
        raw_value=value,
        source=source,
        confidence=90,
        evidence_seq=1,
    )


async def _current_rows(sm: async_sessionmaker[AsyncSession], form_id: UUID) -> list[FieldAnswer]:
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(FieldAnswer).where(
                        FieldAnswer.form_id == form_id,
                        FieldAnswer.field_path == FIELD,
                        FieldAnswer.is_current.is_(True),
                    )
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_supersede_and_redelivery_hold_one_current_row(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    form_id: UUID,
) -> None:
    sm = admin_sessionmaker
    # First worker write supersedes the intake answer…
    async with sm() as s, s.begin():
        wrote = await _write(
            s,
            tenant_id=rbac_world.tenant_id,
            form_id=form_id,
            value="Blue Cross",
            source=AnswerSource.AI_CALL.value,
        )
    assert wrote is True
    # …an at-least-once REDELIVERY of the identical answer is a committed no-op…
    async with sm() as s, s.begin():
        wrote = await _write(
            s,
            tenant_id=rbac_world.tenant_id,
            form_id=form_id,
            value="Blue Cross",
            source=AnswerSource.AI_CALL.value,
        )
    assert wrote is False
    # …and the real index holds exactly one current row (the ai_call value).
    current = await _current_rows(sm, form_id)
    assert len(current) == 1
    assert current[0].value == {"value": "Blue Cross"}
    assert current[0].source == AnswerSource.AI_CALL.value


@pytest.mark.asyncio
async def test_concurrent_writers_cannot_produce_two_current_rows(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    form_id: UUID,
) -> None:
    """A worker ai_call write racing a human write on the SAME field: both demote the
    same current row in overlapping transactions. `fa_current_uq` must reject the
    loser's insert — the DB can never hold two current rows (the loser's redelivery/
    retry is the recovery path, exactly the consumer's unacked-event semantics)."""
    sm = admin_sessionmaker
    s1, s2 = sm(), sm()
    loser_error: BaseException | None = None
    try:
        async with s1.begin():
            await _write(
                s1,
                tenant_id=rbac_world.tenant_id,
                form_id=form_id,
                value="Aetna",
                source=AnswerSource.AI_CALL.value,
            )

            async def second_writer() -> None:
                # Blocks on s1's row lock (both UPDATE the same current row), then
                # loses the fa_current_uq insert race once s1 commits.
                async with s2.begin():
                    await _write(
                        s2,
                        tenant_id=rbac_world.tenant_id,
                        form_id=form_id,
                        value="Cigna",
                        source=AnswerSource.HUMAN.value,
                    )

            task = asyncio.create_task(second_writer())
            await asyncio.sleep(0.3)  # let s2 reach and block on the row lock
            # exiting s1.begin() commits, releasing the lock into s2's flush
        try:
            await asyncio.wait_for(task, timeout=10)
        except IntegrityError as exc:
            loser_error = exc
    finally:
        await s1.close()
        await s2.close()

    # The loser MUST have been rejected by the index (never silently two-current).
    assert isinstance(loser_error, IntegrityError)
    assert "fa_current_uq" in str(loser_error.orig)
    current = await _current_rows(sm, form_id)
    assert len(current) == 1
    assert current[0].value == {"value": "Aetna"}  # the committed winner
