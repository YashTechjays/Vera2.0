"""Per-call rate limiting for coaching mode (proposal §"Rate limit").

A fixed rolling window, ONE Redis counter per `call_id` — not per (call_id,
supervisor_id) — shared by every supervisor coaching that call AND by the
whisper transcribe endpoint (the confirmed product decision: coaching and
whisper draw from the same combined budget, not two separate ones). `INCR` +
`EXPIRE` on the first hit is the atomic window: the counter self-clears after
`window_seconds`, so there is nothing to reset or garbage-collect.
"""

import time
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode

_KEY_PREFIX = "vera:coach-rl:"


def _key(call_id: UUID) -> str:
    return f"{_KEY_PREFIX}{call_id}"


class CallRateLimiter(Protocol):
    async def check_and_increment(self, call_id: UUID) -> bool:
        """Record one action against *call_id*'s window; True if it's within the
        limit, False if this action must be rejected."""
        ...


class InMemoryCallRateLimiter:
    """Dev/tests. Monotonic-clock window; counters vanish on restart."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._windows: dict[UUID, tuple[float, int]] = {}

    async def check_and_increment(self, call_id: UUID) -> bool:
        now = time.monotonic()
        started_at, count = self._windows.get(call_id, (now, 0))
        if now - started_at >= self._window_seconds:
            started_at, count = now, 0
        count += 1
        self._windows[call_id] = (started_at, count)
        return count <= self._limit


class RedisCallRateLimiter:
    """Production. `INCR` + `EXPIRE` (only on the window's first hit) is the
    atomic rolling window — a single round trip per action."""

    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds

    async def check_and_increment(self, call_id: UUID) -> bool:
        key = _key(call_id)
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, self._window_seconds)
        return count <= self._limit


async def check_rate_limit(limiter: CallRateLimiter, call_id: UUID) -> None:
    """Record the action, or reject it as over the shared coaching/whisper budget (429)."""
    if not await limiter.check_and_increment(call_id):
        raise CustomAPIException(
            DefaultExceptionCode.RATE_LIMIT_EXCEEDED,
            message="too many coaching/whisper actions on this call — slow down",
        )
