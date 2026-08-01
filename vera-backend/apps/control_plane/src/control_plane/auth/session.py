"""Opaque session tokens — the only request-time credential.

Login (auth/session is store; auth endpoints mint) returns a random opaque
token; the session state lives in Redis (or an in-memory store for dev/tests)
under a short TTL. This replaces client-issued/verified JWTs entirely:

  * revocation is a single DEL (HIPAA auto-logoff / explicit logout),
  * no signing keys to manage or rotate,
  * the verify path is identical no matter which provider authenticated.

Two keyspaces share one store via a `namespace`:
  * "sess" — a fully-authenticated session (`mfa_passed` is always true here);
  * "mfa"  — the brief pending-MFA challenge between password success and the
             second factor. SessionVerifier NEVER accepts an "mfa" token.
  * "sess_abs" — a companion to a "sess" token holding only the absolute-cap TTL,
                 set once at login and never extended; bounds total session lifetime.
"""

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from redis.asyncio import Redis

from control_plane.auth.identity import InvalidTokenError, VerifiedIdentity
from vera_core.models.enums import AccountType

SESSION_NS = "sess"
MFA_NS = "mfa"
MFA_ENROLL_NS = "mfa_enroll"  # first-login MFA bootstrap (enforced tenant, not yet enrolled)
# Companion key for a fully-authenticated session: same token, holds no data — only
# its TTL, set once at login to the absolute max and NEVER extended. extend_session
# caps the sliding `sess` TTL at this key's remaining TTL, so `sess` can never outlive
# the cap and the verify hot path needs no clock and no extra read.
SESSION_ABS_NS = "sess_abs"


@dataclass(frozen=True)
class SessionData:
    """What we store behind a token. Never contains PHI or the password/TOTP
    secret — only identifiers and the MFA gate state.

    `tenant_id` is `None` for a platform operator's session (a SUPER_ADMIN with no
    home tenant); a tenant user's session always carries their tenant. `tenant_slug`
    is that tenant's URL slug, used for display and invite-URL construction;
    `None` for a platform operator (no home tenant) and for any session minted
    before slug capture.

    `session_id` is a non-secret handle for THIS login (one user's two browsers get
    different ids), so per-session resources can be named without exposing the token —
    it is never a credential itself."""

    user_id: UUID
    tenant_id: UUID | None
    email: str
    subject: str
    provider_type: str
    mfa_passed: bool
    account_type: str  # serialized AccountType value — required; no default
    tenant_slug: str | None = None
    session_id: UUID = field(default_factory=uuid4)

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": str(self.user_id),
                "tenant_id": str(self.tenant_id) if self.tenant_id is not None else None,
                "email": self.email,
                "subject": self.subject,
                "provider_type": self.provider_type,
                "mfa_passed": self.mfa_passed,
                "account_type": self.account_type,
                "tenant_slug": self.tenant_slug,
                "session_id": str(self.session_id),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "SessionData":
        d = json.loads(raw)
        tenant_raw = d["tenant_id"]
        return cls(
            user_id=UUID(d["user_id"]),
            tenant_id=UUID(tenant_raw) if tenant_raw is not None else None,
            email=d["email"],
            subject=d["subject"],
            provider_type=d["provider_type"],
            mfa_passed=d["mfa_passed"],
            account_type=d["account_type"],  # required key — raises KeyError on legacy sessions
            tenant_slug=d.get("tenant_slug"),
            # Sessions minted before this field are still live in Redis — synthesize.
            session_id=UUID(d["session_id"]) if d.get("session_id") else uuid4(),
        )


def _key(namespace: str, token: str) -> str:
    return f"vera:{namespace}:{token}"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


# Sentinel stored in the in-memory sess_abs entry. Only the expiry timestamp of that
# entry is ever read (in extend_session); the SessionData value itself is never used.
# Using a dedicated sentinel makes the intent explicit and mirrors the Redis store,
# which writes "1" (also a valueless placeholder) for sess_abs.
_ABS_SENTINEL: SessionData = SessionData(
    user_id=uuid4(),
    tenant_id=None,
    email="",
    subject="",
    provider_type="",
    mfa_passed=False,
    account_type=AccountType.TENANT.value,
)


class SessionStore(Protocol):
    async def put(self, namespace: str, data: SessionData, ttl_seconds: int) -> str:
        """Store `data` under a fresh opaque token and return the token."""
        ...

    async def get(self, namespace: str, token: str) -> SessionData | None: ...
    async def delete(self, namespace: str, token: str) -> None: ...

    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        """Mint a fully-authenticated session: a `sess` key (EX idle_ttl) plus a
        `sess_abs` companion (EX abs_ttl). Returns the shared opaque token."""
        ...

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        """Slide the `sess` TTL to min(idle_ttl, absolute remaining). Returns the new
        remaining seconds, or None if the absolute cap is reached or the session is gone."""
        ...

    async def absolute_remaining(self, token: str) -> int | None:
        """Read-only seconds left until the absolute cap (the `sess_abs` TTL), without
        sliding anything. Returns None if the cap is reached or the companion is gone."""
        ...

    async def delete_session(self, token: str) -> None:
        """Delete both the `sess` and `sess_abs` keys (logout)."""
        ...


