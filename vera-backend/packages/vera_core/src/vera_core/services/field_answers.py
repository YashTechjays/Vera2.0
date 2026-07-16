"""Shared field_answer read helpers.

`current_values_by_path` is THE definition of "a form's current answers as a
{path: raw} map" — the seed both the review endpoint's completion/promotion
recompute and the dispatcher's call-plan prefill fuse build on. One definition,
so the two can never drift on what "current" means or how values unwrap.

(Phase 2 adds the `ai_call` answer writer here.)
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.review import unwrap_value
from vera_core.models import FieldAnswer


async def current_values_by_path(session: AsyncSession, form_id: UUID) -> dict[str, object]:
    """The form's current answers: {root-anchored field_path: raw unwrapped value}."""
    return {
        path: unwrap_value(value)
        for path, value in (
            await session.execute(
                select(FieldAnswer.field_path, FieldAnswer.value).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        ).all()
    }
