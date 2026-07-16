"""Readers over a form's current field answers.

`load_field_status` feeds the retry decision and is PHI-free (no value columns).
it is the single "current values of a form" query for snapshots and completion %.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.review import FieldStatus
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation


def latest_eval_subquery() -> Any:
    """Subquery: latest created_at per answer_id across all evaluations — the one
    encoding of "the latest FieldEvaluation for an answer" (multiple evals from LLM
    retries must never fan out into duplicate rows). Concurrent evals within the
    same database clock tick choose non-deterministically."""
    return (
        select(
            FieldEvaluation.answer_id,
            func.max(FieldEvaluation.created_at).label("max_created_at"),
        )
        .group_by(FieldEvaluation.answer_id)
        .subquery()
    )


async def load_field_status(session: AsyncSession, form_id: UUID) -> dict[str, FieldStatus]:
    """Return one FieldStatus per current field answer for *form_id*.

    Selects only field_path, source, confidences, and the latest eval's supported
    flag — no value or evidence columns, so this query is PHI-free. An answer with
    no evaluation yields ai_supported=None; ai_confidence is the judge's score
    when an evaluation exists (the satisfaction gate's input), else the extractor's.

    The latest evaluation per answer is resolved in-DB via latest_eval_subquery().
    """
    latest_eval = latest_eval_subquery()
    rows = (
        await session.execute(
            select(
                FieldAnswer.field_path,
                FieldAnswer.source,
                FieldAnswer.confidence,
                FieldEvaluation.supported,
                FieldEvaluation.confidence.label("eval_confidence"),
            )
            .outerjoin(
                latest_eval,
                latest_eval.c.answer_id == FieldAnswer.id,
            )
            .outerjoin(
                FieldEvaluation,
                (FieldEvaluation.answer_id == FieldAnswer.id)
                & (FieldEvaluation.created_at == latest_eval.c.max_created_at),
            )
            .where(FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True))
        )
    ).all()
    return {
        # The satisfaction floor gates on the JUDGE's confidence (design: "judge-
        # supported, confidence >= floor"); the extractor's self-reported score is
        # only the fallback for an answer that has no evaluation yet — where
        # ai_supported=None already fails the gate.
        path: FieldStatus(
            source=source,
            ai_supported=supported,
            ai_confidence=eval_confidence if eval_confidence is not None else confidence,
        )
        for path, source, confidence, supported, eval_confidence in rows
    }
