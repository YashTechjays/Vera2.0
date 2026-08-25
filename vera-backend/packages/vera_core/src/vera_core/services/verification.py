"""The one place the verified fraction is computed from a live session.

`verified_pct` is the fraction of required, applicable, collectable leaves an AUTHORITATIVE
call confirmed. The fallback gate (`control_plane.post_call.resolve_ai_processing`) reads it
through here instead of `completion_pct`, which is what stopped the park-vs-redial decision
depending on which consumer closed the call (spec E3). Values are PHI — passed to the pure
helper, never logged.

NOT yet the single source: `post_call_eval.evaluate_call` still hand-composes the same
sequence inline, because it already holds the parsed doc and an in-memory values map and
routing it here would re-query both. Until it does, the two gates agree only because both
call the same primitives with the same arguments — see the spec's deferred list.

Deliberately not part of `field_status.py`: that module scopes itself to PHI-free reads (no
value columns). Confirming a leaf requires the real answer values (`current_values_by_path`),
so this module — not that one — is where PHI enters the retry decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import satisfied_required_fraction
from vera_core.models import PatientForm, SchemaVersion
from vera_core.services.field_answers import current_values_by_path
from vera_core.services.field_status import load_authoritative_call_ids, load_field_status


async def load_verified_fraction(
    session: AsyncSession, form: PatientForm, *, floor: int
) -> float | None:
    """`verified_pct / 100` for *form*, or `None` for a legacy v1 schema — which declares no
    reference-number field, so "authoritative" is undefined and the caller must fall back to
    its previous gate rather than read 0.0 as "nothing verified"."""
    schema_json: Mapping[str, Any] = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    if not is_v2(schema_json):
        return None
    doc = FormSchemaDoc.model_validate(schema_json)
    status_by_path = await load_field_status(session, form.id)
    authoritative = await load_authoritative_call_ids(
        session, form.id, reference_field=doc.rep_call_reference_number_field
    )
    values = await current_values_by_path(session, form.id)
    return satisfied_required_fraction(
        status_by_path,
        schema_json,
        floor=floor,
        values=values,
        authoritative_calls=authoritative,
    )
