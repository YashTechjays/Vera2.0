"""Unit tests for the coaching/whisper rate limiter — one shared window per
call_id (not per supervisor), per the confirmed product decision."""

from typing import cast
from uuid import uuid4

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from control_plane.rate_limit import (
    InMemoryCallRateLimiter,
    InMemoryPasswordResetRateLimiter,
    RedisCallRateLimiter,
    RedisPasswordResetRateLimiter,
    _key,
    _reset_key,
    check_rate_limit,
)


@pytest.fixture
def redis() -> Redis:
    return cast(Redis, fakeredis.aioredis.FakeRedis(decode_responses=True))


@pytest.mark.asyncio
async def test_allows_actions_up_to_the_limit() -> None:
    limiter = InMemoryCallRateLimiter(limit=3, window_seconds=60)
    call_id = uuid4()

    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is False  # 4th in the same window


@pytest.mark.asyncio
async def test_the_window_is_shared_across_the_same_call_not_split_per_caller() -> None:
    """Two different supervisors coaching the same call draw from ONE budget —
    the limiter has no notion of "who," only "which call."""
    limiter = InMemoryCallRateLimiter(limit=2, window_seconds=60)
    call_id = uuid4()

    assert await limiter.check_and_increment(call_id) is True  # supervisor A
    assert await limiter.check_and_increment(call_id) is True  # supervisor B
    assert await limiter.check_and_increment(call_id) is False  # supervisor A again — over


@pytest.mark.asyncio
async def test_different_calls_have_independent_windows() -> None:
    limiter = InMemoryCallRateLimiter(limit=1, window_seconds=60)
    call_a, call_b = uuid4(), uuid4()

    assert await limiter.check_and_increment(call_a) is True
    assert await limiter.check_and_increment(call_a) is False  # call_a exhausted
    assert await limiter.check_and_increment(call_b) is True  # call_b unaffected


@pytest.mark.asyncio
async def test_check_rate_limit_raises_429_over_the_limit() -> None:
    limiter = InMemoryCallRateLimiter(limit=1, window_seconds=60)
    call_id = uuid4()
    await check_rate_limit(limiter, call_id)  # first action: fine

    with pytest.raises(CustomAPIException) as exc_info:
        await check_rate_limit(limiter, call_id)

    assert exc_info.value.code is DefaultExceptionCode.RATE_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_reset_limiter_allows_requests_up_to_the_limit() -> None:
    limiter = InMemoryPasswordResetRateLimiter(limit=3, window_seconds=900)

    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is False  # 4th in the same window


@pytest.mark.asyncio
async def test_reset_limiter_keys_have_independent_windows() -> None:
    limiter = InMemoryPasswordResetRateLimiter(limit=1, window_seconds=900)

    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is False  # k1 exhausted
    assert await limiter.check_and_increment("k2") is True  # k2 unaffected


@pytest.mark.asyncio
async def test_redis_reset_limiter_counts_and_sets_ttl_atomically(redis: Redis) -> None:
    limiter = RedisPasswordResetRateLimiter(redis, limit=2, window_seconds=900)

    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is True
    assert await limiter.check_and_increment("k1") is False
    ttl = await redis.ttl(_reset_key("k1"))
    assert 0 < ttl <= 900


@pytest.mark.asyncio
async def test_redis_limiter_allows_actions_up_to_the_limit(redis: Redis) -> None:
    limiter = RedisCallRateLimiter(redis, limit=3, window_seconds=60)
    call_id = uuid4()

    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is True
    assert await limiter.check_and_increment(call_id) is False  # 4th in the same window


@pytest.mark.asyncio
async def test_redis_limiter_sets_a_ttl_on_the_first_hit(redis: Redis) -> None:
    """The INCR+EXPIRE must land as one atomic step — a key that's incremented
    but never expires would permanently lock the call out of coaching."""
    limiter = RedisCallRateLimiter(redis, limit=5, window_seconds=60)
    call_id = uuid4()

    await limiter.check_and_increment(call_id)

    ttl = await redis.ttl(_key(call_id))
    assert 0 < ttl <= 60


@pytest.mark.asyncio
async def test_redis_limiter_does_not_reset_the_ttl_on_later_hits(redis: Redis) -> None:
    limiter = RedisCallRateLimiter(redis, limit=5, window_seconds=60)
    call_id = uuid4()
    key = _key(call_id)

    await limiter.check_and_increment(call_id)
    await redis.expire(key, 1)  # simulate time passing within the window
    await limiter.check_and_increment(call_id)

    assert await redis.ttl(key) <= 1
