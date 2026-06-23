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
from typing import Literal, Protocol

from pydantic import BaseModel

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
        self._ended: set[str] = set()
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
        key = transcript_stream_key(room_name)
        async with self._cond:
            self._seq += 1
            self._entries.setdefault(key, []).append(
                (f"{self._seq}-0", {_ENDED_FIELD: _ENDED_VALUE})
            )
            self._ended.add(key)
            self._cond.notify_all()

    async def delete(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        async with self._cond:
            self._entries.pop(key, None)
            self._ended.discard(key)
            self._cond.notify_all()

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        idx = 0
        while True:
            async with self._cond:
                entries = self._entries.get(key, [])
                while idx >= len(entries):
                    if key in self._ended:
                        return
                    await self._cond.wait()
                    entries = self._entries.get(key, [])
                entry_id, fields = entries[idx]
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
        consume method — the SSE endpoint frames over it, the finalizer drains it."""
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
