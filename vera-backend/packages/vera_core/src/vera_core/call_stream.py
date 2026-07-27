"""The single live per-call event stream — envelope model, Redis transport, service.

One stream per room (`vera:call-events:{room}`) carrying typed envelopes, so ONE pipe
feeds every live surface: transcript turns and call-status frames today, form-filling
progress later. It is the only live transport — the real-call SSE, the voice-lab SSE
(which adapts envelopes back to a flat turn wire), the transcript finalizer, the call
summariser and the worker's Observer (filtering `type == "transcript"`) all read it.
The turn vocabulary these envelopes carry lives in `vera_core.transcript`.
Payloads are de-identified only; never hydrated raw PHI.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from vera_core.transcript import TurnRole, TurnSource, source_for_role

logger = logging.getLogger(__name__)

_KEY_PREFIX = "vera:call-events:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"

TYPE_TRANSCRIPT = "transcript"
TYPE_CALL_STATUS = "call_status"
TYPE_HEALTH = "health"
TYPE_FIELD_ANSWER = "field_answer"


def call_stream_key(room_name: str) -> str:
    return f"{_KEY_PREFIX}{room_name}"


class CallStreamEvent(BaseModel):
    """One live event. `data` is type-specific and de-identified by construction."""

    type: str  # "transcript" | "call_status" | "health" | "field_answer" | future types
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
    async def exists(self, room_name: str) -> bool: ...
    def read(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]: ...
    async def read_all(self, room_name: str) -> list[CallStreamEvent]: ...


class RedisCallStreamStore:
    """Redis Streams transport: rolling backstop TTL on publish; ended sentinel + grace
    TTL; replay-then-tail read that stops on the sentinel or a vanished key. `read` is the
    canonical BLOCK-timeout handling every tailing consumer copies (see repo CLAUDE.md)."""

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

    async def exists(self, room_name: str) -> bool:
        return bool(await self._redis.exists(call_stream_key(room_name)))

    async def read(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
        """Replay-then-tail from `start_id` ("0" replays everything; Redis's "$"
        sentinel tails only entries added after this call starts — the coaching
        listener uses "$" so a restart mid-call can't re-inject a stale note).
        When `first_entry_deadline_s` is set and nothing has EVER been seen on
        this stream, give up once that many seconds have elapsed since the read
        started — bounds a tail on a stream that may never appear (see
        `stream_call_events`'s live-no-stream branch). A stream that has already
        yielded at least one entry tails indefinitely regardless — the deadline
        only guards the "is anything ever going to show up" question.

        Every idle BLOCK window yields a `None` keepalive tick so a consumer
        holding a byte stream open (the SSE endpoint) can emit a heartbeat —
        a silent call must not starve proxy read timeouts (nginx defaults to
        60s) into killing the connection."""
        key = call_stream_key(room_name)
        last_id = start_id
        seen = False
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + first_entry_deadline_s if first_entry_deadline_s is not None else None
        )
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # BLOCK with no entries RAISES (per-command read deadline) — idle tick.
                result = None
            if not result:
                if seen and not await self._redis.exists(key):
                    return
                if not seen and deadline is not None and loop.time() >= deadline:
                    return
                yield None  # keepalive tick — see docstring
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
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
        user_id: str | None = None,
    ) -> None:
        """Publish one finalized turn. `source` (the acting side — drives UI attribution)
        defaults from the role; producers pass it explicitly when they know better.
        `user_id` (the specific supervisor who spoke/coached, if known) is only added
        to the envelope when present, so older consumers see no shape change."""
        data: dict[str, Any] = {
            "role": role,
            "source": source or source_for_role(role),
            "text": text,
        }
        if user_id is not None:
            data["user_id"] = user_id
        await self._store.publish(
            room_name, CallStreamEvent(type=TYPE_TRANSCRIPT, data=data, ts=ts)
        )

    async def publish_status(self, room_name: str, status: str, *, ts: int) -> None:
        await self._store.publish(
            room_name, CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": status}, ts=ts)
        )

    async def publish_health(
        self, room_name: str, *, score: int, flag: str, reason: str, ts: int
    ) -> None:
        """Publish one call-health-observer assessment frame (spec: rides the
        same /calls/{id}/events SSE — no new pipe per event type)."""
        await self._store.publish(
            room_name,
            CallStreamEvent(
                type=TYPE_HEALTH, data={"score": score, "flag": flag, "reason": reason}, ts=ts
            ),
        )

    async def publish_field_answer(
        self,
        room_name: str,
        *,
        field_path: str,
        value: str,
        confidence: int | None,
        evidence_seq: int | None,
        completion_pct: float | None,
        dispute: dict[str, Any] | None,
        ts: int,
    ) -> None:
        """Publish one Observer-extracted field answer (spec: rides the same
        /calls/{id}/events SSE — no new pipe per event type). `value` is PHI: it lives
        only in-boundary on this CMEK-protected stream and reaches the browser inside the
        already-authorized SSE session — never log it."""
        await self._store.publish(
            room_name,
            CallStreamEvent(
                type=TYPE_FIELD_ANSWER,
                data={
                    "field_path": field_path,
                    "value": value,
                    "source": "ai_call",
                    "confidence": confidence,
                    "evidence_seq": evidence_seq,
                    "completion_pct": completion_pct,
                    "dispute": dispute,
                },
                ts=ts,
            ),
        )

    def consume(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
        """Replay-then-tail from `start_id`; a `None` item is an idle-window
        keepalive tick."""
        return self._store.read(
            room_name, start_id=start_id, first_entry_deadline_s=first_entry_deadline_s
        )

    async def exists(self, room_name: str) -> bool:
        return await self._store.exists(room_name)

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        """One-shot snapshot for the terminal-path finalizer (see `read_all` above)."""
        return await self._store.read_all(room_name)

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
