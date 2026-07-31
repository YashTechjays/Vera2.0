"""SECURITY DEFINER helpers for inviting/listing a chosen tenant's users from the
platform plane (VR2-30, migration b4d7a95a60fb). `app_user`/`user_role` RLS lets a
platform session reach only NULL-tenant rows and fails SILENTLY in both directions — a
plain SELECT returns nothing, a plain INSERT affects nothing — so the read side needs a
definer fn just as much as the write. Mirrors platform_tenant_config.py.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class TenantUserRow:
    id: UUID
    email: str
    name: str
    status: str
    roles: list[str]


async def invite_tenant_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    email: str,
    name: str,
    invited_by: UUID,
    role_ids: list[UUID],
    grant_ids: list[UUID],
) -> str:
    """Create the invited `app_user` row (+ its `user_role` grants) via the platform
    definer fn, returning 'ok' | 'duplicate' | 'no_tenant'. `grant_ids[i]` is the
    caller-minted id for the grant assigning `role_ids[i]` — the two run parallel because
    user_role.id has no server default (ADR-0002), same as the app_user id itself."""
    result = await session.execute(
        text(
            "SELECT platform_invite_tenant_user("
            "CAST(:tenant_id AS uuid), CAST(:user_id AS uuid), CAST(:email AS text),"
            " CAST(:name AS text), CAST(:invited_by AS uuid),"
            " CAST(:role_ids AS uuid[]), CAST(:grant_ids AS uuid[]))"
        ).bindparams(
            tenant_id=tenant_id,
            user_id=user_id,
            email=email,
            name=name,
            invited_by=invited_by,
            role_ids=role_ids or None,
            grant_ids=grant_ids or None,
        )
    )
    outcome: str = result.scalar_one()
    return outcome


async def list_tenant_users(session: AsyncSession, *, tenant_id: UUID) -> list[TenantUserRow]:
    """A tenant's users (id/email/name/status/roles) via the platform definer fn."""
    rows = (
        await session.execute(
            text(
                "SELECT id, email, name, status, roles FROM platform_list_tenant_users(:tenant_id)"
            ).bindparams(tenant_id=tenant_id)
        )
    ).all()
    return [
        TenantUserRow(id=r.id, email=r.email, name=r.name, status=r.status, roles=list(r.roles))
        for r in rows
    ]
