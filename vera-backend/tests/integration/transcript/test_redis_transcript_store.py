import asyncio
from collections.abc import AsyncGenerator

import pytest
from redis.asyncio import Redis

from vera_core.redis import create_redis
from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_USER,
    RedisTranscriptStore,
    TranscriptEvent,
    TranscriptService,
    transcript_stream_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def svc() -> AsyncGenerator[tuple[TranscriptService, Redis], None]:
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(transcript_stream_key("itroom"))
    service = TranscriptService(RedisTranscriptStore(redis, ttl_seconds=3600, end_grace_seconds=60))
    yield service, redis
    await redis.delete(transcript_stream_key("itroom"))
    await redis.aclose()


async def test_publish_replay_and_end(
    svc: tuple[TranscriptService, Redis],
) -> None:
    service, _redis = svc
    await service.publish_turn("itroom", ROLE_USER, "hi", ts=1)
    await service.publish_turn("itroom", ROLE_AGENT, "hello", ts=2)
    await service.end("itroom")
    got = await service.collect("itroom")
    assert [(e.role, e.text) for e in got] == [(ROLE_USER, "hi"), (ROLE_AGENT, "hello")]


async def test_publish_sets_backstop_ttl(
    svc: tuple[TranscriptService, Redis],
) -> None:
    service, redis = svc
    await service.publish_turn("itroom", ROLE_USER, "hi", ts=1)
    ttl = await redis.ttl(transcript_stream_key("itroom"))
    assert 0 < ttl <= 3600


async def test_consume_tails_live(svc: tuple[TranscriptService, Redis]) -> None:
    service, _redis = svc
    seen: list[str] = []

    async def consume() -> None:
        async for item in service.consume("itroom"):
            if item is not None:  # skip keepalive ticks
                seen.append(item[1].text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await service.publish_turn("itroom", ROLE_USER, "live", ts=1)
    await service.end("itroom")
    await asyncio.wait_for(task, timeout=2.0)
    assert seen == ["live"]


async def test_read_yields_keepalive_sentinel_on_idle_block_timeout() -> None:
    """Every idle XREAD BLOCK window surfaces a None keepalive tick so the SSE
    layer can emit a comment heartbeat (see the call-stream twin test)."""
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(transcript_stream_key("karoom"))
    service = TranscriptService(
        RedisTranscriptStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=50)
    )
    items: list[tuple[str, TranscriptEvent] | None] = []

    async def consume() -> None:
        async for item in service.consume("karoom"):
            items.append(item)

    await service.publish_turn("karoom", ROLE_USER, "first", ts=1)
    task = asyncio.create_task(consume())
    try:
        # Sit idle across several block windows; each must yield a keepalive tick.
        await asyncio.sleep(0.5)
        assert None in items
        await service.end("karoom")
        await asyncio.wait_for(task, timeout=2.0)
        assert [item[1].text for item in items if item is not None] == ["first"]
    finally:
        if not task.done():
            task.cancel()
        await redis.delete(transcript_stream_key("karoom"))
        await redis.aclose()


async def test_consume_survives_idle_block_timeout() -> None:
    # A live stream that goes idle between turns makes XREAD BLOCK raise redis
    # TimeoutError (redis-py 8 turns BLOCK into a read deadline). read() must tolerate
    # it and keep tailing rather than crash the SSE. tiny block_ms => fast idle windows.
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(transcript_stream_key("idleroom"))
    service = TranscriptService(
        RedisTranscriptStore(redis, ttl_seconds=3600, end_grace_seconds=60, block_ms=100)
    )
    seen: list[str] = []

    async def consume() -> None:
        async for item in service.consume("idleroom"):
            if item is not None:  # skip keepalive ticks
                seen.append(item[1].text)

    await service.publish_turn("idleroom", ROLE_USER, "first", ts=1)  # stream now exists
    task = asyncio.create_task(consume())
    try:
        # Sit idle across several block windows so XREAD BLOCK times out repeatedly.
        await asyncio.sleep(0.5)
        assert not task.done()  # the idle timeouts did not crash / close the reader
        await service.publish_turn("idleroom", ROLE_USER, "after-idle", ts=2)
        await service.end("idleroom")
        await asyncio.wait_for(task, timeout=2.0)
        assert seen == ["first", "after-idle"]
    finally:
        if not task.done():
            task.cancel()
        await redis.delete(transcript_stream_key("idleroom"))
        await redis.aclose()
