"""Integration tests for evaluate_call — the post-call eval orchestration.

Exercises the three key behaviours against a real Docker-Postgres instance
(the same test-database as the other integration suites):
  1. happy path: writes FieldAnswer + FieldEvaluation rows, form → COMPLETED.
  2. token-valued field: skips the write, routes form → EXCEPTION_REVIEW.
  3. redelivery: second call is a no-op (idempotency guard).

Seeding pattern mirrors tests/integration/control_plane/test_call_queue.py:
  - superuser engine from `database_url` (bypasses RLS — needed for Tenant insert)
  - a fresh Tenant + FormSchema + SchemaVersion + PatientForm(AI_PROCESSING) + Call
    are created per-test and torn down in a try/finally.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.audit import AuditRecord
from vera_core.db import tenant_session, uuid7
from vera_core.integrations.llm import ExtractedField, FakeLLMClient, JudgeVerdict, TranscriptTurn
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import AnswerSource, CallStatus, FormStatus, InsuranceType
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant
from vera_core.services.post_call_eval import EvalDeps, evaluate_call

# ---------------------------------------------------------------------------
# A minimal valid DSL v2 schema with one required ask field.
# dsl_version "2.1" → is_v2() returns True → completion_pct_v2() branch.
# The ask field needs prompt.ask per the DSL validator.
# ---------------------------------------------------------------------------
_SCHEMA_JSON: dict[str, object] = {
    "dsl_version": "2.1",
    "name": "PostCallEval Test Schema",
    "insurance_type": "infertility_treatment",
    "sections": {
        "coverage": {
            "title": "Coverage",
            "role": "collect",
            "fields": {
                "in_network": {
                    "type": "text",
                    "title": "In Network",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "Is the patient in-network?"},
                }
            },
        }
    },
    "tasks": [
        {
            "task_key": "main",
            "title": "Main Task",
            "sections": ["coverage"],
        }
    ],
}

# The single collection path the minimal schema exposes.
_COLLECTION_PATH = "sections.coverage.in_network"


# ---------------------------------------------------------------------------
# Stub AuditSink — collects records without writing to DB (tests run as
# superuser which is not RLS-constrained, so a DB sink would work too, but
# an in-memory sink keeps the test self-contained).
# ---------------------------------------------------------------------------


class _FakeAuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Fake LiveKit — try_dispatch calls create_call_room on it; we don't want
# a real LiveKit server, and dispatch may be a no-op if no forms are queued.
# ---------------------------------------------------------------------------


class _FakeLiveKit:
    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        pass

    async def delete_room(self, room_name: str) -> None:
        pass

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        pass


# ---------------------------------------------------------------------------
# Seed context returned by the fixture.
# ---------------------------------------------------------------------------


@dataclass
class _SeedCtx:
    tenant_id: UUID
    form_id: UUID
    call_id: UUID
    collection_path: str
    session: AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_audit() -> _FakeAuditSink:
    return _FakeAuditSink()


@pytest.fixture
def fake_livekit() -> _FakeLiveKit:
    return _FakeLiveKit()


@pytest.fixture
async def seeded_ai_processing_form(
    database_url: str,
) -> AsyncGenerator[_SeedCtx]:
    """Insert a Tenant + schema chain + PatientForm(AI_PROCESSING) + Call.

    Uses the superuser engine (bypasses RLS) so we can insert a Tenant row.
    `form_schema.insurance_type` is UNIQUE — uses find-or-create (mirrors the
    pattern in tests/integration/control_plane/test_call_queue.py) so the
    fixture is safe whether the schema row was seeded in an earlier test or not.
    Teardown is scoped: only rows THIS fixture created are removed.
    """
    tenant_id = uuid7()
    form_id = uuid7()
    call_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    created_schema = False
    schema_id: UUID

    # -- seed ----------------------------------------------------------------
    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=str(tenant_id),
                name="PostCallEval Test Tenant",
                status="active",
            )
        )
        # find-or-create form_schema to avoid UNIQUE collision
        existing_schema = (
            await session.execute(
                select(FormSchema).where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                )
            )
        ).scalar_one_or_none()
        if existing_schema is None:
            new_schema = FormSchema(
                id=uuid7(),
                insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                name="PostCallEval Test Schema",
            )
            session.add(new_schema)
            await session.flush()
            schema_id = new_schema.id
            created_schema = True
        else:
            schema_id = existing_schema.id

        session.add(
            SchemaVersion(
                id=schema_version_id,
                schema_id=schema_id,
                version=999,
                schema_json=_SCHEMA_JSON,
            )
        )
        await session.flush()
        session.add(
            PatientForm(
                id=form_id,
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Test Patient",
                status=FormStatus.AI_PROCESSING.value,
            )
        )
        await session.flush()
        session.add(
            Call(
                id=call_id,
                tenant_id=tenant_id,
                form_id=form_id,
                current_status=CallStatus.COMPLETED.value,
                mode="full",
            )
        )

    # -- open a tenant-pinned session for the test ---------------------------
    # `tenant_session` opens the session, begins the transaction, and pins the
    # RLS tenant GUC in one step (the sanctioned PHI-work entrypoint).
    async with tenant_session(sm, tenant_id) as test_session:
        ctx = _SeedCtx(
            tenant_id=tenant_id,
            form_id=form_id,
            call_id=call_id,
            collection_path=_COLLECTION_PATH,
            session=test_session,
        )
        yield ctx

    # -- teardown (FK order) -------------------------------------------------
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("DELETE FROM field_evaluation WHERE tenant_id = :tid").bindparams(
                    tid=tenant_id
                )
            )
            await session.execute(
                text("DELETE FROM field_answer WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM call_form_snapshot WHERE tenant_id = :tid").bindparams(
                    tid=tenant_id
                )
            )
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE tenant_id = :tid)"
                ).bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            if created_schema:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=schema_id)
                )
            await session.execute(
                text("DELETE FROM tenant WHERE id = :tid").bindparams(tid=tenant_id)
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_evaluate_call_writes_answers_and_completes(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [
        TranscriptTurn(0, "agent", "are they in network"),
        TranscriptTurn(1, "user", "yes in network"),
    ]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "in-network", 92, 1)],
        verdicts=[JudgeVerdict(path, True, 88, "yes in network")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert outcome.status == FormStatus.COMPLETED
    assert outcome.answers_written == 1
    rows = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == ctx.form_id,
                    FieldAnswer.source == AnswerSource.AI_CALL.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].evidence == "yes in network"
    assert rows[0].is_current is True

    evals = (
        (
            await ctx.session.execute(
                select(FieldEvaluation).where(FieldEvaluation.answer_id == rows[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(evals) == 1
    assert evals[0].supported is True


async def test_token_valued_field_routes_to_review(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "user", "member id is [[MEMBER_ID_1]]")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "[[MEMBER_ID_1]]", 99, 0)],
        verdicts=[JudgeVerdict(path, True, 99, "member id")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert outcome.status == FormStatus.EXCEPTION_REVIEW

    rows = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(FieldAnswer.source == AnswerSource.AI_CALL.value)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # token value must never be stored


async def test_redelivery_is_a_noop(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "user", "in network")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "in-network", 92, 0)],
        verdicts=[JudgeVerdict(path, True, 88, "in network")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )
    second = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert second.answers_written == 0

    rows = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(FieldAnswer.source == AnswerSource.AI_CALL.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # not doubled
