"""Transcript reconciler: a control-plane lifespan job that recovers transcripts
a hard worker crash would otherwise lose.

`call.ended` — the only trigger that finalizes a call's transcript from Redis into
Postgres — is emitted best-effort from the worker's graceful shutdown. On SIGKILL /
OOM / pod eviction it never fires, so the turns sit in the Redis stream under its
rolling TTL and then expire with no signal. This sweep closes that gap: it scans the
live transcript streams and, for any whose call has NOT been persisted and whose
stream has been idle past the grace window (i.e. the producer is gone), drains it
into the transcript table. Streams whose call is already persisted are just cleared.

Follows the run_forever loop discipline (never die: log + sleep on error). Reuses the
same idempotent finalize_transcript as the call.ended handler, so a race between the
two triggers is a harmless no-op (UNIQUE(call_id, seq) + ON CONFLICT DO NOTHING).
"""

import logging
import time
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.recording_jobs import run_forever
from control_plane.worker_events import finalize_transcript
from vera_core.db import tenant_session
from vera_core.models import Transcript
from vera_core.observability.correlation import RoomRef, parse_room_name
from vera_core.transcript import TranscriptService, transcript_stream_key

logger = logging.getLogger("control_plane.transcript_jobs")

# Derive the stream-key prefix from the one public helper that owns it, rather than
# re-hardcoding "vera:transcript:" — so a change to the key scheme can't drift.
_KEY_PREFIX = transcript_stream_key("")


class TranscriptReconciler:
    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        transcripts: TranscriptService,
        *,
        interval_seconds: int,
        idle_seconds: int,
    ) -> None:
        self._redis = redis
        self._sessionmaker = sessionmaker
        self._transcripts = transcripts
        self._interval = interval_seconds
        self._idle_ms = idle_seconds * 1000

    async def run(self) -> None:
        await run_forever("transcript reconciler", self.tick, self._interval)

    async def tick(self) -> None:
        async for key in self._redis.scan_iter(match=f"{_KEY_PREFIX}*", count=100):
            room_name = key.removeprefix(_KEY_PREFIX)
            ref = parse_room_name(room_name)
            if ref is None:
                continue  # a foreign stream key that isn't a call room
            try:
                await self._reconcile_stream(ref, room_name)
            except Exception:
                # One bad stream must not starve the rest; finalize is idempotent,
                # so a retry next tick is safe.
                logger.exception("transcript reconcile failed for %s", room_name)

    async def _reconcile_stream(self, ref: RoomRef, room_name: str) -> None:
        if await self._has_rows(ref.tenant_id, ref.call_id):
            # The normal call.ended finalizer already persisted this call — the
            # stream is redundant, reclaim it (its grace TTL would too, eventually).
            await self._transcripts.clear(room_name)
            return
        last_ms = await self._last_entry_ms(room_name)
        if last_ms is None:
            return  # empty stream — nothing to recover
        if time.time() * 1000 - last_ms < self._idle_ms:
            # Still being appended to (or only just ended) — leave it for the normal
            # finalizer. Operational idle check, not a persisted timestamp.
            return
        count = await self._finalize(ref, room_name)
        if count:
            logger.warning(
                "transcript reconciler recovered %d turns for call %s (crash-orphaned)",
                count,
                ref.call_id,
            )
        await self._transcripts.clear(room_name)

    async def _has_rows(self, tenant_id: UUID, call_id: UUID) -> bool:
        """True if this call already has persisted transcript rows (a DB seam)."""
        async with tenant_session(self._sessionmaker, tenant_id) as session:
            row = (
                await session.execute(
                    select(Transcript.id).where(Transcript.call_id == call_id).limit(1)
                )
            ).first()
        return row is not None

    async def _finalize(self, ref: RoomRef, room_name: str) -> int:
        return await finalize_transcript(
            self._sessionmaker, self._transcripts, ref.tenant_id, ref.call_id, room_name
        )

    async def _last_entry_ms(self, room_name: str) -> int | None:
        """Millisecond timestamp of the newest stream entry, or None if empty.
        Redis stream ids are `<ms>-<seq>`, so the ms prefix is the append time."""
        # decode_responses=True gives str ids at runtime; the redis-py stubs type the
        # response as a broad union, so cast (as worker_events does for XREAD).
        raw = cast(
            list[tuple[str, dict[str, str]]],
            await self._redis.xrevrange(transcript_stream_key(room_name), count=1),
        )
        if not raw:
            return None
        entry_id = raw[0][0]
        return int(entry_id.split("-")[0])
