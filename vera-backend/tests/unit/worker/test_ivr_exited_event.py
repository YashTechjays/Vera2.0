"""The worker publishes ivr.exited when the navigator reaches a live human."""

import pytest

from agent_worker.main import CallLifecycleEmitter
from vera_core.events import IvrExitedEvent, WorkerEvent, parse_worker_event


def test_ivr_exited_round_trips_through_the_wire_format() -> None:
    event = IvrExitedEvent(room_name="call--t--c", ts=1720000000000)
    assert event.type == "ivr.exited"
    assert parse_worker_event(event.model_dump_json()) == event


class _RecordingBus:
    def __init__(self) -> None:
        self.emitted: list[WorkerEvent] = []

    async def emit(self, event: WorkerEvent) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_lifecycle_emitter_publishes_ivr_exited() -> None:
    bus = _RecordingBus()
    emitter = CallLifecycleEmitter(bus, "call--t--c")  # type: ignore[arg-type]
    await emitter.ivr_exited(now_ms=1720000000000)
    assert bus.emitted == [IvrExitedEvent(room_name="call--t--c", ts=1720000000000)]
