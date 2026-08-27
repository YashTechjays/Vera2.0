"""Which of a form's calls count as authoritative (they captured a reference number)."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.db import tenant_session, uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import AnswerSource, CallStatus, FormStatus, InsuranceType
from vera_core.models.field_answer import FieldAnswer
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant
from vera_core.services.field_status import load_authoritative_call_ids

pytestmark = pytest.mark.integration

REF = "sections.insurance_representative.call_reference_number"


@dataclass
class _AuthoritativeCallsCtx:
    tenant_id: UUID
    form_id: UUID
    session: AsyncSession


@pytest.fixture
async def authoritative_calls_ctx(database_url: str) -> AsyncGenerator[_AuthoritativeCallsCtx]:
    """Seed: Tenant -> FormSchema (find-or-create) -> SchemaVersion -> PatientForm, with no
    Call/FieldAnswer rows — each test adds its own via make_call/make_answer so the four
    authority scenarios stay independent.

    Uses the superuser engine (bypasses RLS) for setup/teardown; yields a tenant-pinned
    session so it mirrors production RLS context. Tears down in FK order.
    """
    tenant_id = uuid7()
    form_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    schema_id_to_delete: UUID | None = None
    schema_id: UUID

    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=str(tenant_id),
                name="AuthoritativeCalls Test Tenant",
                status="active",
            )
        )

        # find-or-create: FormSchema has UNIQUE(insurance_type).
        existing = (
            await session.execute(
                select(FormSchema).where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            fs = FormSchema(
                id=uuid7(),
                insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                name="AuthoritativeCalls Test Schema",
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
                patient_name="AuthoritativeCalls Test Patient",
                status=FormStatus.AI_PROCESSING.value,
            )
        )

    async with tenant_session(sm, tenant_id) as test_session:
        yield _AuthoritativeCallsCtx(tenant_id=tenant_id, form_id=form_id, session=test_session)

    # teardown in FK order
    try:
        async with sm() as session, session.begin():
            for table in ("field_answer", "call", "patient_form"):
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


async def make_call(ctx: _AuthoritativeCallsCtx) -> Call:
    """Insert one terminal Call for ctx's form — terminal so a test creating two calls for the
    same form never trips the at-most-one-live-call partial unique index."""
    call = Call(
        tenant_id=ctx.tenant_id, form_id=ctx.form_id, current_status=CallStatus.COMPLETED.value
    )
    ctx.session.add(call)
    await ctx.session.flush()
    return call


async def make_answer(
    ctx: _AuthoritativeCallsCtx,
    call: Call | None,
    field_path: str,
    value: str,
    *,
    source: str = AnswerSource.AI_CALL.value,
    is_current: bool = True,
) -> FieldAnswer:
    """Insert one FieldAnswer for ctx's form; *call* None means intake/human (call_id NULL)."""
    answer = FieldAnswer(
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=call.id if call is not None else None,
        field_path=field_path,
        value={"value": value},
        source=source,
        is_current=is_current,
    )
    ctx.session.add(answer)
    await ctx.session.flush()
    return answer


@pytest.mark.asyncio
async def test_a_call_that_captured_a_reference_is_authoritative(
    authoritative_calls_ctx: _AuthoritativeCallsCtx,
) -> None:
    ctx = authoritative_calls_ctx
    call = await make_call(ctx)
    await make_answer(ctx, call, REF, "8842-QX-77")
    result = await load_authoritative_call_ids(ctx.session, ctx.form_id, reference_field=REF)
    assert result == {call.id}


@pytest.mark.asyncio
async def test_a_call_with_no_reference_answer_is_not(
    authoritative_calls_ctx: _AuthoritativeCallsCtx,
) -> None:
    ctx = authoritative_calls_ctx
    call = await make_call(ctx)
    await make_answer(ctx, call, "sections.deductibles.individual.total", "$3,000")
    result = await load_authoritative_call_ids(ctx.session, ctx.form_id, reference_field=REF)
    assert result == frozenset()


@pytest.mark.asyncio
async def test_a_superseded_reference_still_makes_its_call_authoritative(
    authoritative_calls_ctx: _AuthoritativeCallsCtx,
) -> None:
    """Attempt 2's reference supersedes attempt 1's, but attempt 1 was still authoritative — an
    `is_current` filter here would demote it and re-ask everything it collected."""
    ctx = authoritative_calls_ctx
    first = await make_call(ctx)
    second = await make_call(ctx)
    await make_answer(ctx, first, REF, "R1", is_current=False)
    await make_answer(ctx, second, REF, "R2", is_current=True)
    result = await load_authoritative_call_ids(ctx.session, ctx.form_id, reference_field=REF)
    assert result == {first.id, second.id}


@pytest.mark.asyncio
async def test_an_intake_answer_at_the_reference_path_makes_no_call_authoritative(
    authoritative_calls_ctx: _AuthoritativeCallsCtx,
) -> None:
    """An intake row has call_id NULL; authority comes from a CALL having captured it."""
    ctx = authoritative_calls_ctx
    await make_answer(ctx, None, REF, "R-from-sheet", source=AnswerSource.INTAKE.value)
    result = await load_authoritative_call_ids(ctx.session, ctx.form_id, reference_field=REF)
    assert result == frozenset()
