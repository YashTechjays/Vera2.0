"""Effective-permission cache, keyed (scope, subject).

`scope` is the tenant UUID for a tenant user, or `None` for the platform
(no-tenant) scope of a SUPER_ADMIN — the cache handles the null scope explicitly,
so callers never invent a sentinel key. Short TTL + explicit invalidation on
role/permission writes. Production uses Memorystore Redis behind the same
protocol; local dev and tests use the in-memory implementation.
"""

import json
import time
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

Scope = UUID | None
Key = tuple[Scope, str]


class PermissionCache(Protocol):
    async def get(self, scope: Scope, subject: str) -> frozenset[str] | None: ...
    async def set(self, scope: Scope, subject: str, permissions: frozenset[str]) -> None: ...
    async def invalidate(self, scope: Scope, subject: str) -> None: ...


class InMemoryPermissionCache:
    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[Key, tuple[float, frozenset[str]]] = {}

    async def get(self, scope: Scope, subject: str) -> frozenset[str] | None:
        entry = self._entries.get((scope, subject))
        if entry is None:
            return None
        expires_at, permissions = entry
        if time.monotonic() >= expires_at:
            del self._entries[(scope, subject)]
            return None
        return permissions

    async def set(self, scope: Scope, subject: str, permissions: frozenset[str]) -> None:
        self._entries[(scope, subject)] = (time.monotonic() + self._ttl, permissions)

    async def invalidate(self, scope: Scope, subject: str) -> None:
        self._entries.pop((scope, subject), None)


class RedisPermissionCache:
    """Memorystore-backed cache: SETEX on vera:perms:{scope}:{subject} with
    a short TTL, DEL on invalidation (called from role/user-role write paths).
    Stores only permission CODES (a JSON array) — never PHI."""

    def __init__(self, redis: Redis, ttl_seconds: float = 30.0) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds)

    @staticmethod
    def _key(scope: Scope, subject: str) -> str:
        return f"vera:perms:{'platform' if scope is None else scope}:{subject}"

    async def get(self, scope: Scope, subject: str) -> frozenset[str] | None:
        raw = await self._redis.get(self._key(scope, subject))
        if raw is None:
            return None
        return frozenset(json.loads(raw))

    async def set(self, scope: Scope, subject: str, permissions: frozenset[str]) -> None:
        await self._redis.set(
            self._key(scope, subject), json.dumps(sorted(permissions)), ex=self._ttl
        )

    async def invalidate(self, scope: Scope, subject: str) -> None:
        await self._redis.delete(self._key(scope, subject))
