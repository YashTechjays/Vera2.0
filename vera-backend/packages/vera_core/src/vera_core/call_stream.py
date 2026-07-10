"""Generalized live per-call event stream — envelope model, Redis transport, service.

The real-call counterpart of `vera_core.transcript` (which stays voice-lab-only):
one stream per room carrying typed envelopes so ONE SSE can deliver every live
surface — transcript turns today, call-status frames, and (later) form-filling
progress — without a new pipe per event type. Payloads are tokenized /
de-identified only (same PHI contract as the transcript stream); never hydrated
raw PHI.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger(__name__)

_KEY_PREFIX = "vera:call-events:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"

TYPE_TRANSCRIPT = "transcript"
TYPE_CALL_STATUS = "call_status"


def call_stream_key(room_name: str) -> str:
    return f"{_KEY_PREFIX}{room_name}"


class CallStreamEvent(BaseModel):
    """One live event. `data` is type-specific and de-identified by construction."""

    type: str  # "transcript" | "call_status" | future types (e.g. "form_field")
    data: dict[str, Any]
    ts: int  # epoch milliseconds


def _event_from_fields(fields: dict[str, str]) -> CallStreamEvent:
    return CallStreamEvent(
        type=fields["type"],
        data=json.loads(fields["data"]),
        ts=int(fields["ts"]),
    )


class CallStreamStore(Protocol):
    async def publish(self, room_name: str, event: CallStreamEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]: ...
    async def read_all(self, room_name: str) -> list[CallStreamEvent]: ...


class RedisCallStreamStore:
    """Redis Streams transport; identical lifecycle to RedisTranscriptStore
    (rolling backstop TTL on publish; ended sentinel + grace TTL; replay-then-tail
    read that stops on the sentinel or a vanished key)."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        end_grace_seconds: int,
        block_ms: int = 5000,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._end_grace_seconds = end_grace_seconds
        self._block_ms = block_ms

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        key = call_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {"type": event.type, "data": json.dumps(event.data), "ts": str(event.ts)})
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def mark_ended(self, room_name: str) -> None:
        key = call_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {_ENDED_FIELD: _ENDED_VALUE})
        pipe.expire(key, self._end_grace_seconds)
        await pipe.execute()

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(call_stream_key(room_name))

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        key = call_stream_key(room_name)
        last_id = "0"
        seen = False
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # BLOCK with no entries RAISES (per-command read deadline) — idle tick.
                result = None
            if not result:
                if seen and not await self._redis.exists(key):
                    return
                continue
            seen = True
            xread_result = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", result)
            _stream, entries = xread_result[0]
            for entry_id, fields in entries:
                last_id = entry_id
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    return
                yield entry_id, _event_from_fields(fields)

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        """One-shot snapshot of every event currently on the stream, oldest first
        (XRANGE `-` to `+` — no BLOCK, no tail). For the terminal-path finalizer:
        the stream may or may not carry an ended sentinel (a crashed worker never
        writes one), so this works with or without it. Skips the sentinel entry and
        any malformed entry; content is never logged (PHI), only a skipped count."""
        entries = await self._redis.xrange(call_stream_key(room_name), "-", "+") or []
        events: list[CallStreamEvent] = []
        malformed = 0
        for _entry_id, fields in cast("list[tuple[str, dict[str, str]]]", entries):
            if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                continue
            try:
                events.append(_event_from_fields(fields))
            except Exception:  # malformed entry; content is PHI, never logged, count only
                malformed += 1
        if malformed:
            logger.warning(
                "call stream %s: skipped %d malformed entr%s",
                room_name,
                malformed,
                "y" if malformed == 1 else "ies",
            )
        return events


class CallStreamService:
    """Produce/consume surface over a CallStreamStore — no caller touches raw Redis.
    `publish_turn` matches the transcript publisher's TurnPublisher protocol so the
    worker's ReorderingEmitter can feed either stream."""

    def __init__(self, store: CallStreamStore) -> None:
        self._store = store

    async def publish_turn(
        self, room_name: str, role: Literal["user", "agent"], text: str, *, ts: int
    ) -> None:
        await self._store.publish(
            room_name,
            CallStreamEvent(type=TYPE_TRANSCRIPT, data={"role": role, "text": text}, ts=ts),
        )

    async def publish_status(self, room_name: str, status: str, *, ts: int) -> None:
        await self._store.publish(
            room_name, CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": status}, ts=ts)
        )

    def consume(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        return self._store.read(room_name)

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        """One-shot snapshot for the terminal-path finalizer (see `read_all` above)."""
        return await self._store.read_all(room_name)

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
