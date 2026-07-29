"""Integration tests for evaluate_call — the post-call eval orchestration.

Exercises the three key behaviours against a real Docker-Postgres instance
(the same test-database as the other integration suites):
  1. happy path: writes FieldAnswer + FieldEvaluation rows, form → EXCEPTION_REVIEW
     (all-satisfied still parks for human sign-off — the pipeline never auto-COMPLETEs).
  2. token-valued field: skips the write, routes form → EXCEPTION_REVIEW.
  3. redelivery: second call is a no-op (status guard: the first eval left AI_PROCESSING).

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
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.audit import AuditRecord
from vera_core.db import tenant_session, uuid7
from vera_core.forms.dsl import PromotedFields
from vera_core.integrations.llm import ExtractedField, FakeLLMClient, JudgeVerdict, TranscriptTurn
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import AnswerSource, CallStatus, FormStatus, InsuranceType
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
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
    # The DSL requires every PatientForm promoted column mapped to a
    # system_fields target — point them all at the single leaf (the same
    # shortcut tests/unit/forms/test_conditions.py uses).
    "system_fields": {"in_network": "sections.coverage.in_network"},
    "rep_call_reference_number_field": "sections.coverage.in_network",
    "promoted_fields": dict.fromkeys(PromotedFields.model_fields, "sections.coverage.in_network"),
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
                },
                "notes": {
                    "type": "text",
                    "title": "Notes",
                    "role": "ask",
                    "required": False,
                    "prompt": {"ask": "Any additional notes?"},
                },
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

# Second (optional) collection path — used by the judge+top-up tests.
_NOTES_PATH = "sections.coverage.notes"


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

    async def reload_form(self) -> PatientForm:
        """Re-query the PatientForm so DB-generated columns (enqueued_at) are visible.

        Flushes pending writes, expires the identity-map entry so the next SELECT
        goes to the DB rather than returning the cached in-memory object, then
        fetches the fresh row.  This is the only way to observe server-side
        defaults like `enqueued_at = func.now()` within the same transaction.
        """
        await self.session.flush()
        self.session.expire_all()
        return (
            await self.session.execute(select(PatientForm).where(PatientForm.id == self.form_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_audit() -> _FakeAuditSink:
    return _FakeAuditSink()


@pytest.fixture
def fake_livekit() -> _FakeLiveKit:
    return _FakeLiveKit()


async def _seed_form(database_url: str, *, retry_count: int = 0) -> AsyncGenerator[_SeedCtx]:
    """Shared seed helper: Tenant + schema chain + PatientForm(AI_PROCESSING) + Call.

    `retry_count` seeds the form's initial retry_count (0 for normal fixture,
    max_retries for the _maxed variant).  Uses the superuser engine (bypasses RLS).
    `form_schema.insurance_type` is UNIQUE — uses find-or-create so the fixture is
    safe whether the schema row was seeded in an earlier test or not.  Teardown is
    scoped: only rows THIS call created are removed.
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
                retry_count=retry_count,
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
        await session.flush()
        # Mirror what the callback writes in production: a snapshot row with
        # before_state already populated so step-8 UPDATE matches a real row.
        session.add(
            CallFormSnapshot(
                tenant_id=tenant_id,
                call_id=call_id,
                before_state={},
                after_state={},
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


@pytest.fixture
async def seeded_ai_processing_form(
    database_url: str,
) -> AsyncGenerator[_SeedCtx]:
    """Tenant + schema chain + PatientForm(AI_PROCESSING, retry_count=0) + Call."""
    async for ctx in _seed_form(database_url, retry_count=0):
        yield ctx


@pytest.fixture
async def seeded_ai_processing_form_maxed(
    database_url: str,
) -> AsyncGenerator[_SeedCtx]:
    """Same as seeded_ai_processing_form but retry_count == tenant.max_retries (5).

    Used to verify that an incomplete form with no retries remaining routes to
    EXCEPTION_REVIEW instead of re-queuing.
    """
    async for ctx in _seed_form(database_url, retry_count=5):
        yield ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _observer_answer(
    ctx: _SeedCtx, path: str, value: str, *, confidence: int = 90, evidence_seq: int | None = 1
) -> FieldAnswer:
    """A FieldAnswer as worker_events._handle_call_answer_recorded writes it:
    ai_call source with the live call's id, current."""
    return FieldAnswer(
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        field_path=path,
        value={"value": value},
        source=AnswerSource.AI_CALL.value,
        confidence=confidence,
        evidence_seq=evidence_seq,
        is_current=True,
    )


async def test_verdict_path_mismatch_is_logged_not_silent(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A judge verdict whose field_path matches no judged answer must be
    WARN-logged (paths only — never values), and the matching verdicts must
    still be written. Silent drops made 'judge never ran' and 'judge output
    mismatched' indistinguishable (2026-07-27 E2E)."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "user", "yes in network")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(ctx.collection_path, "in-network", 92, 0)],
        verdicts=[
            JudgeVerdict("sections.coverage.bogus_path", True, 90, "??"),
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    with caplog.at_level("WARNING", logger="vera_core.services.post_call_eval"):
        await evaluate_call(
            ctx.session,
            deps,
            tenant_id=ctx.tenant_id,
            form_id=ctx.form_id,
            call_id=ctx.call_id,
            turns=turns,
        )

    warnings = [r for r in caplog.records if "judge verdict" in r.getMessage()]
    assert len(warnings) == 1
    assert "sections.coverage.bogus_path" in warnings[0].getMessage()

    # The matching verdict was still written.
    answer = (
        await ctx.session.execute(
            select(FieldAnswer).where(
                FieldAnswer.form_id == ctx.form_id,
                FieldAnswer.field_path == ctx.collection_path,
                FieldAnswer.is_current.is_(True),
            )
        )
    ).scalar_one()
    evals = (
        (
            await ctx.session.execute(
                select(FieldEvaluation).where(FieldEvaluation.answer_id == answer.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(evals) == 1


async def test_observer_answers_are_judged_and_missing_paths_topped_up(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Regression for the Observer/eval conflict (2026-07-27 E2E): live ai_call
    answers must NOT no-op the eval. The eval judges them, extracts only the
    still-missing paths, judges those too, and finishes with a real reason."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    await ctx.session.flush()

    turns = [
        TranscriptTurn(0, "agent", "are they in network"),
        TranscriptTurn(1, "user", "yes in network, no notes"),
    ]
    llm = FakeLLMClient(
        extracted=[ExtractedField(_NOTES_PATH, "no notes", 80, 1)],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
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

    # Top-up extraction asked ONLY for the missing path.
    assert llm.extract_calls == [[_NOTES_PATH]]
    # One combined judge pass over observer answer + topped-up answer.
    assert len(llm.judge_calls) == 1
    assert {ef.field_path for ef in llm.judge_calls[0]} == {ctx.collection_path, _NOTES_PATH}

    # Not a no-op: the form transitioned with a real reason (required field
    # satisfied by the observer answer → ready_for_review).
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "ready_for_review"

    # FieldEvaluation rows exist for BOTH answers.
    answers = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == ctx.form_id,
                    FieldAnswer.source == AnswerSource.AI_CALL.value,
                    FieldAnswer.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {a.field_path for a in answers} == {ctx.collection_path, _NOTES_PATH}
    for a in answers:
        evals = (
            (
                await ctx.session.execute(
                    select(FieldEvaluation).where(FieldEvaluation.answer_id == a.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(evals) == 1, f"no FieldEvaluation for {a.field_path}"


async def test_nothing_missing_skips_extraction_and_judges_observer_answers(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """When the Observer answered every collection path, the eval must not
    spend an extract call — judge-only."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    ctx.session.add(_observer_answer(ctx, _NOTES_PATH, "none", evidence_seq=2))
    await ctx.session.flush()

    turns = [TranscriptTurn(0, "user", "yes in network, no notes")]
    llm = FakeLLMClient(
        extracted=[],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
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

    assert llm.extract_calls == []  # no top-up needed
    assert len(llm.judge_calls) == 1
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "ready_for_review"


async def test_duplicate_extract_paths_dedupe_instead_of_poisoning(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """The LLM emitting the same field_path twice must not violate fa_current_uq
    (which would leave the job unacked and reclaim-loop it forever): the last
    occurrence wins and exactly one current answer is written."""
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [
        TranscriptTurn(0, "agent", "are they in network"),
        TranscriptTurn(1, "user", "yes in network"),
    ]
    llm = FakeLLMClient(
        extracted=[
            ExtractedField(path, "out-of-network", 55, 0),
            ExtractedField(path, "in-network", 92, 1),
        ],
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

    assert outcome.answers_written == 1
    rows = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == ctx.form_id,
                    FieldAnswer.field_path == path,
                    FieldAnswer.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].value["value"] == "in-network"  # last occurrence won


async def test_evaluate_call_writes_answers_and_parks_for_review(
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

    # Even with every required field satisfied, the pipeline parks the form in
    # EXCEPTION_REVIEW for human sign-off — it never auto-COMPLETEs.
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
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

    # Step 8: verify after_state was written into the snapshot row.
    snapshot = (
        await ctx.session.execute(
            select(CallFormSnapshot).where(CallFormSnapshot.call_id == ctx.call_id)
        )
    ).scalar_one()
    assert snapshot.after_state == {path: "in-network"}


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

    form = await ctx.reload_form()
    assert form.enqueued_at is None  # review path must not queue the form


async def test_blank_valued_field_is_never_stored(  # VR2-93
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    # This writer bypasses record_answer, so a blank extraction would demote the
    # baseline and leave an empty field flagged as a dispute.
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "user", "I don't have that information")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "  ", 40, 0)],
        verdicts=[],
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

    rows = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(FieldAnswer.source == AnswerSource.AI_CALL.value)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # a blank value must never become the current answer


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


async def test_non_ai_processing_form_is_noop(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Fix A: a form not in AI_PROCESSING must be ACKed cleanly (no raise, no writes)."""
    ctx = seeded_ai_processing_form

    # Force the form into IN_CALL so the job is stale / callback-rollback case.
    # ctx.session is a superuser session (bypasses RLS) — reuse it directly.
    await ctx.session.execute(
        text("UPDATE patient_form SET status = 'in_call' WHERE id = :fid").bindparams(
            fid=ctx.form_id
        )
    )
    await ctx.session.flush()

    # Expire the cached object so the session sees the updated status.
    ctx.session.expire_all()

    turns = [TranscriptTurn(0, "user", "in network")]
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    # Must return without raising (ACKable) and write nothing.
    assert outcome.answers_written == 0
    assert outcome.status == FormStatus.IN_CALL

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
    assert rows == []


async def test_llm_failure_routes_to_exception_review(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Fix B: an LLM extract error must route the form to EXCEPTION_REVIEW without raising."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "user", "in network")]
    llm = FakeLLMClient(
        extracted=[],
        verdicts=[],
        raise_on_extract=RuntimeError("Vertex quota exceeded"),
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

    # Must not raise, must route to review, must write no ai_call answers.
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    assert outcome.answers_written == 0

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
    assert rows == []

    # Form status in DB should be exception_review.
    form_row = (
        await ctx.session.execute(
            text("SELECT status FROM patient_form WHERE id = :fid").bindparams(fid=ctx.form_id)
        )
    ).one()
    assert form_row.status == FormStatus.EXCEPTION_REVIEW.value


async def test_incomplete_with_retries_left_requeues(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A form with a required unfilled field and retries remaining re-queues (IN_QUEUE).

    The LLM extracts nothing — the single required ask field stays empty — so
    retryable_required_paths returns it.  retry_count starts at 0, max_retries=5,
    so the retry branch fires: form → IN_QUEUE, retry_count incremented to 1.

    Note: try_dispatch runs inside the same transaction immediately after the
    IN_QUEUE transition.  Because max_concurrent_calls leaves free slots and no forms are active,
    the fake LiveKit dispatches immediately (IN_QUEUE → IN_CALL within the same
    flush).  We therefore assert on the *outcome* (what _finish returned) rather
    than the post-dispatch DB status, and verify that retry_count was incremented
    (the state machine side effect that proves the IN_QUEUE branch fired).
    """
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "user", "sorry I cannot share that")]
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    # _finish returns IN_QUEUE — the re-queue decision was made.
    assert outcome.status == FormStatus.IN_QUEUE
    # try_dispatch fires immediately inside the same transaction, so by the time
    # we reload, the form may be IN_CALL.  What proves IN_QUEUE fired is
    # retry_count == 1 (incremented by FormStateMachine on AI_PROCESSING → IN_QUEUE).
    form = await ctx.reload_form()
    assert form.retry_count == 1
    # Status is either IN_QUEUE (dispatch blocked) or IN_CALL (dispatch succeeded).
    assert form.status in (FormStatus.IN_QUEUE.value, FormStatus.IN_CALL.value)


async def test_incomplete_retries_exhausted_goes_to_review(
    seeded_ai_processing_form_maxed: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A form with a required unfilled field and no retries remaining routes to EXCEPTION_REVIEW.

    retry_count starts at 5 == tenant.max_retries, so the state machine blocks the
    IN_QUEUE transition.  The decision matrix falls through to EXCEPTION_REVIEW with
    reason="retries_exhausted".
    """
    ctx = seeded_ai_processing_form_maxed
    turns = [TranscriptTurn(0, "user", "no")]
    llm = FakeLLMClient(extracted=[], verdicts=[])
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
    form = await ctx.reload_form()
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.enqueued_at is None  # review path must not queue the form
    assert form.review_reason == "retries_exhausted"


async def test_incomplete_retryable_with_auto_retry_disabled_goes_to_review(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Retryable required field + retries remaining, but the tenant auto-retry
    flag is off (EvalDeps default) → no requeue; EXCEPTION_REVIEW with the
    honest reason auto_retry_disabled."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "agent", "are they in network")]
    # LLM extracts nothing → the required ask field stays unsatisfied (retryable).
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)  # flag defaults off

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "auto_retry_disabled"
    assert form.retry_count == 0  # the IN_QUEUE branch never fired


async def test_incomplete_with_retries_left_requeues_clears_review_reason(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Non-review outcome (IN_QUEUE) always clears review_reason."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "user", "sorry I cannot share that")]
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert outcome.status == FormStatus.IN_QUEUE
    form = await ctx.reload_form()
    assert form.review_reason is None  # non-review outcome always clears it


async def test_user_canceled_call_never_requeues(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A supervisor-ended (CANCELED) call is routed to human review, never
    auto-redialed — even with an unsatisfied required field and retries remaining.
    Mirrors resolve_ai_processing's user_ended gate, which the eval path bypasses."""
    ctx = seeded_ai_processing_form
    # The seed fixture creates the call COMPLETED; mark it user-canceled.
    await ctx.session.execute(
        update(Call).where(Call.id == ctx.call_id).values(current_status=CallStatus.CANCELED.value)
    )
    await ctx.session.flush()

    # LLM extracts nothing → the required ask field stays unsatisfied (retryable),
    # and the seeded tenant's max_retries (5) leaves retries available — so a
    # normally-ended call would route to IN_QUEUE here.
    turns = [TranscriptTurn(0, "user", "sorry, we got cut off")]
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    # Never IN_QUEUE for a user-canceled call — it parks for a human instead.
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.review_reason == "user_ended"
    assert form.enqueued_at is None  # the review path must not queue the form


async def test_stale_job_for_older_call_is_skipped(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A redelivered job for a call that is no longer the form's latest attempt
    must no-op: evaluating the older call's transcript would demote the newer
    attempt's answers and decide the form on stale data (crash-between-commit-
    and-XACK redelivery during a later attempt's AI_PROCESSING window)."""
    ctx = seeded_ai_processing_form
    ctx.session.add(
        Call(
            id=uuid7(),
            tenant_id=ctx.tenant_id,
            form_id=ctx.form_id,
            current_status=CallStatus.COMPLETED.value,
            mode="full",
        )
    )
    await ctx.session.flush()

    llm = FakeLLMClient(
        extracted=[ExtractedField(ctx.collection_path, "in-network", 92, 1)],
        verdicts=[JudgeVerdict(ctx.collection_path, True, 88, "yes in network")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=[TranscriptTurn(0, "user", "yes in network")],
    )

    assert outcome.status == FormStatus.AI_PROCESSING  # untouched — newer job owns it
    assert outcome.answers_written == 0
    assert llm.extract_calls == []  # the eval never ran
    assert llm.judge_calls == []


async def test_extract_failure_still_judges_observer_answers(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A top-up extraction blip must not forfeit judge verdicts for the
    Observer's already-captured answers — the judge pass runs regardless, then
    the form routes to LLM_ERROR review."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    await ctx.session.flush()

    llm = FakeLLMClient(
        extracted=[],
        verdicts=[JudgeVerdict(ctx.collection_path, True, 88, "yes in network")],
        raise_on_extract=RuntimeError("vertex blip"),
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=[TranscriptTurn(0, "user", "yes in network")],
    )

    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "llm_error"
    assert len(llm.judge_calls) == 1  # observer answer was still judged

    answer = (
        await ctx.session.execute(
            select(FieldAnswer).where(
                FieldAnswer.form_id == ctx.form_id,
                FieldAnswer.field_path == ctx.collection_path,
                FieldAnswer.is_current.is_(True),
            )
        )
    ).scalar_one()
    evals = (
        (
            await ctx.session.execute(
                select(FieldEvaluation).where(FieldEvaluation.answer_id == answer.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(evals) == 1


async def test_hallucinated_token_path_outside_request_does_not_quarantine(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """A token-shaped value the LLM emits for a path it was NOT asked about is
    dropped entirely — it must not route the form to TOKEN_VALUE review for a
    field that was never written."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    await ctx.session.flush()

    llm = FakeLLMClient(
        extracted=[
            ExtractedField(ctx.collection_path, "[[MEMBER_ID_1]]", 99, 0),  # not requested
            ExtractedField(_NOTES_PATH, "no notes", 80, 1),
        ],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=[TranscriptTurn(0, "user", "yes in network, no notes")],
    )

    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "ready_for_review"  # NOT token_value

    # The observer's real value survived untouched.
    answer = (
        await ctx.session.execute(
            select(FieldAnswer).where(
                FieldAnswer.form_id == ctx.form_id,
                FieldAnswer.field_path == ctx.collection_path,
                FieldAnswer.is_current.is_(True),
            )
        )
    ).scalar_one()
    assert answer.value["value"] == "in-network"


async def test_observer_answer_without_evidence_anchor_is_judged_with_none(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """An Observer answer recorded without an evidence turn (evidence_seq NULL)
    must reach the judge with no anchor — not a fabricated turn 0."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network", evidence_seq=None))
    ctx.session.add(_observer_answer(ctx, _NOTES_PATH, "none", evidence_seq=2))
    await ctx.session.flush()

    llm = FakeLLMClient(
        extracted=[],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=[TranscriptTurn(0, "agent", "hello"), TranscriptTurn(1, "user", "yes")],
    )

    assert len(llm.judge_calls) == 1
    by_path = {ef.field_path: ef for ef in llm.judge_calls[0]}
    assert by_path[ctx.collection_path].evidence_seq is None
    assert by_path[_NOTES_PATH].evidence_seq == 2
