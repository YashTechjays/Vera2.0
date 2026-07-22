"""Read-side helpers for call attempt history and per-field provenance.

Loads which call wrote each current AI answer, the judge's latest verdict, the
form's attempt timeline (lineage + snapshot diffs). PHI discipline: snapshot
values are read to compute diffs but only field *paths* leave this module;
judge `evidence` is de-identified (tokenized before the LLM saw it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models.call import Call, CallLineage
from vera_core.models.enums import AnswerSource, RecordingStatus
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.transcript import Recording
from vera_core.services.field_status import latest_eval_subquery

_MISSING = object()


@dataclass(frozen=True)
class JudgeInfo:
    confidence: int | None
    supported: bool
    evidence: str | None


@dataclass(frozen=True)
class FieldProvenance:
    attempt: int
    mode: str
    judge: JudgeInfo | None


@dataclass(frozen=True)
class CallAttempt:
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]
    # Visibility inputs + playability for the caller-aware `recording` DTO field
    # (vera_core stays caller-agnostic; the API layer applies call_hidden_from).
    initiated_by_id: UUID | None = None
    published: bool = False
    recording_available: bool = False


def snapshot_changed_paths(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[str]:
    """Field paths whose value differs between a call's before/after snapshots.
    Paths only — values never leave. Tolerates None/partial snapshots."""
    b = before or {}
    a = after or {}
    return sorted(p for p in set(b) | set(a) if b.get(p, _MISSING) != a.get(p, _MISSING))


async def load_call_attempts(session: AsyncSession, form_id: UUID) -> list[CallAttempt]:
    """The form's calls as a 1-based attempt timeline, oldest first
    (created_at, then id — UUIDv7 — as the deterministic tie-break)."""
    calls = (
        await session.execute(
            select(
                Call.id,
                Call.mode,
                Call.current_status,
                Call.created_at,
                Call.initiated_by_id,
                Call.published,
            )
            .where(Call.form_id == form_id)
            .order_by(Call.created_at.asc(), Call.id.asc())
        )
    ).all()
    if not calls:
        return []
    ids = [c.id for c in calls]
    retry_of = {
        row.retry_call_id: row.parent_call_id
        for row in (
            await session.execute(
                select(CallLineage.retry_call_id, CallLineage.parent_call_id).where(
                    CallLineage.retry_call_id.in_(ids)
                )
            )
        ).all()
    }
    snapshots = {
        row.call_id: (row.before_state, row.after_state)
        for row in (
            await session.execute(
                select(
                    CallFormSnapshot.call_id,
                    CallFormSnapshot.before_state,
                    CallFormSnapshot.after_state,
                ).where(CallFormSnapshot.call_id.in_(ids))
            )
        ).all()
    }
    playable = {
        row.call_id
        for row in (
            await session.execute(
                select(Recording.call_id).where(
                    Recording.call_id.in_(ids),
                    Recording.status == RecordingStatus.AVAILABLE.value,
                )
            )
        ).all()
    }
    out: list[CallAttempt] = []
    for attempt, c in enumerate(calls, start=1):
        before, after = snapshots.get(c.id, (None, None))
        out.append(
            CallAttempt(
                id=c.id,
                attempt=attempt,
                mode=c.mode,
                status=c.current_status,
                created_at=c.created_at,
                retry_of=retry_of.get(c.id),
                changed_paths=snapshot_changed_paths(before, after),
                initiated_by_id=c.initiated_by_id,
                published=c.published,
                recording_available=c.id in playable,
            )
        )
    return out


async def load_field_provenance(
    session: AsyncSession, form_id: UUID, attempt_by_call: Mapping[UUID, tuple[int, str]]
) -> dict[str, FieldProvenance]:
    """Per-path provenance for the form's current ai_call answers: which attempt
    wrote it (via *attempt_by_call*, from load_call_attempts) + the latest judge
    verdict (latest_eval_subquery, shared with load_field_status)."""
    latest_eval = latest_eval_subquery()
    rows = (
        await session.execute(
            select(
                FieldAnswer.field_path,
                FieldAnswer.call_id,
                FieldEvaluation.confidence,
                FieldEvaluation.supported,
                FieldEvaluation.evidence,
            )
            .outerjoin(latest_eval, latest_eval.c.answer_id == FieldAnswer.id)
            .outerjoin(
                FieldEvaluation,
                (FieldEvaluation.answer_id == FieldAnswer.id)
                & (FieldEvaluation.created_at == latest_eval.c.max_created_at),
            )
            .where(
                FieldAnswer.form_id == form_id,
                FieldAnswer.is_current.is_(True),
                FieldAnswer.source == AnswerSource.AI_CALL.value,
                FieldAnswer.call_id.is_not(None),
            )
        )
    ).all()
    out: dict[str, FieldProvenance] = {}
    for path, call_id, confidence, supported, evidence in rows:
        am = attempt_by_call.get(call_id)
        if am is None:  # answer's call not in the timeline (shouldn't happen) — skip
            continue
        judge = (
            JudgeInfo(confidence=confidence, supported=supported, evidence=evidence)
            if supported is not None
            else None
        )
        out[path] = FieldProvenance(attempt=am[0], mode=am[1], judge=judge)
    return out
