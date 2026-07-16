"""CallLifecycleEmitter — the worker's answered/ended signals to the control plane."""

import pytest

from agent_worker.main import CallLifecycleEmitter
from vera_core.events import CallAnsweredEvent, CallEndedEvent, WorkerEvent


class _FakeBus:
    def __init__(self) -> None:
        self.emitted: list[WorkerEvent] = []

    async def emit(self, event: WorkerEvent) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_answered_then_ended_emit_in_order() -> None:
    bus = _FakeBus()
    emitter = CallLifecycleEmitter(bus, "call--t--c")  # type: ignore[arg-type]
    await emitter.answered(now_ms=100)
    await emitter.ended(now_ms=200)
    assert isinstance(bus.emitted[0], CallAnsweredEvent)
    assert isinstance(bus.emitted[1], CallEndedEvent)
    assert bus.emitted[0].room_name == "call--t--c"
    assert bus.emitted[0].ts == 100


@pytest.mark.asyncio
async def test_emit_failures_never_raise() -> None:
    class _Boom:
        async def emit(self, event: WorkerEvent) -> None:
            raise RuntimeError("redis down")

    emitter = CallLifecycleEmitter(_Boom(), "call--t--c")  # type: ignore[arg-type]
    await emitter.answered(now_ms=1)  # must not raise — never break a live call
    await emitter.ended(now_ms=2)
