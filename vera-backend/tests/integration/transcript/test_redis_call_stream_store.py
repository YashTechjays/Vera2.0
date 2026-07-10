import asyncio
from collections.abc import AsyncGenerator

import pytest
from redis.asyncio import Redis

from vera_core.call_stream import (
    CallStreamService,
    RedisCallStreamStore,
    call_stream_key,
)
from vera_core.redis import create_redis

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def svc() -> AsyncGenerator[tuple[CallStreamService, Redis], None]:
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(call_stream_key("itroom"))
    service = CallStreamService(RedisCallStreamStore(redis, ttl_seconds=3600, end_grace_seconds=60))
    yield service, redis
    await redis.delete(call_stream_key("itroom"))
    await redis.aclose()


async def test_publish_replay_and_end(svc: tuple[CallStreamService, Redis]) -> None:
    service, _redis = svc
    await service.publish_turn("itroom", "user", "hi", ts=1)
    await service.publish_status("itroom", "active", ts=2)
    await service.end("itroom")
    got = [event async for _id, event in service.consume("itroom")]
    assert [(e.type, e.data) for e in got] == [
        ("transcript", {"role": "user", "text": "hi"}),
        ("call_status", {"status": "active"}),
    ]


async def test_publish_sets_backstop_ttl(svc: tuple[CallStreamService, Redis]) -> None:
    service, redis = svc
    await service.publish_turn("itroom", "user", "hi", ts=1)
    ttl = await redis.ttl(call_stream_key("itroom"))
    assert 0 < ttl <= 3600


async def test_consume_tails_live(svc: tuple[CallStreamService, Redis]) -> None:
    service, _redis = svc
    seen: list[str] = []

    async def consume() -> None:
        async for _id, e in service.consume("itroom"):
            seen.append(e.type)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await service.publish_turn("itroom", "user", "live", ts=1)
    await service.end("itroom")
    await asyncio.wait_for(task, timeout=2.0)
    assert seen == ["transcript"]


async def test_exists_false_before_publish_true_after(svc: tuple[CallStreamService, Redis]) -> None:
    service, _redis = svc
    assert await service.exists("itroom") is False
    await service.publish_turn("itroom", "user", "hi", ts=1)
    assert await service.exists("itroom") is True


async def test_exists_false_after_delete(svc: tuple[CallStreamService, Redis]) -> None:
    service, _redis = svc
    await service.publish_turn("itroom", "user", "hi", ts=1)
    await service.clear("itroom")
    assert await service.exists("itroom") is False


async def test_read_terminates_at_deadline_when_stream_never_appears() -> None:
    """A never-seen stream (nothing has ever been published) must not tail forever
    — the first-entry deadline bounds the "will anything ever show up" wait."""
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(call_stream_key("deadlineroom"))
    service = CallStreamService(
        RedisCallStreamStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=50)
    )
    try:
        seen: list[str] = []
        async for _id, e in service.consume("deadlineroom", first_entry_deadline_s=0.15):
            seen.append(e.type)
        assert seen == []
    finally:
        await redis.delete(call_stream_key("deadlineroom"))
        await redis.aclose()


async def test_read_none_deadline_preserves_indefinite_tailing() -> None:
    """`first_entry_deadline_s=None` (the default) never gives up waiting for the
    first entry — today's behavior, unchanged."""
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(call_stream_key("nodeadlineroom"))
    service = CallStreamService(
        RedisCallStreamStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=50)
    )
    seen: list[str] = []

    async def consume() -> None:
        async for _id, e in service.consume("nodeadlineroom", first_entry_deadline_s=None):
            seen.append(e.type)

    task = asyncio.create_task(consume())
    try:
        # Sit idle well past what a small deadline would have allowed — the
        # tail must still be alive because no deadline was set.
        await asyncio.sleep(0.3)
        assert not task.done()
        await service.publish_turn("nodeadlineroom", "user", "late", ts=1)
        await service.end("nodeadlineroom")
        await asyncio.wait_for(task, timeout=2.0)
        assert seen == ["transcript"]
    finally:
        if not task.done():
            task.cancel()
        await redis.delete(call_stream_key("nodeadlineroom"))
        await redis.aclose()


async def test_read_deadline_does_not_cut_off_a_seen_stream() -> None:
    """Once at least one entry has been seen, the deadline no longer applies — a
    long-running live call must not be cut off mid-tail."""
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(call_stream_key("seenroom"))
    service = CallStreamService(
        RedisCallStreamStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=50)
    )
    seen: list[str] = []

    async def consume() -> None:
        async for _id, e in service.consume("seenroom", first_entry_deadline_s=0.15):
            seen.append(e.type)

    await service.publish_turn("seenroom", "user", "first", ts=1)  # seen=True immediately
    task = asyncio.create_task(consume())
    try:
        # Outlive the deadline while idle — a seen stream must not be cut off.
        await asyncio.sleep(0.3)
        assert not task.done()
        await service.publish_status("seenroom", "ended", ts=2)
        await service.end("seenroom")
        await asyncio.wait_for(task, timeout=2.0)
        assert seen == ["transcript", "call_status"]
    finally:
        if not task.done():
            task.cancel()
        await redis.delete(call_stream_key("seenroom"))
        await redis.aclose()


async def test_consume_survives_idle_block_timeout() -> None:
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(call_stream_key("idleroom"))
    service = CallStreamService(
        RedisCallStreamStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=100)
    )
    seen: list[str] = []

    async def consume() -> None:
        async for _id, e in service.consume("idleroom"):
            seen.append(e.type)

    await service.publish_turn("idleroom", "user", "first", ts=1)  # stream now exists
    task = asyncio.create_task(consume())
    try:
        # Sit idle across several block windows so XREAD BLOCK times out repeatedly.
        await asyncio.sleep(0.5)
        assert not task.done()  # the idle timeouts did not crash / close the reader
        await service.publish_status("idleroom", "ended", ts=2)
        await service.end("idleroom")
        await asyncio.wait_for(task, timeout=2.0)
        assert seen == ["transcript", "call_status"]
    finally:
        if not task.done():
            task.cancel()
        await redis.delete(call_stream_key("idleroom"))
        await redis.aclose()
