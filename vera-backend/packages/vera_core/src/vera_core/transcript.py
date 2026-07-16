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
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, model_validator
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

# Turn vocabulary, shared by every live stream (voice-lab transcript + real-call events).
# `role` says WHAT the turn is (speech vs. a keypad press vs. future event kinds) and is
# meant to grow; `source` says WHO acted (the constrained actor set mirroring the
# transcript table's `source` column) and drives attribution — e.g. which side of the
# UI a turn renders on.
ROLE_USER: Literal["user"] = "user"
ROLE_AGENT: Literal["agent"] = "agent"
ROLE_DTMF: Literal["dtmf"] = "dtmf"  # a keypad press (DTMF), text = the digits sent

SOURCE_REP: Literal["rep"] = "rep"  # the human on the line (payer rep / IVR side)
SOURCE_BOT: Literal["bot"] = "bot"  # Vera — speech or an action it took
SOURCE_SUPERVISOR: Literal["supervisor"] = "supervisor"  # a supervisor who took over the call

type TurnRole = Literal["user", "agent", "dtmf"]
type TurnSource = Literal["rep", "bot", "supervisor"]

_SOURCE_BY_ROLE: dict[str, TurnSource] = {
    ROLE_USER: SOURCE_REP,
    ROLE_AGENT: SOURCE_BOT,
    ROLE_DTMF: SOURCE_BOT,
}


def source_for_role(role: TurnRole) -> TurnSource:
    """The acting source implied by a role — the producer-side stamp for today's roles,
    and the consumer-side fallback for legacy stream entries published before `source`."""
    return _SOURCE_BY_ROLE[role]


_KEY_PREFIX = "vera:transcript:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"


def transcript_stream_key(room_name: str) -> str:
    """Redis stream key for a room's live transcript (mirrors vera:sess:/vera:perms:)."""
    return f"{_KEY_PREFIX}{room_name}"


class TranscriptEvent(BaseModel):
    """One finalized turn. `text` is always tokenized / de-identified."""

    role: TurnRole
    source: TurnSource
    text: str
    ts: int  # epoch milliseconds

    @model_validator(mode="before")
    @classmethod
    def _derive_source(cls, data: Any) -> Any:
        # Legacy stream entries (published before `source` existed) carry only a role;
        # derive the actor so no consumer ever sees a source-less turn mid-deploy.
        if isinstance(data, dict) and data.get("source") is None:
            role = data.get("role")
            if isinstance(role, str) and role in _SOURCE_BY_ROLE:
                return {**data, "source": _SOURCE_BY_ROLE[role]}
        return data


def _event_from_fields(fields: dict[str, str]) -> TranscriptEvent:
    # `source` may be absent on legacy entries; the model validator derives it from role.
    return TranscriptEvent.model_validate(
        {
            "role": fields["role"],
            "source": fields.get("source"),
            "text": fields["text"],
            "ts": int(fields["ts"]),
        }
    )


class TranscriptStore(Protocol):
    """Low-level transport. Callers use TranscriptService, not this directly."""

    async def publish(self, room_name: str, event: TranscriptEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent] | None]: ...
    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]: ...


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
            {"role": event.role, "source": event.source, "text": event.text, "ts": str(event.ts)},
        )

    async def mark_ended(self, room_name: str) -> None:
        # The sentinel entry is what terminates read(); no separate ended-flag needed.
        await self._append(transcript_stream_key(room_name), {_ENDED_FIELD: _ENDED_VALUE})

    async def delete(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        async with self._cond:
            self._entries.pop(key, None)
            self._cond.notify_all()

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent] | None]:
        # Never yields the None keepalive tick — it waits on a Condition, not a
        # blocking read, so there is no idle window to surface.
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
            yield entry_id, _event_from_fields(fields)

    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        out: list[tuple[str, TranscriptEvent]] = []
        async with self._cond:
            for entry_id, fields in self._entries.get(key, []):
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    continue
                out.append((entry_id, _event_from_fields(fields)))
        return out


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
        pipe.xadd(
            key,
            {"role": event.role, "source": event.source, "text": event.text, "ts": str(event.ts)},
        )
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

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent] | None]:
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
                # Keepalive tick: the SSE endpoint frames this as a comment so a
                # silent call keeps bytes flowing through proxy read timeouts.
                yield None
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
                yield entry_id, _event_from_fields(fields)

    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        entries = cast(
            list[tuple[str, dict[str, str]]],
            await self._redis.xrange(key),
        )
        out: list[tuple[str, TranscriptEvent]] = []
        for entry_id, fields in entries:
            if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                continue
            out.append((entry_id, _event_from_fields(fields)))
        return out


class TranscriptService:
    """The reusable produce/consume API over a TranscriptStore. Producers
    (the worker) and consumers (the SSE endpoint today; the finalizer + analytics
    later) all go through this one surface — never raw Redis."""

    def __init__(self, store: TranscriptStore) -> None:
        self._store = store

    async def publish_turn(
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
    ) -> None:
        """Publish one finalized turn. `source` (the acting side) defaults from the role
        for today's vocabularies; producers pass it explicitly when they know better."""
        await self._store.publish(
            room_name,
            TranscriptEvent(role=role, source=source or source_for_role(role), text=text, ts=ts),
        )

    def consume(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent] | None]:
        """Replay from the start, then tail until the stream ends. A `None` item is
        an idle-window keepalive tick (Redis store only). The single shared
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
        return [item[1] async for item in self._store.read(room_name) if item is not None]

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)

    async def snapshot(self, room_name: str) -> list[TranscriptEvent]:
        """One-shot, non-blocking drain of the current stream (post-call re-read).
        Unlike consume()/collect(), returns even if the ended sentinel is absent."""
        return [event for _id, event in await self._store.snapshot(room_name)]
