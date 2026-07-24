"""SECURITY DEFINER write helper for the per-tenant AI form-filling (observer) toggle
(migration 59308656acda). The tenant table's platform-readable RLS policy (migration
0022) is SELECT-only, so a platform session cannot UPDATE a tenant row directly; the
`platform_set_tenant_observer_enabled` definer function performs the write, guarded by
the `app.platform` GUC. Mirrors auth/platform_provisioning.py.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant_observer_enabled(
    session: AsyncSession, *, tenant_id: UUID, enabled: bool
) -> bool:
    """Flip a tenant's `observer_enabled` via the platform definer function. Returns
    `True` when a tenant row matched, `False` when no tenant has that id."""
    result = await session.execute(
        text(
            "SELECT platform_set_tenant_observer_enabled(CAST(:tenant_id AS uuid), :enabled)"
        ).bindparams(tenant_id=tenant_id, enabled=enabled)
    )
    return bool(result.scalar_one())
