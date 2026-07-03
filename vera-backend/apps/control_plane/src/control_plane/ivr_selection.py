"""Runtime generic-vs-playbook IVR selection.

At call start the control plane (which has DB access, unlike the PHI-walled worker) resolves
the ACTIVE per-provider IVR playbook and hands its non-PHI config overlay to the worker via
dispatch metadata. No active playbook → the worker uses the generic navigator. `insurance_provider`
/ `ivr_playbook` are GLOBAL tables (no RLS), so this resolves on any session scope.
"""

import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import IvrPlaybook
from vera_core.schemas import IvrPlaybookConfig

logger = logging.getLogger(__name__)

_ACTIVE = "active"


async def add_active_playbook_metadata(
    session: AsyncSession, provider_id: UUID | None, metadata: dict[str, Any]
) -> None:
    """When the provider has an ACTIVE IVR playbook, add its non-PHI overlay to dispatch `metadata`
    under `ivr_playbook`; otherwise (provider unset or no active playbook) leave `metadata`
    untouched so the worker uses the generic navigator — a missing key is the generic default
    (see prompt.build_ivr_instructions). The write path validates `instructions` against
    IvrPlaybookConfig, but the table predates that path (seed scripts, raw SQL), so a row that
    no longer validates degrades to the generic navigator instead of 500ing every call start
    for the provider; `exclude_none` keeps unset fields out."""
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
    try:
        playbook = IvrPlaybookConfig.model_validate(instructions)
    except ValidationError:
        logger.warning(
            "active ivr_playbook for provider %s failed validation; using generic navigator",
            provider_id,
        )
        return
    metadata["ivr_playbook"] = playbook.model_dump(exclude_none=True)
