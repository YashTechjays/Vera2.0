"""Live transcript stream — event model, Redis-stream transport, and the reusable
TranscriptService.

The worker publishes finalized, de-identified turns; consumers (the SSE endpoint today,
the persistence finalizer + analytics later) read them. Everyone goes through
TranscriptService so the consume/publish surface is defined once and no caller touches
raw Redis. The stream carries only tokenized text (the de-identified side of the PHI
wall) — never hydrated raw PHI (see repo CLAUDE.md).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, Protocol, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

ROLE_USER: Literal["user"] = "user"
ROLE_AGENT: Literal["agent"] = "agent"

_KEY_PREFIX = "vera:transcript:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"


def transcript_stream_key(room_name: str) -> str:
    """Redis stream key for a room's live transcript (mirrors vera:sess:/vera:perms:)."""
    return f"{_KEY_PREFIX}{room_name}"


class TranscriptEvent(BaseModel):
    """One finalized turn. `text` is always tokenized / de-identified."""

    role: Literal["user", "agent"]
    text: str
    ts: int  # epoch milliseconds


class TranscriptStore(Protocol):
    """Low-level transport. Callers use TranscriptService, not this directly."""

    async def publish(self, room_name: str, event: TranscriptEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]: ...


class InMemoryTranscriptStore:
    """Reference impl: replay all entries, then tail until the ended sentinel."""

    def __init__(self) -> None:
        self._entries: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._seq = 0
        self._cond = asyncio.Condition()

    async def _append(self, key: str, fields: dict[str, str]) -> None:
        async with self._cond:
            self._seq += 1
            self._entries.setdefault(key, []).append((f"{self._seq}-0", fields))
            self._cond.notify_all()

    async def publish(self, room_name: str, event: TranscriptEvent) -> None:
        await self._append(
            transcript_stream_key(room_name),
            {"role": event.role, "text": event.text, "ts": str(event.ts)},
        )

    async def mark_ended(self, room_name: str) -> None:
        # The sentinel entry is what terminates read(); no separate ended-flag needed.
        await self._append(transcript_stream_key(room_name), {_ENDED_FIELD: _ENDED_VALUE})

    async def delete(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        async with self._cond:
            self._entries.pop(key, None)
            self._cond.notify_all()

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        idx = 0
        while True:
            async with self._cond:
                while idx >= len(self._entries.get(key, [])):
                    await self._cond.wait()
                entry_id, fields = self._entries[key][idx]
                idx += 1
            if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                return
            yield (
                entry_id,
                TranscriptEvent(
                    role=fields["role"],
                    text=fields["text"],
                    ts=int(fields["ts"]),
                ),
            )


class RedisTranscriptStore:
    """Redis Streams transport. XADD on publish (refreshing a rolling backstop TTL so an
    abandoned stream self-clears); `mark_ended` appends the sentinel + a short grace TTL
    so connected readers drain then the key clears. `read` replays from `0` via XREAD
    then tails (BLOCK), stopping on the sentinel or when the key disappears."""

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
        # XREAD BLOCK wakes instantly on a new entry (incl. the ended sentinel); this
        # only bounds how often an idle stream re-checks EXISTS for an abnormal clear.
        self._block_ms = block_ms

    async def publish(self, room_name: str, event: TranscriptEvent) -> None:
        # XADD + the rolling backstop EXPIRE in a single round-trip.
        key = transcript_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {"role": event.role, "text": event.text, "ts": str(event.ts)})
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def mark_ended(self, room_name: str) -> None:
        # Append the sentinel + set the short grace TTL in a single round-trip.
        key = transcript_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {_ENDED_FIELD: _ENDED_VALUE})
        pipe.expire(key, self._end_grace_seconds)
        await pipe.execute()

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(transcript_stream_key(room_name))

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        last_id = "0"
        seen = False  # have we observed the stream actually exist yet?
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # redis-py turns BLOCK into a per-command read deadline and RAISES
                # (not empty) when an idle stream produces nothing in the window.
                # Treat it as "no new entries this interval".
                result = None
            if not result:
                # Terminate only if the stream was live and is now gone (grace TTL
                # elapsed / deleted). A not-yet-created stream stays open and waits
                # for its first entry (the worker may still be spinning up).
                if seen and not await self._redis.exists(key):
                    return
                continue
            seen = True
            xread_result = cast(
                list[tuple[str, list[tuple[str, dict[str, str]]]]],
                result,
            )
            _stream, entries = xread_result[0]
            for entry_id, fields in entries:
                last_id = entry_id
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    return
                yield (
                    entry_id,
                    TranscriptEvent(
                        role=fields["role"],
                        text=fields["text"],
                        ts=int(fields["ts"]),
                    ),
                )


class TranscriptService:
    """The reusable produce/consume API over a TranscriptStore. Producers
    (the worker) and consumers (the SSE endpoint today; the finalizer + analytics
    later) all go through this one surface — never raw Redis."""

    def __init__(self, store: TranscriptStore) -> None:
        self._store = store

    async def publish_turn(
        self,
        room_name: str,
        role: Literal["user", "agent"],
        text: str,
        *,
        ts: int,
    ) -> None:
        await self._store.publish(room_name, TranscriptEvent(role=role, text=text, ts=ts))

    def consume(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        """Replay from the start, then tail until the stream ends. The single shared
        consume method — the SSE endpoint frames over it, the finalizer drains it.

        Producer contract: turns are published in chronological order (`event.ts`
        non-decreasing) even across a barge-in, so the stream a consumer reads is already
        correct — append/persist in this order as-is, no re-sort by `ts` needed. (The stream
        is append-only, so the producer enforces order before each write; a consumer cannot
        reorder after the fact anyway.)"""
        return self._store.read(room_name)

    async def collect(self, room_name: str) -> list[TranscriptEvent]:
        """Drain an ended stream into a list (finalizer/tests).

        Precondition: end() must have been called for this room, otherwise this
        coroutine blocks indefinitely (it tails until the ended sentinel).
        """
        return [event async for _id, event in self._store.read(room_name)]

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
