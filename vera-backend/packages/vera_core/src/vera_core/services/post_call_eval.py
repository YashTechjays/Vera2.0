"""Post-call eval: judge the Observer's live ai_call answers for the finished
call, extract only the still-missing collection paths from the transcript
(top-up), judge those too, and decide the form's terminal status. Pure helpers
here; Redelivery safety comes from the status guard + single-transaction
atomicity (a committed eval already left AI_PROCESSING; a rolled-back one left
no partial state) — there is deliberately no answer-existence guard: the
Observer writes ai_call answers DURING the call, so their presence proves
nothing about whether the eval ran.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.audit import AuditRecord, AuditSink
from vera_core.forms.answers import canonical_answer, leaf_literals, spoken_literals
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import (
    REVIEW_CONFIDENCE_FLOOR,
    form_completion_pct,
    is_blank_answer,
    retryable_required_paths,
    satisfied_required_fraction,
    unsatisfied_required_paths,
    unwrap_value,
)
from vera_core.integrations.llm import ExtractedField, LLMClient, TranscriptTurn
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.authoring import SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import AnswerSource, CallStatus, FormStatus, ReviewReason
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant
from vera_core.services.field_status import load_field_status
from vera_core.services.form_state_machine import FormStateMachine
from vera_core.services.queue_dispatcher import try_dispatch

logger = logging.getLogger("vera_core.services.post_call_eval")

# Legacy de-identification token shape ("[[SSN_1]]"). The tokenization wall was
# removed 2026-07-13 (phi_codec deleted; transcripts are plaintext in-boundary),
# but a token-shaped extraction still means "not a real value" — quarantine it
# to review rather than persisting it as an answer.
PHI_TOKEN_RE = re.compile(r"\[\[([A-Z][A-Z_]*)_(\d+)\]\]")


def has_phi_token(value: str) -> bool:
    """True if the extracted value still contains a `[[TYPE_N]]` PHI token — meaning the
    LLM surfaced an identifier we cannot safely materialize (no live vault). Such fields
    are routed to review rather than stored as a token."""
    return PHI_TOKEN_RE.search(value) is not None


def evidence_text(turns: list[TranscriptTurn], evidence_seq: int | None) -> str | None:
    if evidence_seq is not None and 0 <= evidence_seq < len(turns):
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
    kms: Any = None
    recording: Any = None
    plan_service: Any = None
    floor: int = REVIEW_CONFIDENCE_FLOOR
    # Mirrors settings.form_auto_retry_enabled — the DEPLOYMENT-WIDE kill-switch;
    # ANDed with tenant.auto_retry_enabled at the decision site, so either one
    # off means the eval never auto-redials a payer (same gate the fallback
    # resolver applies). Default False: safe when a caller forgets to wire it.
    auto_retry_enabled: bool = False


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

    tenant: Tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    version: SchemaVersion = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    # A user-ended (CANCELED) call must never be auto-redialed — the supervisor who
    # ended it does not want the payer called back. resolve_ai_processing enforces this
    # on the non-eval path; the eval pipeline bypasses that gate, so re-check it here.
    call: Call | None = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()
    user_ended = call is not None and (
        call.current_status == CallStatus.CANCELED.value or call.end_requested_by_id is not None
    )
    # (0b) Stale-call guard — only the form's LATEST attempt may be evaluated.
    # A job can be redelivered after its transaction committed (crash between
    # commit and XACK); if the form has since re-entered AI_PROCESSING for a
    # newer call, the status guard alone would let the stale job demote the new
    # attempt's answers and decide the form on the old transcript. The newer
    # attempt's own job owns the resolution; ACK this one as a no-op.
    if call is not None:
        # created_at is transaction-start time and can tie across attempts, so
        # break ties on the uuid7 PK — same discipline as the other latest-call
        # queries (calls.py, call_provenance.py, worker_events.py).
        newer = (
            await session.execute(
                select(Call.id)
                .where(
                    Call.form_id == form_id,
                    tuple_(Call.created_at, Call.id) > (call.created_at, call.id),
                )
                .limit(1)
            )
        ).first()
        if newer is not None:
            logger.warning(
                "post_call_eval: call %s is not form %s's latest attempt — skipping (stale job)",
                call_id,
                form_id,
            )
            return EvalOutcome(status=FormStatus(form.status), answers_written=0)
    prev_status = form.status
    sm = FormStateMachine()

    async def _finish(
        target: FormStatus,
        *,
        written: int,
        reviewed: list[str],
        reason: str | None = None,
    ) -> EvalOutcome:
        sm.transition(form, target, tenant_max_retries=tenant.max_retries, reason=reason)
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
            session,
            tenant_id,
            deps.livekit,
            deps.kms,
            audit=deps.audit,
            recording=deps.recording,
            plan_service=deps.plan_service,
            retry_floor=deps.floor,
        )
        return EvalOutcome(status=target, answers_written=written, reviewed_fields=reviewed)

    # (3) No transcript → route to EXCEPTION_REVIEW.
    if not turns:
        return await _finish(
            FormStatus.EXCEPTION_REVIEW, written=0, reviewed=[], reason=ReviewReason.NO_TRANSCRIPT
        )

    # (2) Parse schema — collection paths for extraction. A document the model
    # can't parse (e.g. a legacy v1 schema — FormSchemaDoc only accepts 2.1)
    # must route to review, not raise: an exception here leaves the job unacked
    # and reclaim would re-run it forever.
    try:
        doc = FormSchemaDoc.model_validate(version.schema_json)
    except Exception as exc:
        logger.error(
            "post_call_eval: unsupported schema for form %s — routing to EXCEPTION_REVIEW (%s: %s)",
            form_id,
            type(exc).__name__,
            exc,
        )
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=0,
            reviewed=[],
            reason=ReviewReason.UNSUPPORTED_SCHEMA,
        )
    paths = doc.collection_paths()

    # (4a) The form's current answers, fetched once — used both to isolate the
    # Observer's live answers for THIS call (worker_events persists ai_call
    # answers during the call since 4f0b8a9; they ARE the extraction for
    # whatever the call covered, so the eval judges them instead of
    # re-extracting) and to compute what is still missing (4b), avoiding a
    # second read of the same rows.
    current_rows = (
        (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    observer_pairs: list[tuple[ExtractedField, FieldAnswer]] = [
        (
            ExtractedField(
                field_path=row.field_path,
                value=str(unwrap_value(row.value)),
                confidence=row.confidence or 0,
                # None stays None — fabricating turn 0 would point the judge at
                # the call opener as the supposed evidence for this answer.
                evidence_seq=row.evidence_seq,
            ),
            row,
        )
        for row in current_rows
        if row.call_id == call_id and row.source == AnswerSource.AI_CALL.value
    ]

    # (4b) Top-up extraction: only paths with no current answer AT ALL — an
    # intake / human / prior-attempt answer is not missing, and the LLM must
    # never supersede one (extracted paths are filtered back to this set).
    answered = {row.field_path for row in current_rows}
    missing = [p for p in paths if p not in answered]
    extracted: list[ExtractedField] = []
    extract_failed = False
    literals = leaf_literals(doc)
    if missing:
        try:
            extracted = await deps.llm.extract(
                field_paths=missing, turns=turns, special_values=spoken_literals(literals)
            )
        except Exception as exc:
            # Do NOT return yet: the Observer's answers below still deserve their
            # judge pass — forfeiting it would reproduce the "answers but no
            # verdicts" stranding this module exists to prevent.
            logger.error(
                "post_call_eval: LLM extract failed for form %s — routing to "
                "EXCEPTION_REVIEW after judging observer answers (%s: %s)",
                form_id,
                type(exc).__name__,
                exc,
            )
            extract_failed = True
    # Keep only what was asked for: a hallucinated path must not supersede an
    # intake/human answer, and must not trip the token quarantine below for a
    # field that is never written (top-up semantics).
    requested = set(missing)
    extracted = [ef for ef in extracted if ef.field_path in requested]
    token_fields = [ef.field_path for ef in extracted if has_phi_token(ef.value)]
    # blank answers never demote a baseline (VR2-93) — this writer bypasses
    # record_answer, so it needs the same guard
    clean = [
        ef for ef in extracted if not has_phi_token(ef.value) and not is_blank_answer(ef.value)
    ]
    # The LLM may emit the same field_path twice; keep the last occurrence. Two
    # inserts for one path would violate the fa_current_uq partial unique index
    # (the batch demote runs before the inserts) and poison-loop the job.
    clean = list({ef.field_path: ef for ef in clean}.values())
    # Gates compare byte-exact, so an un-snapped "unlimited" leaves its gated follow-ups
    # unsatisfied and can redial the payer below (`canonical_answer`).
    clean = [
        replace(ef, value=canonical_answer(ef.value, literals.get(ef.field_path))) for ef in clean
    ]
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

    # (6) One judge pass over the Observer's answers + the topped-up ones. The
    # verdicts feed the satisfaction check below via load_field_status —
    # nothing is decided per-field here.
    to_judge: list[tuple[ExtractedField, FieldAnswer]] = observer_pairs + kept
    if to_judge:
        try:
            raw_verdicts = await deps.llm.judge(extracted=[ef for ef, _ in to_judge], turns=turns)
        except Exception as exc:
            logger.error(
                "post_call_eval: LLM judge failed for form %s — routing to "
                "EXCEPTION_REVIEW (%s: %s)",
                form_id,
                type(exc).__name__,
                exc,
            )
            return await _finish(
                FormStatus.EXCEPTION_REVIEW,
                written=len(kept),
                reviewed=[ef.field_path for ef, _ in to_judge],
                reason=ReviewReason.LLM_ERROR,
            )
        verdicts = {v.field_path: v for v in raw_verdicts}
        if len(verdicts) != len(raw_verdicts):
            # Same LLM quirk the extract side dedupes: duplicate verdicts for
            # one path collapse last-wins here — surface it, paths only.
            dupes = sorted(
                {
                    v.field_path
                    for v in raw_verdicts
                    if sum(1 for w in raw_verdicts if w.field_path == v.field_path) > 1
                }
            )
            logger.warning(
                "post_call_eval: judge returned duplicate verdicts for form %s — "
                "last occurrence wins (%s)",
                form_id,
                dupes,
            )
        judged_paths = {ef.field_path for ef, _ in to_judge}
        unmatched_verdicts = sorted(set(verdicts) - judged_paths)
        unjudged_answers = sorted(judged_paths - set(verdicts))
        if unmatched_verdicts or unjudged_answers:
            # Paths only — schema constants, never answer values (PHI rule).
            logger.warning(
                "post_call_eval: judge verdict/path mismatch for form %s — "
                "%d verdict(s) match no judged answer (%s); "
                "%d judged answer(s) got no verdict (%s)",
                form_id,
                len(unmatched_verdicts),
                unmatched_verdicts,
                len(unjudged_answers),
                unjudged_answers,
            )
        for ef, answer in to_judge:
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

    # (6b) A failed top-up extraction routes to review only AFTER the observer
    # answers were judged above — their verdicts are persisted either way.
    if extract_failed:
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=[ef.field_path for ef, _ in to_judge],
            reason=ReviewReason.LLM_ERROR,
        )

    # (7) Recompute completion % from the form's current answers — derived
    # in-memory: the only current-row changes since the (4a) fetch are the
    # top-up inserts in `kept` (their paths come from `missing`, so they are
    # disjoint from `current_rows` and the batch demote touched no fetched row).
    # Snapped here too, not just on the rows written above: an intake, human or
    # older-release answer reaches this map unsnapped, and every gate decision below —
    # completion, the verified fraction, unsatisfied/retryable, after_state — reads it.
    current_values: dict[str, object] = {
        row.field_path: canonical_answer(unwrap_value(row.value), literals.get(row.field_path))
        for row in current_rows
    }
    current_values.update({ef.field_path: ef.value for ef, _ in kept})
    form.completion_pct = form_completion_pct(current_values, version.schema_json)

    # verified_pct must always mirror completion_pct — compute it here, before the
    # token_fields early return below, so a token-flagged form never persists a stale
    # verified_pct beside a fresh completion_pct.
    status_by_path = await load_field_status(session, form_id)
    verified_fraction = satisfied_required_fraction(
        status_by_path, version.schema_json, floor=deps.floor, values=current_values
    )
    form.verified_pct = round(verified_fraction * 100, 2)

    # (8) Update call_form_snapshot.after_state (the before_state row was written
    #     at dispatch — or backfilled by the closeout callback for legacy calls).
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
            reason=ReviewReason.TOKEN_VALUE,
        )
    # The authoritative decision evaluates gates against the REAL current values
    # (in-session, never logged) — the dispatcher's PHI-free sentinel
    # approximation is only for the retry-nudge labels. COMPLETED requires every
    # required field of EVERY role satisfied. The pipeline NEVER auto-COMPLETEs:
    # even an all-satisfied form parks in EXCEPTION_REVIEW for a human to sign off
    # (COMPLETED is a human-only transition out of review, once disputes clear).
    unsatisfied = unsatisfied_required_paths(
        status_by_path, version.schema_json, floor=deps.floor, values=current_values
    )
    if not unsatisfied:
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=[],
            reason=ReviewReason.READY_FOR_REVIEW,
        )
    # Good-enough gate: the call verified the tenant's threshold of the applicable-
    # required fields, so park for review instead of redialing for the tail. Only
    # suppresses a retry — the guards below decide the sub-threshold case.
    if verified_fraction >= float(tenant.retry_fill_threshold):
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=unsatisfied,
            reason=ReviewReason.FILL_THRESHOLD_MET,
        )
    retryable = retryable_required_paths(
        status_by_path, version.schema_json, floor=deps.floor, values=current_values
    )
    if retryable and sm.can_retry(form, tenant_max_retries=tenant.max_retries):
        # Never auto-redial a call a supervisor deliberately ended: route to human
        # review instead of re-queueing (see user_ended above).
        if user_ended:
            return await _finish(
                FormStatus.EXCEPTION_REVIEW,
                written=len(kept),
                reviewed=unsatisfied,
                reason=ReviewReason.USER_ENDED,
            )
        if tenant.allows_auto_retry(deps.auto_retry_enabled):
            return await _finish(
                FormStatus.IN_QUEUE, written=len(kept), reviewed=[], reason="retry"
            )
        # Covers either gate being off: the deployment kill-switch, the tenant's
        # own auto_retry_enabled, or both.
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=unsatisfied,
            reason=ReviewReason.AUTO_RETRY_DISABLED,
        )
    return await _finish(
        FormStatus.EXCEPTION_REVIEW,
        written=len(kept),
        reviewed=unsatisfied,
        reason=(
            ReviewReason.RETRIES_EXHAUSTED if retryable else ReviewReason.UNSATISFIED_UNASKABLE
        ),
    )
