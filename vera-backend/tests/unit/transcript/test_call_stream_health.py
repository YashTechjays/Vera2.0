"""The health envelope rides the same per-call stream as transcript/status."""

import pytest

from vera_core.call_stream import TYPE_HEALTH, CallStreamEvent, CallStreamService


class _SpyStore:
    def __init__(self) -> None:
        self.published: list[tuple[str, CallStreamEvent]] = []

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.published.append((room_name, event))


@pytest.mark.asyncio
async def test_publish_health_envelope() -> None:
    store = _SpyStore()
    service = CallStreamService(store)  # type: ignore[arg-type]
    await service.publish_health("room-1", score=35, flag="long_silence", reason="hold", ts=99)
    [(room, event)] = store.published
    assert room == "room-1"
    assert event.type == TYPE_HEALTH == "health"
    assert event.data == {"score": 35, "flag": "long_silence", "reason": "hold"}
    assert event.ts == 99
