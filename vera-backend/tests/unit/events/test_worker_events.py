"""Unit tests for the worker→control-plane event contract and Redis-stream bus."""

import pytest
from pydantic import ValidationError

from vera_core.events import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallFailedEvent,
    CallFailureReason,
    WorkerEventBus,
    parse_worker_event,
)


def test_call_failed_event_round_trips() -> None:
    event = CallFailedEvent(
        room_name="call--t--c", reason=CallFailureReason.BUSY_OR_DECLINED, ts=1720000000000
    )
    parsed = parse_worker_event(event.model_dump_json())
    assert parsed == event
    assert parsed.type == "call.failed"


def test_parse_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        parse_worker_event('{"type": "not.a.real.event", "room_name": "r", "ts": 1}')


def test_event_carries_no_phi_fields() -> None:
    # The wire payload must never grow a phone-number / transcript field.
    assert set(CallFailedEvent.model_fields) == {"type", "room_name", "reason", "ts"}


class _FakeRedis:
    def __init__(self) -> None:
        self.added: list[tuple[str, dict[str, str], int | None, bool]] = []
        self.group_calls: list[tuple[str, str]] = []
        self.busygroup = False

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> None:
        self.added.append((stream, fields, maxlen, approximate))

    async def xgroup_create(self, stream: str, group: str, *, id: str, mkstream: bool) -> None:
        from redis.exceptions import ResponseError

        self.group_calls.append((stream, group))
        if self.busygroup:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")


@pytest.mark.asyncio
async def test_emit_xadds_trimmed_json() -> None:
    redis = _FakeRedis()
    bus = WorkerEventBus(redis, maxlen=500)  # type: ignore[arg-type]
    event = CallFailedEvent(room_name="call--t--c", reason=CallFailureReason.NO_ANSWER, ts=1)
    await bus.emit(event)
    stream, fields, maxlen, approximate = redis.added[0]
    assert stream == WORKER_EVENTS_STREAM
    assert parse_worker_event(fields["event"]) == event
    assert (maxlen, approximate) == (500, True)


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent_on_busygroup() -> None:
    redis = _FakeRedis()
    redis.busygroup = True
    bus = WorkerEventBus(redis)  # type: ignore[arg-type]
    await bus.ensure_group()  # must not raise
    assert redis.group_calls == [(WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP)]
