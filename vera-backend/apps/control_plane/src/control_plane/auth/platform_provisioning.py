"""SECURITY DEFINER write helpers for NULL-tenant (platform-operator) rows
(migration: platform operator lifecycle definer functions). The platform-readable
RLS policy's WITH CHECK is strict equality (vera_core/db/rls.py), so the RLS-bound
app role can never INSERT or UPDATE a NULL-tenant row directly — only SELECT and
DELETE work unassisted (RLS evaluates USING, not WITH CHECK, for those). Mirrors the
platform MFA definer pattern (control_plane/auth/mfa.py, migration f066c667ddc1) for
the invite/accept/deactivate lifecycle instead of MFA enrollment.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_operator_invite(
    session: AsyncSession, *, email: str, name: str, invited_by: UUID | None
) -> UUID:
    """Create an invited platform AppUser and grant it SUPER_ADMIN, atomically."""
    user_id: UUID = (
        await session.execute(
            text(
                "SELECT platform_create_operator_invite(:email, :name, CAST(:invited_by AS uuid))"
            ).bindparams(email=email, name=name, invited_by=invited_by)
        )
    ).scalar_one()
    return user_id


async def create_password_identity(
    session: AsyncSession, *, app_user_id: UUID, email: str, hashed_password: str
) -> UUID:
    """Create the password UserIdentity for a platform operator accepting their invite."""
    identity_id: UUID = (
        await session.execute(
            text(
                "SELECT platform_create_password_identity("
                "CAST(:app_user_id AS uuid), :email, :hashed_password)"
            ).bindparams(app_user_id=app_user_id, email=email, hashed_password=hashed_password)
        )
    ).scalar_one()
    return identity_id


async def set_operator_status(session: AsyncSession, *, app_user_id: UUID, status: str) -> bool:
    """Flip a platform operator's status to 'active' or 'deactivated'. Returns
    whether a row was actually updated (False if the id doesn't match a platform
    operator)."""
    result = await session.execute(
        text("SELECT platform_set_operator_status(CAST(:app_user_id AS uuid), :status)").bindparams(
            app_user_id=app_user_id, status=status
        )
    )
    return bool(result.scalar_one())
