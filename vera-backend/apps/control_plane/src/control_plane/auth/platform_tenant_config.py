"""SECURITY DEFINER write helpers for platform-managed tenant config. The tenant
table's platform-readable RLS policy (migration 0022) is SELECT-only, so a platform
session cannot UPDATE a tenant row directly; `set_tenant_observer_enabled` writes the
AI form-filling (observer) toggle (migration 59308656acda) and `set_tenant_retry_config`
writes the auto-retry flag/threshold (migration 9de48c83deeb), each through its own
definer function guarded by the `app.platform` GUC. Mirrors auth/platform_provisioning.py.
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


async def set_tenant_retry_config(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    enabled: bool | None,
    threshold: float | None,
) -> bool:
    """Update a tenant's auto-retry flag/threshold via the platform definer fn
    (NULL = leave unchanged). Returns True when a tenant row matched."""
    # Explicit casts on :enabled/:threshold too (not just :tenant_id): when either is
    # NULL, asyncpg has no type to infer it as, and Postgres can't resolve the
    # (uuid, boolean, numeric) overload against an untyped NULL alongside a
    # double-precision literal for the other param.
    result = await session.execute(
        text(
            "SELECT platform_set_tenant_retry_config("
            "CAST(:tenant_id AS uuid), CAST(:enabled AS boolean), CAST(:threshold AS numeric))"
        ).bindparams(tenant_id=tenant_id, enabled=enabled, threshold=threshold)
    )
    return bool(result.scalar_one())
