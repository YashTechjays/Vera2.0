"""Single-use, time-boxed user-invite tokens (workforce onboarding).

The raw token lives only inside the invite link (emailed and/or copied out-of-band
by an admin). We store it **hashed** (sha256) as the Redis key, value = the pending
account it provisions — so a Redis dump cannot be replayed into account access
(stronger than the raw-token keying in `auth/session.py`, justified by the longer
TTL and credential-setup power). Expiry auto-revokes; accept deletes the entry
(single use). Invitees are workforce members, so this carries no PHI.

Five namespaces share one store:
  * "invite"           — the onboarding token that lets an invitee set their password;
  * "invite_mfa"       — a short-lived token bridging password-set → MFA activation when
                         the tenant enforces MFA, so onboarding completes without a session;
  * "platform_invite"  — the onboarding token for a platform-operator (tenant_id is None);
  * "platform_invite_mfa" — the short-lived MFA token for a platform-operator;
  * "pwreset"          — the self-service password-reset token (tenant tier).
"""

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

INVITE_NS = "invite"
INVITE_MFA_NS = "invite_mfa"
PLATFORM_INVITE_NS = "platform_invite"
PLATFORM_INVITE_MFA_NS = "platform_invite_mfa"
PASSWORD_RESET_NS = "pwreset"


@dataclass(frozen=True)
class InviteData:
    """What an invite token resolves to — only identifiers, never a secret.
    `tenant_id` is None for a platform-operator invite (no tenant); the caller's
    namespace (tenant vs. platform) is the actual security boundary, this field is
    just the data payload."""

    tenant_id: UUID | None
    app_user_id: UUID
    email: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_id": str(self.tenant_id) if self.tenant_id is not None else None,
                "app_user_id": str(self.app_user_id),
                "email": self.email,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "InviteData":
        d = json.loads(raw)
        return cls(
            tenant_id=UUID(d["tenant_id"]) if d["tenant_id"] is not None else None,
            app_user_id=UUID(d["app_user_id"]),
            email=d["email"],
        )


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _hashed(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _key(namespace: str, token: str) -> str:
    # Key on the HASH of the token, never the token itself.
    return f"vera:{namespace}:{_hashed(token)}"


class InvitationStore(Protocol):
    async def put(self, namespace: str, data: InviteData, ttl_seconds: int) -> str:
        """Store `data` under a fresh opaque token (hashed at rest) and return the
        raw token for the link."""
        ...

    async def get(self, namespace: str, token: str) -> InviteData | None:
        """Non-consuming peek — for a validate-style check the token is still live,
        with no single-use guarantee. Never use this before doing the token's actual
        work; use `get_and_delete` there instead."""
        ...

    async def delete(self, namespace: str, token: str) -> None: ...

    async def get_and_delete(self, namespace: str, token: str) -> InviteData | None:
        """Atomically read and consume — the only safe way to redeem a single-use
        token. A plain get() followed by a later delete() leaves a window where two
        concurrent redemptions both pass validation before either one closes it."""
        ...


class InMemoryInvitationStore:
    """Dev/tests. Monotonic-clock TTL; entries vanish on restart."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, InviteData]] = {}

    async def put(self, namespace: str, data: InviteData, ttl_seconds: int) -> str:
        token = _new_token()
        self._entries[_key(namespace, token)] = (time.monotonic() + ttl_seconds, data)
        return token

    async def get(self, namespace: str, token: str) -> InviteData | None:
        entry = self._entries.get(_key(namespace, token))
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() >= expires_at:
            del self._entries[_key(namespace, token)]
            return None
        return data

    async def delete(self, namespace: str, token: str) -> None:
        self._entries.pop(_key(namespace, token), None)

    async def get_and_delete(self, namespace: str, token: str) -> InviteData | None:
        # dict.pop is synchronous — no await point between the check and the removal,
        # so this is already atomic with respect to other coroutines on this loop.
        entry = self._entries.pop(_key(namespace, token), None)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() >= expires_at:
            return None
        return data


class RedisInvitationStore:
    """Production. SETEX on put (TTL = invite lifetime), DEL on accept; Redis expiry
    auto-revokes a stale invite."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def put(self, namespace: str, data: InviteData, ttl_seconds: int) -> str:
        token = _new_token()
        await self._redis.set(_key(namespace, token), data.to_json(), ex=ttl_seconds)
        return token

    async def get(self, namespace: str, token: str) -> InviteData | None:
        raw = await self._redis.get(_key(namespace, token))
        if raw is None:
            return None
        return InviteData.from_json(raw.decode() if isinstance(raw, bytes) else raw)

    async def delete(self, namespace: str, token: str) -> None:
        await self._redis.delete(_key(namespace, token))

    async def get_and_delete(self, namespace: str, token: str) -> InviteData | None:
        # GETDEL is a single atomic Redis command — no window for a second reader to
        # observe the key between the read and the delete.
        raw = await self._redis.getdel(_key(namespace, token))
        if raw is None:
            return None
        return InviteData.from_json(raw.decode() if isinstance(raw, bytes) else raw)
