import asyncio

import pytest

from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_USER,
    InMemoryTranscriptStore,
    TranscriptService,
    transcript_stream_key,
)


def test_stream_key_pattern() -> None:
    assert transcript_stream_key("call--t--c") == "vera:transcript:call--t--c"


def _service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())


@pytest.mark.asyncio
async def test_publish_then_collect_in_order() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "hi", ts=1)
    await svc.publish_turn("room", ROLE_AGENT, "hello", ts=2)
    await svc.end("room")
    got = await svc.collect("room")
    assert [(e.role, e.text) for e in got] == [(ROLE_USER, "hi"), (ROLE_AGENT, "hello")]


@pytest.mark.asyncio
async def test_consume_tails_live_then_ends() -> None:
    svc = _service()
    seen: list[str] = []

    async def consume() -> None:
        async for _id, event in svc.consume("room"):
            seen.append(event.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # reader starts and blocks (stream empty)
    await svc.publish_turn("room", ROLE_USER, "live", ts=1)
    await svc.end("room")
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == ["live"]


@pytest.mark.asyncio
async def test_consume_yields_unique_entry_ids() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "a", ts=1)
    await svc.publish_turn("room", ROLE_USER, "b", ts=2)
    await svc.end("room")
    ids = [entry_id async for entry_id, _e in svc.consume("room")]
    assert len(ids) == len(set(ids)) == 2
