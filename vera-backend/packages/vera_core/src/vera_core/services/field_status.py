"""Load per-field satisfaction inputs for the retry decision — PHI-free (no values)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.review import FieldStatus
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation


async def load_field_status(session: AsyncSession, form_id: UUID) -> dict[str, FieldStatus]:
    """Return one FieldStatus per current field answer for *form_id*.

    Selects only field_path, source, confidence, and the latest eval's supported
    flag — no value or evidence columns, so this query is PHI-free. An answer with
    no evaluation yields ai_supported=None.

    The latest evaluation per answer is resolved in-DB via a subquery on
    MAX(created_at) so that multiple evaluations (e.g. LLM retries) never produce
    duplicate rows for the same field path.
    """
    # Subquery: latest created_at per answer_id across all evaluations.
    latest_eval = (
        select(
            FieldEvaluation.answer_id,
            func.max(FieldEvaluation.created_at).label("max_created_at"),
        )
        .group_by(FieldEvaluation.answer_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                FieldAnswer.field_path,
                FieldAnswer.source,
                FieldAnswer.confidence,
                FieldEvaluation.supported,
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
        path: FieldStatus(
            filled=True, source=source, ai_supported=supported, ai_confidence=confidence
        )
        for path, source, confidence, supported in rows
    }
