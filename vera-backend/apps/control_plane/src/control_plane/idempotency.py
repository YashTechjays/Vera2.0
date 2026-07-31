"""Idempotency-Key handling for mutating ingress (API Contract §7 / ADR
vera2-database-design §707).

Redis holds a short-lived **in-flight lock** per (tenant, key): the first request
presenting a given `Idempotency-Key` claims the lock and proceeds; a concurrent
retry that arrives while the original is still in flight is rejected with 409 so it
cannot race a second resource into existence. The lock auto-expires after a short
TTL (the request horizon — seconds), keyed `vera:idem:<tenant_id>:<key>`.

Durable de-duplication of *late* retries is **not** Redis's job — it is enforced by
a UNIQUE constraint on the resource's natural key (e.g. the external ref) in
Postgres, the authoritative backstop. Redis only narrows the concurrent-duplicate
race; it never remembers a resource id, so there is no cross-store record to keep
consistent. Tenant isolation is by key prefix (this is not Postgres; no RLS).

Usage inside a mutating route::

    key = Depends(require_idempotency_key)
    await claim_or_conflict(store, tenant_id, key, settings.idempotency_lock_ttl_seconds)
    # ... create the resource; a UNIQUE violation from a late retry maps to a
    #     duplicate response at the route (the durable backstop) ...

`claim_or_conflict` keeps the lock for the whole TTL even when the request then fails,
which locks the caller out of their own corrected resubmit — see `idempotency_guard`.
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Header
from redis.asyncio import Redis

from control_plane.exceptions import CustomAPIException, ExceptionCode

logger = logging.getLogger(__name__)

IDEM_NS = "idem"
_LOCKED = "1"

# Platform (tenant-less) callers namespace their locks under the nil UUID; uuid7 never
# generates it, so it cannot collide with a real tenant's namespace.
PLATFORM_IDEM_SCOPE = UUID(int=0)


def _key(tenant_id: UUID, user_id: UUID, key: str) -> str:
    return f"vera:{IDEM_NS}:{tenant_id}:{user_id}:{key}"


class IdempotencyStore(Protocol):
    async def claim(self, tenant_id: UUID, user_id: UUID, key: str, ttl_seconds: int) -> bool:
        """Atomically take the in-flight lock (set iff absent). True if this caller
        now owns the operation; False if a request with this key is in flight."""
        ...

    async def release(self, tenant_id: UUID, user_id: UUID, key: str) -> None:
        """Drop a lock this caller claimed."""
        ...


class InMemoryIdempotencyStore:
    """Dev/tests. Monotonic-clock TTL; locks vanish on restart."""

    def __init__(self) -> None:
        self._locks: dict[str, float] = {}

    async def claim(self, tenant_id: UUID, user_id: UUID, key: str, ttl_seconds: int) -> bool:
        k = _key(tenant_id, user_id, key)
        now = time.monotonic()
        expires_at = self._locks.get(k)
        if expires_at is not None and now < expires_at:
            return False
        self._locks[k] = now + ttl_seconds
        return True

    async def release(self, tenant_id: UUID, user_id: UUID, key: str) -> None:
        self._locks.pop(_key(tenant_id, user_id, key), None)


class RedisIdempotencyStore:
    """Production. `SET NX EX` is the atomic claim; Redis expiry releases the lock."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(self, tenant_id: UUID, user_id: UUID, key: str, ttl_seconds: int) -> bool:
        claimed = await self._redis.set(
            _key(tenant_id, user_id, key), _LOCKED, nx=True, ex=ttl_seconds
        )
        return bool(claimed)

    async def release(self, tenant_id: UUID, user_id: UUID, key: str) -> None:
        await self._redis.delete(_key(tenant_id, user_id, key))


async def claim_or_conflict(
    store: IdempotencyStore, tenant_id: UUID, user_id: UUID, key: str, ttl_seconds: int
) -> None:
    """Take the in-flight lock, or reject the request as a concurrent duplicate (409)."""
    if not await store.claim(tenant_id, user_id, key, ttl_seconds):
        raise CustomAPIException(ExceptionCode.IDEMPOTENCY_CONFLICT)


@asynccontextmanager
async def idempotency_guard(
    store: IdempotencyStore, tenant_id: UUID, user_id: UUID, key: str, ttl_seconds: int
) -> AsyncIterator[None]:
    """`claim_or_conflict` that frees the key again if the body raises.

    ONLY for a body whose every effect is inside the request transaction: release
    means "the body raised", which stands in for "nothing was created". A body that
    also writes Redis, sends mail, or dispatches work breaks that equivalence — its
    retry would repeat the side effect — so those keep the bare `claim_or_conflict`.
    """
    await claim_or_conflict(store, tenant_id, user_id, key, ttl_seconds)
    try:
        yield
    except BaseException:
        # Best-effort: the TTL is the backstop, so a failed release must never
        # replace the caller's error with a Redis one. Type name only (PHI rule).
        try:
            await store.release(tenant_id, user_id, key)
        except Exception as exc:
            logger.warning("idempotency release failed: %s", type(exc).__name__)
        raise


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Dependency for mutating-ingress routes: the `Idempotency-Key` header is
    mandatory. Declared as a typed `Header` param (not read off `Request`) so it
    shows up as an input in the OpenAPI/Swagger docs — kept optional here only so a
    miss returns the custom 400 envelope rather than a generic 422."""
    if not idempotency_key:
        raise CustomAPIException(ExceptionCode.MISSING_IDEMPOTENCY_KEY)
    return idempotency_key
