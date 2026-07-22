"""Shared reset/reissue logic for a stuck invitee — someone whose invite link or
MFA bridge token expired before they finished onboarding, leaving `status="invited"`
permanently stuck (no prior resend/reset path existed in this codebase for either
tier). Used by both the tenant and platform resend-invitation endpoints; the only
difference between tiers is the InvitationStore namespace and which AuthEvent the
caller emits."""

import json
from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.invitations import (
    InMemoryInvitationStore,
    InvitationStore,
    InviteData,
)
from vera_core.models import AppUser, UserIdentity
from vera_core.models.enums import ProviderKind

if TYPE_CHECKING:
    from redis.asyncio import Redis


async def reset_and_reissue_invite(
    session: AsyncSession,
    invites: InvitationStore,
    *,
    namespace: str,
    app_user: AppUser,
    ttl_seconds: int,
    redis: "Redis | None" = None,
) -> str:
    """Delete any stale password UserIdentity for `app_user` (safe: it's useless if
    MFA was never completed — a fresh accept will create a new one), invalidate any
    stale invite tokens, and mint a fresh invite token in `namespace`.
    Returns the raw token for the caller to build a fresh invite_url. DELETE needs
    no SECURITY DEFINER helper even for a NULL-tenant platform row: RLS only
    evaluates USING (not WITH CHECK) for DELETE, and the platform-readable policy's
    USING clause already permits NULL-tenant rows under a platform session."""
    await session.execute(
        delete(UserIdentity).where(
            UserIdentity.app_user_id == app_user.id,
            UserIdentity.provider_type == ProviderKind.PASSWORD.value,
        )
    )

    # Invalidate stale tokens for this user in the given namespace.
    # Handle both InMemoryInvitationStore (for tests) and RedisInvitationStore (for prod).
    if isinstance(invites, InMemoryInvitationStore):
        # For in-memory store, iterate through entries and delete matching ones
        keys_to_delete = []
        for key, (_expires_at, data) in invites._entries.items():
            if key.startswith(f"vera:{namespace}:") and data.app_user_id == app_user.id:
                keys_to_delete.append(key)
        for key in keys_to_delete:
            del invites._entries[key]
    elif redis is not None:
        # For Redis store, use SCAN to find and delete matching keys
        cursor = 0
        namespace_prefix = f"vera:{namespace}:"
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{namespace_prefix}*", count=100)
            for key in keys:  # type: ignore[assignment]
                try:
                    raw_value = await redis.get(key)
                    if raw_value is not None:
                        value_str = (
                            raw_value.decode() if isinstance(raw_value, bytes) else raw_value
                        )
                        data = json.loads(value_str)
                        if data.get("app_user_id") == str(app_user.id):
                            await redis.delete(key)
                except Exception:
                    # Ignore errors parsing individual keys; they're likely stale
                    pass
            if cursor == 0:
                break

    return await invites.put(
        namespace,
        InviteData(tenant_id=app_user.tenant_id, app_user_id=app_user.id, email=app_user.email),
        ttl_seconds,
    )
