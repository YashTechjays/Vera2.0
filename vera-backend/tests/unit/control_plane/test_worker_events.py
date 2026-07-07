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
        # Configured per-test to mimic redis-py's decoded XREADGROUP/XAUTOCLAIM shapes.
        self.xreadgroup_response: object = None
        self.xautoclaim_response: object = ("0-0", [], [])
        # When set, xreadgroup raises it — used to mimic redis-py turning a BLOCK
        # window with no new entries into a raised TimeoutError.
        self.xreadgroup_error: Exception | None = None

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        return 1

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> object:
        if self.xreadgroup_error is not None:
            raise self.xreadgroup_error
        return self.xreadgroup_response

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> object:
        return self.xautoclaim_response


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


@pytest.mark.asyncio
async def test_read_once_unpacks_xreadgroup_response_and_dispatches() -> None:
    """Drives `_read_once` with a realistic decoded XREADGROUP shape (decode_responses=True,
    no `justid`): `[[stream_name, [(entry_id, fields), ...]]]`.
    """
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xreadgroup_response = [
        ["vera:worker-events", [("10-0", _event_fields())]],
    ]
    await _consumer(redis, livekit)._read_once()
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["10-0"]


@pytest.mark.asyncio
async def test_reclaim_stale_unpacks_xautoclaim_response_and_dispatches() -> None:
    """Drives `_reclaim_stale` with a realistic decoded XAUTOCLAIM shape:
    `(cursor, [(entry_id, fields), ...], deleted_ids)`.
    """
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xautoclaim_response = ("0-0", [("11-0", _event_fields())], [])
    await _consumer(redis, livekit)._reclaim_stale()
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["11-0"]


@pytest.mark.asyncio
async def test_read_once_treats_block_timeout_as_idle() -> None:
    """redis-py turns an XREADGROUP BLOCK window with no new entries into a raised
    redis.exceptions.TimeoutError. That is a normal idle tick, not an error: _read_once
    must swallow it and return quietly (no teardown, no exception propagated to the
    run loop's generic RedisError back-off)."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xreadgroup_error = RedisTimeoutError("Timeout reading from localhost:6379")
    # Must not raise.
    await _consumer(redis, livekit)._read_once()
    assert livekit.calls == []
    assert redis.acked == []
