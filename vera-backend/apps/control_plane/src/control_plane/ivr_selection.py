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

from vera_core.models import InsuranceProvider, IvrPlaybook
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
