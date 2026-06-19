"""Scoped elevation (break-glass) — the app-side wrapper over the migration-0002
SECURITY DEFINER functions (ADR-0006 §B/§C).

The app role has NO direct INSERT/UPDATE on `tenant_elevation`; every write and the
oversight/per-request reads go through `create_elevation_grant` / `end_elevation_grant`
/ `active_elevation_grants`. Per-request validation (`active_grant_for`, used by the
request chain) and the platform oversight list (`active_grants`) share one function so
"active" means the same thing — un-ended and unexpired — in both places.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_GRANT_COLUMNS = (
    "id, super_admin_user_id, target_tenant_id, reason, granted_at, expires_at, ended_at"
)


@dataclass(frozen=True)
class ElevationGrant:
    id: UUID
    super_admin_user_id: UUID
    target_tenant_id: UUID
    reason: str
    granted_at: datetime
    expires_at: datetime
    ended_at: datetime | None


async def create_grant(
    session: AsyncSession,
    *,
    super_admin_user_id: UUID,
    target_tenant_id: UUID,
    reason: str,
    duration_minutes: int,
) -> UUID:
    """Create a single-tenant grant; returns its id. `expires_at` is computed
    DB-side (`now() + interval`) from `duration_minutes`, so no app clock takes
    part. Raises a DB error (mapped to 400 by the endpoint) on an empty reason or
    a non-positive duration, and a unique violation (409) if the operator already
    holds an active grant."""
    grant_id: UUID = (
        await session.execute(
            text("SELECT create_elevation_grant(:a, :t, :r, :d)").bindparams(
                a=super_admin_user_id, t=target_tenant_id, r=reason, d=duration_minutes
            )
        )
    ).scalar_one()
    return grant_id


async def end_grant(session: AsyncSession, elevation_id: UUID) -> bool:
    """End a grant early. Returns True if it was active, False if already ended
    (or unknown) — an idempotent no-op the endpoint maps to 404."""
    ended: bool = (
        await session.execute(text("SELECT end_elevation_grant(:g)").bindparams(g=elevation_id))
    ).scalar_one()
    return ended


async def active_grants(
    session: AsyncSession,
    *,
    super_admin_user_id: UUID | None = None,
    target_tenant_id: UUID | None = None,
) -> list[ElevationGrant]:
    rows = (
        await session.execute(
            text(f"SELECT {_GRANT_COLUMNS} FROM active_elevation_grants(:a, :t)").bindparams(
                a=super_admin_user_id, t=target_tenant_id
            )
        )
    ).mappings()
    return [ElevationGrant(**row) for row in rows]


async def active_grant_for(
    session: AsyncSession, *, super_admin_user_id: UUID, target_tenant_id: UUID
) -> ElevationGrant | None:
    """The operator's active grant into `target_tenant_id`, or None — the per-request
    elevation check behind the tenant context / elevated session."""
    grants = await active_grants(
        session, super_admin_user_id=super_admin_user_id, target_tenant_id=target_tenant_id
    )
    return grants[0] if grants else None


async def active_grant_for_operator(
    session: AsyncSession, *, operator: UUID
) -> ElevationGrant | None:
    """The operator's single active grant, or None. Relies on the DB unique constraint
    that an operator holds AT MOST ONE active grant — so the operating tenant is
    unambiguous without a target in the request. If that constraint is ever relaxed,
    callers MUST pass an explicit target selector instead of trusting grants[0]."""
    grants = await active_grants(session, super_admin_user_id=operator)
    assert len(grants) <= 1, "DB unique constraint guarantees ≤1 active grant per operator"
    return grants[0] if grants else None
