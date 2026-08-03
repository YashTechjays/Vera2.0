"""Per-call rate limiting for coaching mode (proposal §"Rate limit").

A fixed rolling window, ONE Redis counter per `call_id` — not per (call_id,
supervisor_id) — shared by every supervisor coaching that call AND by the
whisper transcribe endpoint (the confirmed product decision: coaching and
whisper draw from the same combined budget, not two separate ones). `INCR`
and `EXPIRE ... NX` (only sets the TTL if the key doesn't already have one)
run together in one `MULTI`/`EXEC` transaction — a single round trip, so a
cancelled/killed request can never observe the increment without the expiry
(two separate awaits, INCR then a conditional EXPIRE, would leave an
orphaned, never-expiring key if the process died in between).
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
    """Production. `INCR` + `EXPIRE ... NX` run in one `MULTI`/`EXEC`
    transaction (see module docstring) — a single round trip per action."""

    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds

    async def check_and_increment(self, call_id: UUID) -> bool:
        key = _key(call_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self._window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= self._limit


# Password-reset throttle keys are sha256(slug:email) — no raw email in Redis, and
# unknown emails consume budget too; over-limit is a silent drop, never a 429.
_RESET_KEY_PREFIX = "vera:pwreset-rl:"


def _reset_key(key: str) -> str:
    return f"{_RESET_KEY_PREFIX}{key}"


class PasswordResetRateLimiter(Protocol):
    async def check_and_increment(self, key: str) -> bool:
        """Record one reset request against *key*'s window; True if within the limit."""
        ...


class InMemoryPasswordResetRateLimiter:
    """Dev/tests. Monotonic-clock window; counters vanish on restart."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = {}

    async def check_and_increment(self, key: str) -> bool:
        now = time.monotonic()
        started_at, count = self._windows.get(key, (now, 0))
        if now - started_at >= self._window_seconds:
            started_at, count = now, 0
        count += 1
        self._windows[key] = (started_at, count)
        return count <= self._limit


class RedisPasswordResetRateLimiter:
    """Production. Same atomic `INCR` + `EXPIRE ... NX` shape as the coaching
    limiter (see module docstring)."""

    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds

    async def check_and_increment(self, key: str) -> bool:
        redis_key = _reset_key(key)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, self._window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= self._limit


async def check_rate_limit(limiter: CallRateLimiter, call_id: UUID) -> None:
    """Record the action, or reject it as over the shared coaching/whisper budget (429)."""
    if not await limiter.check_and_increment(call_id):
        raise CustomAPIException(
            DefaultExceptionCode.RATE_LIMIT_EXCEEDED,
            message="too many coaching/whisper actions on this call — slow down",
        )
