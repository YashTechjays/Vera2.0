"""Persist a call's live event stream into the durable `transcript` table at the
call's terminal path, then clear the Redis stream.

Durability first, Redis hygiene second: the call-events stream carries only a
rolling TTL backstop (`vera_core.call_stream`), which silently discards content
once it lapses — today `transcript` has no writer, so every turn is lost the
moment the TTL expires. Both terminal paths (`call_closeout.close_call`'s two
callers — the worker-event consumer and the pipeline sweeper) call
`finalize_transcript` right after `close_call` returns a `RoomRef`, before
anything else runs, so the DB write always happens before the room is refilled
or the next sweep tick.

Exception-safe by design: a finalizer failure must never block closeout (the
call's terminal status is already committed by the time this runs) — log and
swallow, leaving the stream's TTL as the backstop. Idempotent: `ON CONFLICT
(call_id, seq) DO NOTHING` absorbs a redelivered `call.ended`, and re-reading an
already-cleared stream just yields zero rows.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent, CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.models.enums import TranscriptSource
from vera_core.models.transcript import Transcript
from vera_core.observability.correlation import RoomRef

logger = logging.getLogger(__name__)

# role -> TranscriptSource: "user" is the payer rep, the human on a real call;
# "agent" is Vera. A role outside this map can only come from a corrupted
# envelope — there is no third live role today — so it is dropped rather than
# guessed at (mapping it to BOT would misattribute speech that may not be the
# agent's).
_SOURCE_BY_ROLE: dict[str, TranscriptSource] = {
    "user": TranscriptSource.REP,
    "agent": TranscriptSource.BOT,
}


def _build_rows(
    ref: RoomRef, events: Sequence[CallStreamEvent]
) -> tuple[list[dict[str, Any]], int]:
    """Map transcript-type envelopes to `Transcript` row dicts, oldest first.

    `seq` is 0-based over the rows actually produced — a stream can also carry
    non-transcript envelopes (e.g. `call_status`), which never occupy a seq slot.
    Returns `(rows, skipped_role_count)`; the caller logs the count only (never
    the role/text — PHI)."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    seq = 0
    for event in events:
        if event.type != TYPE_TRANSCRIPT:
            continue
        role = str(event.data.get("role", ""))
        source = _SOURCE_BY_ROLE.get(role)
        if source is None:
            skipped += 1
            continue
        rows.append(
            {
                "tenant_id": ref.tenant_id,
                "call_id": ref.call_id,
                "seq": seq,
                "source": source.value,
                "role": role,
                "message": str(event.data.get("text", "")),
                "spoke_at": datetime.fromtimestamp(event.ts / 1000, tz=UTC),
            }
        )
        seq += 1
    return rows, skipped


async def finalize_transcript(
    sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    ref: RoomRef,
    room_name: str,
) -> int:
    """Drain `room_name`'s event stream into `transcript` rows for `ref.call_id`,
    commit, then delete the stream. Returns the row count written — 0 if the
    stream never existed, every event was a non-transcript/unrecognized-role
    envelope, or the finalizer itself failed (never raises)."""
    try:
        events = await call_stream_service.read_all(room_name)
        rows, skipped = _build_rows(ref, events)
        if skipped:
            logger.warning(
                "transcript finalize: skipped %d turn(s) with an unrecognized role for call %s",
                skipped,
                ref.call_id,
            )
        if rows:
            async with tenant_session(sessionmaker, ref.tenant_id) as session:
                stmt = (
                    insert(Transcript)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["call_id", "seq"])
                )
                await session.execute(stmt)
        # Clear only after the insert above committed (tenant_session commits on
        # a clean exit) — a failed commit must leave the stream in place so the
        # next redelivery / the TTL backstop still has it.
        await call_stream_service.clear(room_name)
        return len(rows)
    except Exception as exc:
        # Exception content is unsafe here — SQLAlchemy statement errors embed the
        # bound parameters (the transcript text, PHI); log only the exception type.
        logger.warning(
            "transcript finalize failed for call %s (%s); stream TTL is the backstop",
            ref.call_id,
            type(exc).__name__,
        )
        return 0
