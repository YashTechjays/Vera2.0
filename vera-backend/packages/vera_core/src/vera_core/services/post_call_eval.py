"""Post-call re-read: extract collected fields from the (de-identified) transcript,
persist them, judge each, and decide the form's terminal status. Pure helpers here;
the DB orchestration lives in `evaluate_call` below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from phi_codec.tokens.token import TOKEN_RE
from vera_core.audit import AuditRecord, AuditSink
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import (
    REVIEW_CONFIDENCE_FLOOR,
    form_completion_pct,
    retryable_required_paths,
)
from vera_core.integrations.llm import ExtractedField, LLMClient, TranscriptTurn
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.authoring import SchemaVersion
from vera_core.models.enums import AnswerSource, FormStatus
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant
from vera_core.services.field_status import load_current_values, load_field_status
from vera_core.services.form_state_machine import FormStateMachine
from vera_core.services.queue_dispatcher import try_dispatch

logger = logging.getLogger("vera.post_call_eval")


def has_phi_token(value: str) -> bool:
    """True if the extracted value still contains a `[[TYPE_N]]` PHI token — meaning the
    LLM surfaced an identifier we cannot safely materialize (no live vault). Such fields
    are routed to review rather than stored as a token."""
    return TOKEN_RE.search(value) is not None


def evidence_text(turns: list[TranscriptTurn], evidence_seq: int) -> str | None:
    if 0 <= evidence_seq < len(turns):
        return turns[evidence_seq].text
    return None


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class EvalDeps:
    llm: LLMClient
    audit: AuditSink
    livekit: Any
    floor: int = REVIEW_CONFIDENCE_FLOOR


@dataclass
class EvalOutcome:
    status: FormStatus
    answers_written: int
    reviewed_fields: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _demote_current(session: AsyncSession, form_id: UUID, field_paths: list[str]) -> None:
    """Demote any existing current answers for (form, paths) — the merge invariant."""
    if not field_paths:
        return
    await session.execute(
        update(FieldAnswer)
        .where(
            FieldAnswer.form_id == form_id,
            FieldAnswer.field_path.in_(field_paths),
            FieldAnswer.is_current.is_(True),
        )
        .values(is_current=False)
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def evaluate_call(
    session: AsyncSession,
    deps: EvalDeps,
    *,
    tenant_id: UUID,
    form_id: UUID,
    call_id: UUID,
    turns: list[TranscriptTurn],
) -> EvalOutcome:
    """Extract, persist, judge, update status, and dispatch.

    Runs inside a caller-provided tenant-scoped session. Idempotent on redelivery.
    """
    form: PatientForm = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one()

    # (0) Form-state guard — if the form is not AI_PROCESSING the callback transaction
    # rolled back after emitting the job, or the job is stale.  Transitioning out of any
    # other state (e.g. IN_CALL → EXCEPTION_REVIEW) is illegal and would loop forever via
    # XAUTOCLAIM.  ACK cleanly with a no-op outcome instead.
    if FormStatus(form.status) != FormStatus.AI_PROCESSING:
        logger.warning(
            "post_call_eval: form %s is in status %r, not AI_PROCESSING — skipping (stale job)",
            form_id,
            form.status,
        )
        return EvalOutcome(status=FormStatus(form.status), answers_written=0)

    # (1) Idempotency guard — return early if this call was already processed.
    already = (
        await session.execute(
            select(FieldAnswer.id)
            .where(
                FieldAnswer.call_id == call_id,
                FieldAnswer.source == AnswerSource.AI_CALL.value,
            )
            .limit(1)
        )
    ).first()
    if already is not None:
        return EvalOutcome(status=FormStatus(form.status), answers_written=0)

    tenant: Tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    version: SchemaVersion = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    prev_status = form.status
    sm = FormStateMachine()

    async def _finish(
        target: FormStatus,
        *,
        written: int,
        reviewed: list[str],
        reason: str | None = None,
    ) -> EvalOutcome:
        sm.transition(form, target, tenant_max_retries=tenant.max_retries)
        if target == FormStatus.IN_QUEUE:
            form.enqueued_at = func.now()
        await session.flush()
        detail: dict[str, object] = {
            "from": prev_status,
            "to": form.status,
            "call_id": str(call_id),
            "reviewed": len(reviewed),
            "answers": written,
            "trigger": "post_call_eval",
            **({"reason": reason} if reason is not None else {}),
        }
        await deps.audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.SERVICE,
                actor_label="post-call-eval",
                event_type=AuditEvent.FORM_STATUS_CHANGE.value,
                resource_type="patient_form",
                resource_id=str(form_id),
                detail=detail,
            )
        )
        await try_dispatch(
            session, tenant_id, deps.livekit, audit=deps.audit, retry_floor=deps.floor
        )
        return EvalOutcome(status=target, answers_written=written, reviewed_fields=reviewed)

    # (3) No transcript → route to EXCEPTION_REVIEW.
    if not turns:
        return await _finish(
            FormStatus.EXCEPTION_REVIEW, written=0, reviewed=[], reason="no_transcript"
        )

    # (2) Parse schema — collection paths for extraction.
    doc = FormSchemaDoc.model_validate(version.schema_json)
    paths = doc.collection_paths()

    # (4-5) Extract + persist (skip token-valued fields). Keep each written row so
    # its ID is in hand for the judge pass — the PK is client-minted (uuid7), so
    # `.id` is populated at construction and needs no re-query after flush.
    try:
        extracted = await deps.llm.extract(field_paths=paths, turns=turns)
    except Exception as exc:
        logger.error(
            "post_call_eval: LLM extract failed for form %s — routing to EXCEPTION_REVIEW (%s: %s)",
            form_id,
            type(exc).__name__,
            exc,
        )
        return await _finish(
            FormStatus.EXCEPTION_REVIEW, written=0, reviewed=[], reason="llm_error"
        )
    token_fields = [ef.field_path for ef in extracted if has_phi_token(ef.value)]
    clean = [ef for ef in extracted if not has_phi_token(ef.value)]
    # Demote the outgoing current rows in one statement BEFORE adding their
    # replacements, so the merge invariant (one current row per path) holds at flush.
    await _demote_current(session, form_id, [ef.field_path for ef in clean])
    kept: list[tuple[ExtractedField, FieldAnswer]] = []
    for ef in clean:
        answer = FieldAnswer(
            tenant_id=tenant_id,
            form_id=form_id,
            call_id=call_id,
            field_path=ef.field_path,
            value={"value": ef.value},
            source=AnswerSource.AI_CALL.value,
            confidence=ef.confidence,
            evidence_seq=ef.evidence_seq,
            evidence=evidence_text(turns, ef.evidence_seq),
            is_current=True,
        )
        session.add(answer)
        kept.append((ef, answer))
    await session.flush()

    # (6) Judge + write FieldEvaluation. The verdicts feed the satisfaction check
    # below via load_field_status — nothing is decided per-field here.
    try:
        raw_verdicts = await deps.llm.judge(extracted=[ef for ef, _ in kept], turns=turns)
    except Exception as exc:
        logger.error(
            "post_call_eval: LLM judge failed for form %s — routing to EXCEPTION_REVIEW (%s: %s)",
            form_id,
            type(exc).__name__,
            exc,
        )
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=[ef.field_path for ef, _ in kept],
            reason="llm_error",
        )
    verdicts = {v.field_path: v for v in raw_verdicts}
    for ef, answer in kept:
        v = verdicts.get(ef.field_path)
        if v is not None:
            session.add(
                FieldEvaluation(
                    tenant_id=tenant_id,
                    answer_id=answer.id,
                    confidence=v.confidence,
                    evidence=v.evidence,
                    supported=v.supported,
                )
            )
    await session.flush()

    # (7) Recompute completion % from the form's current answers.
    current_values = await load_current_values(session, form_id)
    form.completion_pct = form_completion_pct(current_values, version.schema_json)

    # (8) Update call_form_snapshot.after_state (the before_state row was written
    #     by the callback; here we fill in after_state).
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(CallFormSnapshot)
            .where(CallFormSnapshot.call_id == call_id)
            .values(after_state=current_values)
        ),
    )
    if result.rowcount == 0:
        logger.warning(
            "post_call_eval: no call_form_snapshot row for call_id=%s — after_state not written",
            call_id,
        )

    # (9-12) Decide status, transition, audit, dispatch.
    if token_fields:
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=token_fields,
            reason="token_value",
        )
    status_by_path = await load_field_status(session, form_id)
    retryable = retryable_required_paths(status_by_path, version.schema_json, floor=deps.floor)
    if not retryable:
        return await _finish(FormStatus.COMPLETED, written=len(kept), reviewed=[])
    if sm.can_retry(form, tenant_max_retries=tenant.max_retries):
        return await _finish(FormStatus.IN_QUEUE, written=len(kept), reviewed=[], reason="retry")
    return await _finish(
        FormStatus.EXCEPTION_REVIEW,
        written=len(kept),
        reviewed=retryable,
        reason="retries_exhausted",
    )