class InMemorySessionStore:
    """Dev/tests. Monotonic-clock TTL; values vanish on restart."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, SessionData]] = {}

    async def put(self, namespace: str, data: SessionData, ttl_seconds: int) -> str:
        token = _new_token()
        self._entries[_key(namespace, token)] = (time.monotonic() + ttl_seconds, data)
        return token

    async def get(self, namespace: str, token: str) -> SessionData | None:
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

    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        token = _new_token()
        now = time.monotonic()
        self._entries[_key(SESSION_NS, token)] = (now + idle_ttl, data)
        # abs entry: only its expiry timestamp is meaningful — value is a sentinel.
        self._entries[_key(SESSION_ABS_NS, token)] = (now + abs_ttl, _ABS_SENTINEL)
        return token

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        now = time.monotonic()
        abs_entry = self._entries.get(_key(SESSION_ABS_NS, token))
        if abs_entry is None:
            return None
        abs_expires_at, _ = abs_entry
        abs_remaining = abs_expires_at - now
        if abs_remaining <= 0:
            return None
        sess_key = _key(SESSION_NS, token)
        sess_entry = self._entries.get(sess_key)
        if sess_entry is None:
            return None
        _, data = sess_entry
        new_ttl = min(idle_ttl, int(abs_remaining))
        self._entries[sess_key] = (now + new_ttl, data)
        return new_ttl

    async def absolute_remaining(self, token: str) -> int | None:
        abs_entry = self._entries.get(_key(SESSION_ABS_NS, token))
        if abs_entry is None:
            return None
        abs_expires_at, _ = abs_entry
        abs_remaining = abs_expires_at - time.monotonic()
        if abs_remaining <= 0:
            return None
        return int(abs_remaining)

    async def delete_session(self, token: str) -> None:
        self._entries.pop(_key(SESSION_NS, token), None)
        self._entries.pop(_key(SESSION_ABS_NS, token), None)


class RedisSessionStore:
    """Production. SETEX on put (TTL = session/challenge lifetime), DEL on
    logout/consume; Redis expiry handles auto-logoff."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def put(self, namespace: str, data: SessionData, ttl_seconds: int) -> str:
        token = _new_token()
        await self._redis.set(_key(namespace, token), data.to_json(), ex=ttl_seconds)
        return token

    async def get(self, namespace: str, token: str) -> SessionData | None:
        raw = await self._redis.get(_key(namespace, token))
        if raw is None:
            return None
        return SessionData.from_json(raw.decode() if isinstance(raw, bytes) else raw)

    async def delete(self, namespace: str, token: str) -> None:
        await self._redis.delete(_key(namespace, token))

    async def mint_session(self, data: SessionData, idle_ttl: int, abs_ttl: int) -> str:
        token = _new_token()
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(_key(SESSION_NS, token), data.to_json(), ex=idle_ttl)
            # The companion value is irrelevant — only its TTL matters.
            pipe.set(_key(SESSION_ABS_NS, token), "1", ex=abs_ttl)
            await pipe.execute()
        return token

    async def extend_session(self, token: str, idle_ttl: int) -> int | None:
        # TTL: positive seconds remaining; -1 (no expiry, never happens here) / -2 (no key).
        abs_remaining = await self._redis.ttl(_key(SESSION_ABS_NS, token))
        if abs_remaining <= 0:
            return None
        new_ttl = min(idle_ttl, abs_remaining)
        # Sub-millisecond TOCTOU: sess_abs could expire between the ttl() read above and the
        # expire() write below. This is benign — new_ttl was <= abs_remaining at read time, so
        # sess never outlives the cap; the next verify will 401 within ~1 s of the true cap.
        # Strict zero-window enforcement would require a Lua compare-and-expire; the ~1 s skew
        # is acceptable here and the added complexity is deliberately avoided.
        extended = await self._redis.expire(_key(SESSION_NS, token), new_ttl)
        if not extended:  # sess key already gone (idle-expired)
            return None
        return new_ttl

    async def absolute_remaining(self, token: str) -> int | None:
        # TTL: positive seconds remaining; -1 (no expiry, never happens here) / -2 (no key).
        abs_remaining = await self._redis.ttl(_key(SESSION_ABS_NS, token))
        if abs_remaining <= 0:
            return None
        return abs_remaining

    async def delete_session(self, token: str) -> None:
        await self._redis.delete(_key(SESSION_NS, token), _key(SESSION_ABS_NS, token))


class SessionVerifier:
    """Resolves a bearer session token to a VerifiedIdentity. Only accepts
    fully-authenticated "sess" tokens — a pending-MFA challenge is rejected."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def verify(self, token: str) -> VerifiedIdentity:
        data = await self._store.get(SESSION_NS, token)
        if data is None or not data.mfa_passed:
            raise InvalidTokenError("invalid or expired session")
        return VerifiedIdentity(
            user_id=data.user_id,
            subject=data.subject,
            email=data.email,
            tenant_id=data.tenant_id,
            account_type=AccountType(data.account_type),
            tenant_slug=data.tenant_slug,
            session_id=data.session_id,
        )
