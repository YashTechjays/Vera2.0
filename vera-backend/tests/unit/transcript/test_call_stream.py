"""CallStreamEvent envelope + service semantics (in-memory store variant)."""

import json
from collections.abc import AsyncIterator
from typing import cast

import pytest
from redis.asyncio import Redis

from vera_core.call_stream import (
    CallStreamEvent,
    CallStreamService,
    RedisCallStreamStore,
    call_stream_key,
)


class _MemStore:
    """Minimal in-memory store capturing publishes; read replays then stops on end."""

    def __init__(self) -> None:
        self.events: list[CallStreamEvent] = []
        self.ended = False
        self.last_read_deadline: float | None = None

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.events.append(event)

    async def mark_ended(self, room_name: str) -> None:
        self.ended = True

    async def delete(self, room_name: str) -> None:
        self.events.clear()

    async def exists(self, room_name: str) -> bool:
        return bool(self.events) or self.ended

    async def read(
        self, room_name: str, *, first_entry_deadline_s: float | None = None
    ) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        self.last_read_deadline = first_entry_deadline_s
        for i, event in enumerate(self.events):
            yield (f"{i}-0", event)

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return list(self.events)


class _FakeRedis:
    """Just enough of the redis-py surface for RedisCallStreamStore.read_all."""

    def __init__(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self._entries = entries

    async def xrange(self, _key: str, _min: str, _max: str) -> list[tuple[str, dict[str, str]]]:
        return self._entries


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


@pytest.mark.asyncio
async def test_consume_forwards_first_entry_deadline_to_store() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    _ = [e async for _id, e in svc.consume("r", first_entry_deadline_s=12.5)]
    assert store.last_read_deadline == 12.5


@pytest.mark.asyncio
async def test_consume_defaults_first_entry_deadline_to_none() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    _ = [e async for _id, e in svc.consume("r")]
    assert store.last_read_deadline is None


@pytest.mark.asyncio
async def test_service_exists_false_for_untouched_room() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    assert await svc.exists("r") is False


@pytest.mark.asyncio
async def test_service_exists_true_after_publish() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "user", "hi", ts=1)
    assert await svc.exists("r") is True


@pytest.mark.asyncio
async def test_service_read_all_delegates_to_store() -> None:
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "user", "hi", ts=1)
    got = await svc.read_all("r")
    assert got == [CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1)]


def _entry(fields: dict[str, str]) -> tuple[str, dict[str, str]]:
    return ("1-0", fields)


@pytest.mark.asyncio
async def test_redis_store_read_all_snapshots_without_blocking() -> None:
    fields = {"type": "transcript", "data": json.dumps({"role": "agent", "text": "hi"}), "ts": "5"}
    redis = _FakeRedis([_entry(fields)])
    store = RedisCallStreamStore(cast(Redis, redis), ttl_seconds=60, end_grace_seconds=10)
    got = await store.read_all("r")
    assert got == [CallStreamEvent(type="transcript", data={"role": "agent", "text": "hi"}, ts=5)]


@pytest.mark.asyncio
async def test_redis_store_read_all_skips_the_ended_sentinel() -> None:
    turn = {"type": "transcript", "data": json.dumps({"role": "user", "text": "hi"}), "ts": "1"}
    sentinel = {"event": "ended"}
    redis = _FakeRedis([("1-0", turn), ("2-0", sentinel)])
    store = RedisCallStreamStore(cast(Redis, redis), ttl_seconds=60, end_grace_seconds=10)
    got = await store.read_all("r")
    assert [e.data for e in got] == [{"role": "user", "text": "hi"}]


@pytest.mark.asyncio
async def test_redis_store_read_all_skips_malformed_entries_without_raising() -> None:
    """A crashed worker's half-written entry, or Redis corruption, must not sink the
    whole finalize — skip it and keep the well-formed entries either side of it."""
    good_1 = {"type": "transcript", "data": json.dumps({"role": "user", "text": "a"}), "ts": "1"}
    malformed = {"type": "transcript", "data": "{not json", "ts": "2"}  # bad JSON
    missing_field = {"type": "transcript", "ts": "3"}  # no "data" field at all
    good_2 = {"type": "transcript", "data": json.dumps({"role": "agent", "text": "b"}), "ts": "4"}
    redis = _FakeRedis(
        [
            ("1-0", good_1),
            ("2-0", malformed),
            ("3-0", missing_field),
            ("4-0", good_2),
        ]
    )
    store = RedisCallStreamStore(cast(Redis, redis), ttl_seconds=60, end_grace_seconds=10)
    got = await store.read_all("r")
    assert [e.data["text"] for e in got] == ["a", "b"]


@pytest.mark.asyncio
async def test_redis_store_read_all_empty_stream_returns_empty_list() -> None:
    redis = _FakeRedis([])
    store = RedisCallStreamStore(cast(Redis, redis), ttl_seconds=60, end_grace_seconds=10)
    assert await store.read_all("r") == []
