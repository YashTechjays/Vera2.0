from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from control_plane.post_call_consumer import build_turns
from vera_core.call_stream import CallStreamEvent, CallStreamService
from vera_core.integrations.llm import TranscriptTurn
from vera_core.observability.correlation import room_name_for_call


class _MemStore:
    """Minimal CallStreamStore: publish + read_all are all build_turns touches."""

    def __init__(self) -> None:
        self._events: dict[str, list[CallStreamEvent]] = {}

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self._events.setdefault(room_name, []).append(event)

    async def mark_ended(self, room_name: str) -> None:
        return None

    async def delete(self, room_name: str) -> None:
        self._events.pop(room_name, None)

    async def exists(self, room_name: str) -> bool:
        return room_name in self._events

    async def read(
        self, room_name: str, *, first_entry_deadline_s: float | None = None
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
        for i, event in enumerate(self._events.get(room_name, [])):
            yield f"{i}-0", event

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return list(self._events.get(room_name, []))


@pytest.mark.asyncio
async def test_build_turns_enumerates_transcript_events() -> None:
    svc = CallStreamService(_MemStore())
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    await svc.publish_turn(room, "user", "hello", ts=1)
    await svc.publish_turn(room, "agent", "in network", ts=2)

    turns = await build_turns(svc, tenant_id, call_id)

    assert turns == [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]


@pytest.mark.asyncio
async def test_build_turns_skips_non_transcript_frames() -> None:
    svc = CallStreamService(_MemStore())
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    await svc.publish_status(room, "in_progress", ts=1)
    await svc.publish_turn(room, "user", "hello", ts=2)
    await svc.publish_health(room, score=80, flag="ok", reason="", ts=3)

    turns = await build_turns(svc, tenant_id, call_id)

    assert turns == [TranscriptTurn(0, "user", "hello")]


@pytest.mark.asyncio
async def test_build_turns_of_missing_stream_is_empty() -> None:
    svc = CallStreamService(_MemStore())
    assert await build_turns(svc, uuid4(), uuid4()) == []
