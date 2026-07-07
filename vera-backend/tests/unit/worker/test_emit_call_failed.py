"""The worker publishes a typed call.failed event when an outbound call fails."""

import pytest

from agent_worker.main import _emit_call_failed
from vera_core.events import CallFailedEvent, CallFailureReason, WorkerEvent, parse_worker_event


class _RecordingBus:
    def __init__(self) -> None:
        self.emitted: list[WorkerEvent] = []

    async def emit(self, event: WorkerEvent) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_emit_call_failed_publishes_typed_event() -> None:
    bus = _RecordingBus()
    await _emit_call_failed(
        bus,  # type: ignore[arg-type]
        "call--t--c",
        CallFailureReason.BUSY_OR_DECLINED,
        now_ms=1720000000000,
    )
    assert bus.emitted == [
        CallFailedEvent(
            room_name="call--t--c",
            reason=CallFailureReason.BUSY_OR_DECLINED,
            ts=1720000000000,
        )
    ]
    # And it is wire-serializable.
    assert parse_worker_event(bus.emitted[0].model_dump_json()) == bus.emitted[0]
