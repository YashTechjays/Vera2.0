"""Runtime generic-vs-playbook IVR selection.

At call start the control plane (which has DB access, unlike the PHI-walled worker) resolves
the ACTIVE per-provider IVR playbook and hands its non-PHI config overlay to the worker via
dispatch metadata. No active playbook → the worker uses the generic navigator. `insurance_provider`
/ `ivr_playbook` are GLOBAL tables (no RLS), so this resolves on any session scope.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.intake import resolve_path
from vera_core.models import InsuranceProvider, IvrPlaybook, PatientForm
from vera_core.models.enums import PlaybookStatus, ProviderStatus
from vera_core.schemas import IvrCallData, IvrPlaybookConfig

# ibv_standard intake paths for the non-promoted identifiers the navigator reads out.
# (member_id / patient_name / patient_dob are promoted columns, read directly.) Schema-
# specific: if another schema gets IVR calls, resolve these from its `system_fields` handles.
_GROUP_NUMBER_PATH = "sections.insurance_information.group_number"
_PROVIDER_NPI_PATH = "sections.provider_reference_information.npi"
_TAX_ID_PATH = "sections.hospital_information.tax_id"


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


def _spoken_value(raw: Any) -> str | None:
    """Trim to a value fit to speak, or None. Drops the intake `"N/A"` default so a
    placeholder never becomes a spoken identifier (the prompt falls back to neutral phrasing)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return None
    return text


def add_ivr_call_data_metadata(form: PatientForm, metadata: dict[str, Any]) -> None:
    """Attach the patient/provider identifiers the IVR navigator speaks/keys to dispatch
    `metadata` under `ivr_call_data`, read off the call's already-loaded `PatientForm` (no
    extra queries). Promoted columns (member_id / patient_name / patient_dob) are read
    directly; group_number / provider NPI / Tax ID come from `intake_payload`. `exclude_none`
    keeps unset fields out; nothing is attached when the form carries no identifiers, so the
    navigator falls back to its built-in placeholders.

    PHI: these are raw patient identifiers. They ride LiveKit dispatch metadata (inside the
    trust boundary) into the LLM prompt by deliberate design — never log/trace/emit them here."""
    provider_npi = _spoken_value(resolve_path(form.intake_payload, _PROVIDER_NPI_PATH))
    data = IvrCallData(
        patient_name=_spoken_value(form.patient_name),
        member_id=_spoken_value(form.member_id),
        date_of_birth=form.patient_dob.strftime("%m/%d/%Y") if form.patient_dob else None,
        group_number=_spoken_value(resolve_path(form.intake_payload, _GROUP_NUMBER_PATH)),
        provider_npi=provider_npi,
        # No distinct "provider ID" in the schema — reuse the NPI so the rule has a value.
        provider_id=provider_npi,
        tax_id=_spoken_value(resolve_path(form.intake_payload, _TAX_ID_PATH)),
    ).model_dump(exclude_none=True)
    if data:
        metadata["ivr_call_data"] = data
