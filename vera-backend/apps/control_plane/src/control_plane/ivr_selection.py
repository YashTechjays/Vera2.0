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

from vera_core.models import IvrPlaybook
from vera_core.schemas import IvrPlaybookConfig

_ACTIVE = "active"


async def resolve_active_playbook(
    session: AsyncSession, provider_id: UUID | None
) -> IvrPlaybookConfig | None:
    """Return the provider's ACTIVE IVR playbook overlay, or None when the provider is unset or
    has no active playbook. The write path validates `instructions` against IvrPlaybookConfig, so
    the stored value round-trips cleanly (mirrors calls.py trusting the stored persona_tweak)."""
    if provider_id is None:
        return None
    instructions = (
        await session.execute(
            select(IvrPlaybook.instructions).where(
                IvrPlaybook.provider_id == provider_id, IvrPlaybook.status == _ACTIVE
            )
        )
    ).scalar_one_or_none()
    if instructions is None:
        return None
    return IvrPlaybookConfig.model_validate(instructions)


async def add_active_playbook_metadata(
    session: AsyncSession, provider_id: UUID | None, metadata: dict[str, Any]
) -> None:
    """When the provider has an active playbook, add its non-PHI overlay to dispatch `metadata`
    under `ivr_playbook`; otherwise leave metadata untouched so the worker uses the generic
    navigator. `exclude_none` keeps unset fields out — the worker treats a missing key as the
    generic default (see prompt.build_ivr_instructions)."""
    playbook = await resolve_active_playbook(session, provider_id)
    if playbook is not None:
        metadata["ivr_playbook"] = playbook.model_dump(exclude_none=True)
