"""Unit tests for the coaching/whisper rate limiter — one shared window per
call_id (not per supervisor), per the confirmed product decision."""

from uuid import uuid4

import pytest

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from control_plane.rate_limit import InMemoryCallRateLimiter, check_rate_limit


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
