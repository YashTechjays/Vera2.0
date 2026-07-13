"""Runtime generic-vs-playbook IVR selection.

At call start the control plane (which has DB access, unlike the PHI-walled worker) resolves
the ACTIVE per-provider IVR playbook and hands its non-PHI config overlay to the worker via
dispatch metadata. No active playbook → the worker uses the generic navigator. `insurance_provider`
/ `ivr_playbook` are GLOBAL tables (no RLS), so this resolves on any session scope.
"""

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc, Leaf, parse_date_format
from vera_core.forms.placeholders import placeholder_tokens, resolve_field_path
from vera_core.forms.review import unwrap_value
from vera_core.models import FieldAnswer, InsuranceProvider, IvrPlaybook, PatientForm, SchemaVersion
from vera_core.models.enums import PlaybookStatus, ProviderStatus
from vera_core.schemas import IvrPlaybookConfig


async def add_active_playbook_metadata(
    session: AsyncSession, provider_id: UUID | None, metadata: dict[str, Any]
) -> None:
    """When an ACTIVE provider has an ACTIVE IVR playbook, add its non-PHI overlay to dispatch
    `metadata` under `ivr_playbook`; otherwise (provider unset/inactive or no active playbook)
    leave `metadata` untouched so the worker uses the generic navigator — a missing key is the
    generic default (see prompt.build_ivr_instructions). An inactive provider never steers a
    call (this is the only provider-status gate Voice Lab's session-start passes through). The
    read is lenient (from_stored drops unknown/bad-value fields, mirroring the admin _detail
    view), and `.first()` on a newest-first query tolerates a stray duplicate active row instead
    of 500ing every call start; `exclude_none` keeps unset fields out."""
    if provider_id is None:
        return
    instructions = (
        (
            await session.execute(
                select(IvrPlaybook.instructions)
                .join(InsuranceProvider, InsuranceProvider.id == IvrPlaybook.provider_id)
                .where(
                    IvrPlaybook.provider_id == provider_id,
                    IvrPlaybook.status == PlaybookStatus.ACTIVE,
                    InsuranceProvider.status == ProviderStatus.ACTIVE,
                )
                .order_by(IvrPlaybook.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if instructions is None:
        return
    # Only attach a non-empty overlay: a missing key is the generic default, and a row where
    # nothing survives from_stored (all keys unknown/bad) must stay generic, not ship `{}`.
    overlay = IvrPlaybookConfig.from_stored(instructions).model_dump(exclude_none=True)
    if overlay:
        metadata["ivr_playbook"] = overlay


def _to_date(text: str, leaf: Leaf | None) -> date | None:
    """Parse a stored date value: ISO first (the machine-intake shape), else the leaf's own
    display `date_format` (the review UI's shape). None if neither parses."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    fmt = leaf.validation.date_format if leaf and leaf.validation else None
    return parse_date_format(text, fmt) if fmt else None


def _spoken_value(raw: Any, leaf: Leaf | None) -> str | None:
    """Trim a resolved value to a form fit to speak, or None. Drops blank / the intake `"N/A"`
    default (so a placeholder never becomes a spoken identifier — the prompt falls back to its
    neutral default), and normalizes a date leaf to `MM/DD/YYYY` for a clean spoken readout."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return None
    if leaf is not None and leaf.type == "date":
        parsed = _to_date(text, leaf)
        if parsed is not None:
            return parsed.strftime("%m/%d/%Y")
    return text


def build_agent_context(doc: FormSchemaDoc, values_by_path: dict[str, Any]) -> dict[str, str]:
    """Resolve every `{{token}}` the schema defines (`system_fields` handles + `context`-role leaf
    paths) to its value from `values_by_path` (a `{field_path: value}` map). Pure — no DB. Empty /
    `"N/A"` values are dropped and date leaves normalized to `MM/DD/YYYY`, so the result is exactly
    the tokens the agent can actually speak."""
    leaves = dict(doc.leaf_items())
    context: dict[str, str] = {}
    for token in placeholder_tokens(doc):
        path = resolve_field_path(doc, token)
        value = _spoken_value(values_by_path.get(path), leaves.get(path)) if path else None
        if value is not None:
            context[token] = value
    return context


async def add_agent_context_metadata(
    session: AsyncSession, form: PatientForm, metadata: dict[str, Any]
) -> None:
    """Attach the resolved `{{token}}` -> value context an agent's prompt reads to dispatch
    `metadata` under `agent_context`. Schema-driven, NO hardcoded paths: the values come from the
    active (`is_current`) `field_answer` rows keyed by the form's pinned schema. Nothing is attached
    for a legacy (v1) schema or when the form carries no resolvable values, so the agent falls back
    to its built-in placeholder defaults."""
    schema_json = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one_or_none()
    if schema_json is None or not is_v2(schema_json):
        return
    rows = (
        await session.execute(
            select(FieldAnswer.field_path, FieldAnswer.value).where(
                FieldAnswer.form_id == form.id, FieldAnswer.is_current.is_(True)
            )
        )
    ).all()
    values_by_path = {path: unwrap_value(value) for path, value in rows}
    context = build_agent_context(FormSchemaDoc.model_validate(schema_json), values_by_path)
    if context:
        metadata["agent_context"] = context
