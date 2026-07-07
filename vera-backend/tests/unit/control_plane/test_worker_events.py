"""Unit tests for the control-plane worker-event consumer (no live Redis)."""

from uuid import uuid4

import pytest

from control_plane.worker_events import WorkerEventConsumer
from vera_core.events import CallFailedEvent, CallFailureReason

_VALID_ROOM = f"call--{uuid4()}--{uuid4()}"


class _FakeLiveKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_metadata = False

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        if self.fail_metadata:
            raise RuntimeError("boom")
        self.calls.append(("meta", (room_name, metadata)))

    async def delete_room(self, room_name: str) -> None:
        self.calls.append(("delete", room_name))


class _FakeRedis:
    def __init__(self) -> None:
        self.acked: list[str] = []

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        return 1


def _consumer(redis: _FakeRedis, livekit: _FakeLiveKit) -> WorkerEventConsumer:
    return WorkerEventConsumer(redis, livekit, teardown_grace_ms=0)  # type: ignore[arg-type]


def _event_fields(room: str = _VALID_ROOM) -> dict[str, str]:
    ev = CallFailedEvent(room_name=room, reason=CallFailureReason.NO_ANSWER, ts=1)
    return {"event": ev.model_dump_json()}


@pytest.mark.asyncio
async def test_handle_call_failed_sets_metadata_then_deletes() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("1-0", _event_fields())
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_ignores_non_vera_room() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("2-0", _event_fields(room="lobby"))
    assert livekit.calls == []  # not torn down
    assert redis.acked == ["2-0"]  # but acked (nothing to retry)


@pytest.mark.asyncio
async def test_missing_event_field_is_acked_and_skipped() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("3-0", {"nope": "x"})
    assert livekit.calls == []
    assert redis.acked == ["3-0"]


@pytest.mark.asyncio
async def test_unparseable_event_is_dropped() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    await _consumer(redis, livekit)._process("4-0", {"event": "{not json"})
    assert livekit.calls == []
    assert redis.acked == ["4-0"]


@pytest.mark.asyncio
async def test_handler_failure_leaves_entry_unacked() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    livekit.fail_metadata = True
    await _consumer(redis, livekit)._process("5-0", _event_fields())
    assert redis.acked == []  # left pending for XAUTOCLAIM to retry


@pytest.mark.asyncio
async def test_unknown_event_type_is_acked_and_skipped() -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    consumer = _consumer(redis, livekit)
    consumer._handlers = {}  # remove all handlers so call.failed has no handler
    await consumer._process("6-0", _event_fields())
    assert livekit.calls == []  # no teardown
    assert redis.acked == ["6-0"]  # entry is acked despite no handler
