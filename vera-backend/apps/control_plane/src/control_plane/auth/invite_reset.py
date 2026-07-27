"""Shared reset/reissue logic for a stuck invitee — someone whose invite link or
MFA bridge token expired before they finished onboarding, leaving `status="invited"`
permanently stuck (no prior resend/reset path existed in this codebase for either
tier). Used by both the tenant and platform resend-invitation endpoints; the only
difference between tiers is the InvitationStore namespace and which AuthEvent the
caller emits."""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.invitations import InvitationStore, InviteData
from vera_core.models import AppUser, UserIdentity
from vera_core.models.enums import ProviderKind


async def reset_and_reissue_invite(
    session: AsyncSession,
    invites: InvitationStore,
    *,
    namespace: str,
    app_user: AppUser,
    ttl_seconds: int,
) -> str:
    """Delete any stale password UserIdentity for `app_user` (safe: it's useless if
    MFA was never completed — a fresh accept will create a new one) and mint a
    fresh invite token in `namespace`. Returns the raw token for the caller to
    build a fresh invite_url. DELETE needs no SECURITY DEFINER helper even for a
    NULL-tenant platform row: RLS only evaluates USING (not WITH CHECK) for DELETE,
    and the platform-readable policy's USING clause already permits NULL-tenant
    rows under a platform session."""
    await session.execute(
        delete(UserIdentity).where(
            UserIdentity.app_user_id == app_user.id,
            UserIdentity.provider_type == ProviderKind.PASSWORD.value,
        )
    )
    return await invites.put(
        namespace,
        InviteData(tenant_id=app_user.tenant_id, app_user_id=app_user.id, email=app_user.email),
        ttl_seconds,
    )
