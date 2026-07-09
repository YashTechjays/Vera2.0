"""CallStreamEvent envelope + service semantics (in-memory store variant)."""

from collections.abc import AsyncIterator

import pytest

from vera_core.call_stream import CallStreamEvent, CallStreamService, call_stream_key


class _MemStore:
    """Minimal in-memory store capturing publishes; read replays then stops on end."""

    def __init__(self) -> None:
        self.events: list[CallStreamEvent] = []
        self.ended = False

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.events.append(event)

    async def mark_ended(self, room_name: str) -> None:
        self.ended = True

    async def delete(self, room_name: str) -> None:
        self.events.clear()

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        for i, event in enumerate(self.events):
            yield (f"{i}-0", event)


def test_key_prefix() -> None:
    assert call_stream_key("call--t--c") == "vera:call-events:call--t--c"


@pytest.mark.asyncio
async def test_publish_turn_wraps_transcript_envelope() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "agent", "hello", ts=42)
    assert store.events == [
        CallStreamEvent(type="transcript", data={"role": "agent", "text": "hello"}, ts=42)
    ]


@pytest.mark.asyncio
async def test_publish_status_wraps_call_status_envelope() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_status("r", "active", ts=7)
    assert store.events == [CallStreamEvent(type="call_status", data={"status": "active"}, ts=7)]


@pytest.mark.asyncio
async def test_consume_yields_envelope_events() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "user", "hi", ts=1)
    got = [e async for _id, e in svc.consume("r")]
    assert got[0].type == "transcript" and got[0].data["text"] == "hi"
