"""SECURITY DEFINER write helpers for platform-managed tenant rows (VR2-30 + earlier).
The tenant table's platform-readable RLS policy (migration 0022) is SELECT-only, so a
platform session cannot INSERT/UPDATE a tenant row directly; every write here goes
through its own definer function guarded by the `app.platform` GUC (migration
e529f5cac06d for create/update/status; 59308656acda / 9de48c83deeb for the older
observer/retry-config toggles). Mirrors auth/platform_provisioning.py.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_tenant(
    session: AsyncSession, *, tenant_id: UUID, name: str, slug: str, region: str | None
) -> None:
    """Insert a new tenant via the platform definer function. `tenant_id` is minted by
    the caller (UUIDv7, ADR-0002) — the function does not generate one. Raises
    IntegrityError on a duplicate slug; the router maps that to 409."""
    await session.execute(
        text(
            "SELECT platform_create_tenant("
            "CAST(:tenant_id AS uuid), CAST(:name AS text), CAST(:slug AS text),"
            " CAST(:region AS text))"
        ).bindparams(tenant_id=tenant_id, name=name, slug=slug, region=region)
    )


async def update_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    name: str | None,
    region: str | None,
    clear_region: bool,
    observer_enabled: bool | None,
    auto_retry_enabled: bool | None,
    retry_fill_threshold: float | None,
    max_agents_per_va: int | None,
    max_concurrent_calls: int | None,
    max_retries: int | None,
    queue_expiry_hours: int | None,
    recording_retention_days: int | None,
    clear_recording_retention_days: bool,
) -> bool:
    """Update a tenant's editable settings via the platform definer fn (NULL = leave
    unchanged, except the two `clear_*` flags which explicitly set their nullable
    column to NULL). Returns True when a tenant row matched."""
    # Explicit casts on every param (not just :tenant_id): an untyped NULL alongside a
    # typed literal for a sibling param makes Postgres unable to resolve the overload
    # (same reasoning as set_tenant_retry_config below).
    result = await session.execute(
        text(
            "SELECT platform_update_tenant("
            "CAST(:tenant_id AS uuid), CAST(:name AS text), CAST(:region AS text),"
            " CAST(:clear_region AS boolean), CAST(:observer_enabled AS boolean),"
            " CAST(:auto_retry_enabled AS boolean), CAST(:retry_fill_threshold AS numeric),"
            " CAST(:max_agents_per_va AS integer), CAST(:max_concurrent_calls AS integer),"
            " CAST(:max_retries AS integer), CAST(:queue_expiry_hours AS integer),"
            " CAST(:recording_retention_days AS integer),"
            " CAST(:clear_recording_retention_days AS boolean))"
        ).bindparams(
            tenant_id=tenant_id,
            name=name,
            region=region,
            clear_region=clear_region,
            observer_enabled=observer_enabled,
            auto_retry_enabled=auto_retry_enabled,
            retry_fill_threshold=retry_fill_threshold,
            max_agents_per_va=max_agents_per_va,
            max_concurrent_calls=max_concurrent_calls,
            max_retries=max_retries,
            queue_expiry_hours=queue_expiry_hours,
            recording_retention_days=recording_retention_days,
            clear_recording_retention_days=clear_recording_retention_days,
        )
    )
    return bool(result.scalar_one())


async def set_tenant_status(
    session: AsyncSession, *, tenant_id: UUID, target_status: str
) -> str | None:
    """Flip a tenant between active/deactivated via the platform definer fn. Returns
    the tenant's PREVIOUS status (letting the router tell an already-in-that-state
    409 from a genuine flip in one round trip), or None when no tenant has that id."""
    result = await session.execute(
        text(
            "SELECT platform_set_tenant_status(CAST(:tenant_id AS uuid), :target_status)"
        ).bindparams(tenant_id=tenant_id, target_status=target_status)
    )
    previous: str | None = result.scalar_one()
    return previous


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
