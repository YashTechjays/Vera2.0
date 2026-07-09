"""drain(): non-blocking snapshot for the finalizer — [] on a missing stream,
sentinel excluded, order preserved."""

from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


async def test_drain_missing_stream_returns_empty() -> None:
    service = TranscriptService(InMemoryTranscriptStore())
    assert await service.drain("no-such-room") == []


async def test_drain_returns_all_turns_excluding_sentinel() -> None:
    service = TranscriptService(InMemoryTranscriptStore())
    await service.publish_turn("room", "user", "[[NAME_1]] calling", ts=1)
    await service.publish_turn("room", "agent", "hello [[NAME_1]]", ts=2)
    await service.end("room")
    events = await service.drain("room")
    assert [(e.role, e.text) for e in events] == [
        ("user", "[[NAME_1]] calling"),
        ("agent", "hello [[NAME_1]]"),
    ]
