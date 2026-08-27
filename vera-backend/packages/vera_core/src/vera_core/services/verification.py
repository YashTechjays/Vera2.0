"""The one place the verified fraction is computed from a live session.

`verified_pct` is the fraction of required, applicable, collectable leaves an AUTHORITATIVE
call confirmed. Values are PHI — passed to the pure helper, never logged.

Read by the dispute-resolve endpoint, refreshing the stored column. The eval path computes the
same fraction inline instead: `evaluate_call` already holds the parsed doc, the status map and
the values, so routing through this loader would re-query all three. Both share
`satisfied_required_fraction`, which is where the definition itself lives.

NOT read by `resolve_ai_processing` any more, and that removal is the point:
`is_call_confirmed` requires a judge verdict (`ai_supported`), and that resolver runs precisely
when no judge ran — so this fraction was structurally 0.0 there for every call however good,
and 0.0 is below every threshold. It is a safety net that guarantees a form leaves
AI_PROCESSING; it makes no fill-based retry decision, because it has no evidence to make one
from. One decision, one site: `services/retry_decision.decide_retry`.

Deliberately not part of `field_status.py`: that module scopes itself to PHI-free reads (no
value columns). Confirming a leaf needs the real answer values, so this module — not that one
— is where PHI enters the retry decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import satisfied_required_fraction
from vera_core.services.field_status import load_authoritative_call_ids, load_field_status


async def load_verified_fraction(
    session: AsyncSession,
    form_id: UUID,
    *,
    floor: int,
    doc: FormSchemaDoc | None,
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any],
) -> float | None:
    """`verified_pct / 100` for the form, or `None` when *doc* is None — a legacy v1 schema
    declares no reference-number field, so "authoritative" is undefined and the caller must
    leave the column alone rather than read 0.0 as "nothing verified".

    The parsed doc, its raw json and the current values are passed IN, not re-fetched: the one
    caller already holds all three, and re-deriving them meant a second read of a multi-MB
    JSONB, a second parse of the same document, and a second values query per request. The two
    reads left are the two that must run against the live session, after the caller's writes:
    an answer superseded by a human edit changes both the status map and, when it lands on the
    reference-number leaf, which calls are still authoritative.
    """
    if doc is None:
        return None
    status_by_path = await load_field_status(session, form_id)
    authoritative = await load_authoritative_call_ids(
        session, form_id, reference_field=doc.rep_call_reference_number_field
    )
    return satisfied_required_fraction(
        status_by_path,
        schema_json,
        floor=floor,
        values=values,
        authoritative_calls=authoritative,
    )
