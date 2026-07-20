"""Shared field_answer read helpers + the machine-answer writer.

`current_values_by_path` is THE definition of "a form's current answers as a
{path: raw} map" — the seed both the review endpoint's completion/promotion
recompute and the dispatcher's call-plan prefill fuse build on. One definition,
so the two can never drift on what "current" means or how values unwrap.

`record_answer` is the single supersede-writer the worker-event consumer uses to
persist an Observer-extracted `ai_call` answer; `recompute_form_projection` re-derives
the promoted worklist columns + completion_pct after such a write.
"""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import InvalidIntakeValue, promote_columns
from vera_core.forms.review import completion_pct, completion_pct_v2, unwrap_value
from vera_core.models import FieldAnswer, PatientForm

logger = logging.getLogger("vera_core.field_answers")


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


async def record_answer(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    form_id: UUID,
    call_id: UUID | None,
    field_path: str,
    raw_value: Any,
    source: str,
    confidence: int | None = None,
    evidence_seq: int | None = None,
) -> bool:
    """Supersede the current answer for (form, field) with a new one, returning whether a
    row was written. Idempotent under the worker stream's at-least-once redelivery: an
    identical (source, call_id, value) that is already current is a no-op (returns False)
    Demote-then-flush-then-insert keeps the `fa_current_uq` partial-unique index (one
    current row per field) satisfied through the swap."""
    current = (
        await session.execute(
            select(FieldAnswer).where(
                FieldAnswer.form_id == form_id,
                FieldAnswer.field_path == field_path,
                FieldAnswer.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    # Replay checks, same call + same source only. Cross-source writes (a human resolve) and
    # seq-less answers (intake) fall through — they always supersede.
    if current is not None and current.source == source and current.call_id == call_id:
        if unwrap_value(current.value) == raw_value:
            return False
        if (
            evidence_seq is not None
            and current.evidence_seq is not None
            and evidence_seq < current.evidence_seq
        ):
            return False
    if current is not None:
        current.is_current = False
        await session.flush()  # clear the old current before inserting the new one
    session.add(
        FieldAnswer(
            tenant_id=tenant_id,
            form_id=form_id,
            call_id=call_id,
            field_path=field_path,
            value={"value": raw_value},
            source=source,
            confidence=confidence,
            evidence_seq=evidence_seq,
            is_current=True,
        )
    )
    return True


async def recompute_form_projection(
    session: AsyncSession, form: PatientForm, schema_json: Mapping[str, Any]
) -> None:
    """Re-derive the promoted `patient_form` columns + `completion_pct` from the form's
    current answers, after an answer write. Worker-safe: a bad promoted value (e.g. an
    unparseable date) is logged and skips promotion rather than raising — unlike the
    endpoint's 422 contract. The `flush()` before `refresh()` is load-bearing: refresh
    reloads from the DB and would otherwise discard the pending completion update."""
    current_values = await current_values_by_path(session, form.id)
    doc = FormSchemaDoc.model_validate(schema_json) if is_v2(schema_json) else None
    if doc is not None:
        try:
            promoted = promote_columns(current_values.get, doc)
        except InvalidIntakeValue as exc:
            logger.warning(
                "recompute: promotion skipped for form %s (%s)", form.id, type(exc).__name__
            )
        else:
            for column, _path in doc.promoted_fields.items():
                new_value = getattr(promoted, column)
                if getattr(form, column) != new_value:
                    setattr(form, column, new_value)
        form.completion_pct = completion_pct_v2(current_values, schema_json)
    else:
        form.completion_pct = completion_pct(set(current_values), schema_json)
    await session.flush()
    await session.refresh(form)
