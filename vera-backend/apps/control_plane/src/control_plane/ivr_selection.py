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


async def add_active_playbook_metadata(
    session: AsyncSession, provider_id: UUID | None, metadata: dict[str, Any]
) -> None:
    """When the provider has an ACTIVE IVR playbook, add its non-PHI overlay to dispatch `metadata`
    under `ivr_playbook`; otherwise (provider unset or no active playbook) leave `metadata`
    untouched so the worker uses the generic navigator — a missing key is the generic default
    (see prompt.build_ivr_instructions). The write path validates `instructions` against
    IvrPlaybookConfig, so the stored value round-trips cleanly; `exclude_none` keeps unset fields
    out."""
    if provider_id is None:
        return
    instructions = (
        await session.execute(
            select(IvrPlaybook.instructions).where(
                IvrPlaybook.provider_id == provider_id, IvrPlaybook.status == _ACTIVE
            )
        )
    ).scalar_one_or_none()
    if instructions is None:
        return
    playbook = IvrPlaybookConfig.model_validate(instructions)
    metadata["ivr_playbook"] = playbook.model_dump(exclude_none=True)
