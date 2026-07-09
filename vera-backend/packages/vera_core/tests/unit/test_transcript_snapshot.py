import pytest

from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


@pytest.mark.asyncio
async def test_snapshot_returns_published_turns_in_order():
    store = InMemoryTranscriptStore()
    svc = TranscriptService(store)
    await svc.publish_turn("room1", "user", "hello", ts=1)
    await svc.publish_turn("room1", "agent", "hi there", ts=2)
    await svc.end("room1")  # sentinel present; snapshot must ignore it

    turns = await svc.snapshot("room1")

    assert [(t.role, t.text) for t in turns] == [("user", "hello"), ("agent", "hi there")]


@pytest.mark.asyncio
async def test_snapshot_of_missing_stream_is_empty():
    svc = TranscriptService(InMemoryTranscriptStore())
    assert await svc.snapshot("nope") == []
