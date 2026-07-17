"""Consumes worker→control-plane events (Redis Streams consumer group) and
orchestrates the reaction. One consumer runs per control-plane process; the group
delivers each event to exactly one process, and entries a crashed process left
pending are reclaimed via XAUTOCLAIM (at-least-once). Handlers are idempotent, so
redelivery / a rare double-delivery is harmless.
"""

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_closeout import TERMINAL_VALUES, announce_terminal_status, close_call
from control_plane.dispatch import run_dispatch_pass
from control_plane.livekit_gateway import LiveKitGateway
from control_plane.post_call import resolve_ai_processing
from control_plane.transcript_finalizer import finalize_transcript
from vera_core.audit import AuditRecord, AuditSink
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.events import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallAnsweredEvent,
    CallAnswerRecordedEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)
from vera_core.models import Call, CallEvent, PatientForm, SchemaVersion
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AnswerSource, CallEventType, CallStatus
from vera_core.observability.correlation import parse_room_name
from vera_core.plan_store import CallPlanService
from vera_core.services.field_answers import recompute_form_projection, record_answer

logger = logging.getLogger("control_plane.worker_events")

type EventHandler = Callable[[WorkerEvent], Awaitable[None]]

# The redis-py stubs type XREADGROUP/XAUTOCLAIM responses as broad unions (they
# also cover bytes-mode and other subcommands); with `decode_responses=True` and
# no `justid`, both always return this shape at runtime.
type _StreamEntries = list[tuple[str, dict[str, str]]]

_FAILURE_STATUS: dict[CallFailureReason, CallStatus] = {
    CallFailureReason.NO_ANSWER: CallStatus.NO_ANSWER,
    CallFailureReason.BUSY_OR_DECLINED: CallStatus.BUSY,
    CallFailureReason.FAILED: CallStatus.FAILED,
}


class _RetryEventLater(Exception):
    """Control flow: a canonical-room event arrived before its Call row committed
    (the dispatcher dials inside the dispatch transaction). Leave the entry
    UNACKED so XAUTOCLAIM redelivers it after reclaim_idle_ms — by then the row
    is committed. Events older than _NO_ROW_RETRY_WINDOW_S with still no row are
    genuine voice-lab rooms and are dropped normally."""


_NO_ROW_RETRY_WINDOW_S = 120.0


def _event_is_young(ts_ms: int) -> bool:
    return (time.time() * 1000 - ts_ms) < _NO_ROW_RETRY_WINDOW_S * 1000


def _entry_room(fields: dict[str, str]) -> str:
    """Best-effort room_name for per-room dispatch grouping — a light JSON peek, not full
    validation (poison/roomless entries fall into the "" bucket and drop normally). Only
    the room_name (tenant+call UUIDs) is read; no values are touched."""
    raw = fields.get("event")
    if raw is None:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    room = parsed.get("room_name") if isinstance(parsed, dict) else None
    return room if isinstance(room, str) else ""


def _retry_young_or_drop(room_name: str, ts_ms: int) -> None:
    """A canonical-room event whose Call row isn't there yet: retry if the event is
    young (the dispatcher dials inside the dispatch transaction, so a fast answer/
    failure/end can race the row's commit), else let the caller drop it as a genuine
    voice-lab room. Raises `_RetryEventLater` in the retry case; returns otherwise."""
    if _event_is_young(ts_ms):
        raise _RetryEventLater(room_name)


class WorkerEventConsumer:
    def __init__(
        self,
        redis: Redis,
        livekit: LiveKitGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        kms: Any,
        audit: AuditSink,
        call_stream: CallStreamService,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
        form_auto_retry_enabled: bool = False,
        call_plans: CallPlanService | None = None,
    ) -> None:
        self._redis = redis
        self._livekit = livekit
        self._sessionmaker = sessionmaker
        self._kms = kms
        self._audit = audit
        self._call_stream = call_stream
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._teardown_grace_ms = teardown_grace_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._form_auto_retry_enabled = form_auto_retry_enabled
        self._call_plans = call_plans
        self._bus = WorkerEventBus(redis)
        self._handlers: dict[str, EventHandler] = {
            "call.failed": self._handle_call_failed,
            "call.answered": self._handle_call_answered,
            "call.ended": self._handle_call_ended,
            "call.answer_recorded": self._handle_call_answer_recorded,
        }

    async def run(self) -> None:
        """Ensure the group exists, then loop: reclaim stragglers, read new, dispatch.

        Group bootstrap lives inside the loop (guarded by `group_ready`) rather than
        before it, so a Redis blip at process startup is retried via the same
        back-off as steady-state errors instead of raising out of `run()` and
        killing the background task permanently.
        """
        group_ready = False
        while True:
            try:
                if not group_ready:
                    await self._bus.ensure_group()
                    group_ready = True
                await self._reclaim_stale()
                await self._read_once()
            except asyncio.CancelledError:
                raise
            except RedisError:
                logger.exception("worker-event consumer Redis error; backing off")
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        try:
            resp = await self._redis.xreadgroup(
                WORKER_EVENTS_GROUP,
                self._consumer,
                {WORKER_EVENTS_STREAM: ">"},
                count=16,
                block=self._block_ms,
            )
        except RedisTimeoutError:
            # redis-py turns an XREADGROUP BLOCK window with no new entries into a
            # raised TimeoutError (a per-command read deadline), not an empty result.
            # That is a normal idle tick — treat it as "no new events", NOT a Redis
            # error (which would log a traceback + back off). Mirrors RedisTranscriptStore.
            return
        if not resp:
            return
        streams = cast("list[tuple[str, _StreamEntries]]", resp)
        _stream, entries = streams[0]
        await self._dispatch(entries)

    async def _reclaim_stale(self) -> None:
        # Re-scans from the start of the stream (`start_id="0-0"`) on every call rather
        # than walking the returned cursor — fine given the low event volume here.
        result = await self._redis.xautoclaim(
            WORKER_EVENTS_STREAM,
            WORKER_EVENTS_GROUP,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=16,
        )
        _cursor, entries, _deleted = cast("tuple[str, _StreamEntries, list[str]]", result)
        await self._dispatch(entries)

    async def _dispatch(self, entries: _StreamEntries) -> None:
        """Process a batch concurrently ACROSS rooms but sequentially WITHIN a room.

        Answers for one call (and its terminal call.ended) must land in stream order —
        two answer_recorded events for the same field, or an answer racing call.ended's
        completion recompute, would otherwise interleave. Grouping by room_name preserves
        per-call order while keeping unrelated calls parallel."""
        by_room: dict[str, list[tuple[str, dict[str, str]]]] = {}
        for entry_id, fields in entries:
            by_room.setdefault(_entry_room(fields), []).append((entry_id, fields))

        async def _process_room(room_entries: list[tuple[str, dict[str, str]]]) -> None:
            for entry_id, fields in room_entries:
                await self._process(entry_id, fields)

        await asyncio.gather(*(_process_room(group) for group in by_room.values()))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get("event")
        if raw is None:
            logger.warning("worker event %s has no payload; dropping", entry_id)
            await self._ack(entry_id)
            return
        try:
            event = parse_worker_event(raw)
        except Exception as exc:
            # Type name only — a pydantic ValidationError's message/traceback
            # embeds the raw event payload verbatim.
            logger.warning(
                "dropping unparseable worker event %s (%s)", entry_id, type(exc).__name__
            )
            await self._ack(entry_id)  # poison entry — drop so it can't wedge the group
            return
        logger.info(
            "consumed worker event %s type=%s room=%s",
            entry_id,
            event.type,
            getattr(event, "room_name", "?"),
        )
        handler = self._handlers.get(event.type)
        if handler is None:
            logger.warning("no handler for worker event type %s; dropping", event.type)
            await self._ack(entry_id)
            return
        try:
            await handler(event)
        except _RetryEventLater:
            logger.info(
                "event %s for %s arrived before its call row; leaving unacked for redelivery",
                entry_id,
                getattr(event, "room_name", "?"),
            )
            return  # do NOT ack → XAUTOCLAIM retries once the Call row has committed
        except Exception as exc:
            # Type name only — handlers run the transcript finalizer, whose
            # SQLAlchemy/Redis errors embed transcript text (PHI).
            logger.error(
                "handler failed for event %s (%s); leaving unacked for reclaim",
                entry_id,
                type(exc).__name__,
            )
            return  # do NOT ack → XAUTOCLAIM retries later (at-least-once)
        await self._ack(entry_id)

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, entry_id)

    async def _handle_call_failed(self, event: WorkerEvent) -> None:
        # Narrow without `assert` (stripped under `python -O`); handlers are keyed by
        # event.type, so this only guards against a future mis-registration.
        if not isinstance(event, CallFailedEvent):
            return
        if parse_room_name(event.room_name) is None:
            logger.warning("call.failed for non-vera room %s; ignoring", event.room_name)
            return
        logger.info(
            "call.failed room=%s reason=%s: setting metadata + deleting room",
            event.room_name,
            event.reason.value,
        )
        await self._livekit.set_room_metadata(
            event.room_name, {"status": "call_failed", "reason": event.reason.value}
        )
        # A supervisor already tailing the live SSE learns the call died — the
        # worker never publishes a call_status frame for a failed dial (no
        # session ever existed), so the closeout announces it instead.
        await announce_terminal_status(
            self._call_stream, event.room_name, _FAILURE_STATUS[event.reason]
        )
        # Let the RoomMetadataChanged frame reach the browser before we tear the room down.
        if self._teardown_grace_ms:
            await asyncio.sleep(self._teardown_grace_ms / 1000)
        await self._livekit.delete_room(event.room_name)
        await self._close_and_refill(
            event.room_name, _FAILURE_STATUS[event.reason], trigger="call.failed", ts=event.ts
        )

    async def _handle_call_answered(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallAnsweredEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id).with_for_update())
            ).scalar_one_or_none()
            if call is None:
                _retry_young_or_drop(event.room_name, event.ts)
                return  # voice-lab room
            if call.current_status in TERMINAL_VALUES:
                return  # stale redelivery after terminal
            if call.current_status == CallStatus.ACTIVE.value:
                return  # idempotent redelivery
            call.current_status = CallStatus.ACTIVE.value
            call.started_at = func.now()
            session.add(
                CallEvent(
                    tenant_id=ref.tenant_id,
                    call_id=call.id,
                    event_type=CallEventType.STATUS.value,
                    event_value=CallStatus.ACTIVE.value,
                )
            )

    async def _handle_call_ended(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallEndedEvent):
            return
        await self._close_and_refill(
            event.room_name, CallStatus.COMPLETED, trigger="call.ended", ts=event.ts
        )

    async def _handle_call_answer_recorded(self, event: WorkerEvent) -> None:
        """Persist an Observer-extracted answer as an ai_call field_answer, then re-derive
        the form's promoted columns + completion_pct. Idempotent under redelivery (the
        writer no-ops an unchanged value); the form row lock serializes against a
        concurrent human resolve on the same form."""
        if not isinstance(event, CallAnswerRecordedEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return  # non-vera / console room
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id))
            ).scalar_one_or_none()
            if call is None:
                _retry_young_or_drop(event.room_name, event.ts)
                return  # voice-lab room (or the Call row hasn't committed yet → retry)
            form = (
                await session.execute(
                    select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
                )
            ).scalar_one_or_none()
            if form is None:
                return  # form deleted
            wrote = await record_answer(
                session,
                tenant_id=ref.tenant_id,
                form_id=form.id,
                call_id=call.id,
                field_path=event.field_path,
                raw_value=event.value,
                source=AnswerSource.AI_CALL.value,
                confidence=event.confidence,
                evidence_seq=event.evidence_seq,
            )
            if not wrote:
                return  # idempotent redelivery — value already current
            version = (
                await session.execute(
                    select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
                )
            ).scalar_one()
            await recompute_form_projection(session, form, version.schema_json)
            await self._audit.emit(
                AuditRecord(
                    tenant_id=ref.tenant_id,
                    actor_type=ActorType.SERVICE,
                    actor_user_id=None,
                    actor_label="agent-worker",
                    event_type=AuditEvent.FORM_AI_ANSWER.value,
                    resource_type="patient_form",
                    resource_id=str(form.id),
                    # Field path + call id only — never the extracted value.
                    detail={"field_path": event.field_path, "call_id": str(call.id)},
                )
            )

    async def _close_and_refill(
        self, room_name: str, status: CallStatus, *, trigger: str, ts: int
    ) -> None:
        """Terminal closeout via the shared path, then refill the freed slot
        (dispatch runs AFTER close_call's transaction committed)."""
        room_ref = parse_room_name(room_name)
        if room_ref is not None:
            # Lightweight existence check (no lock) ahead of close_call: a fast
            # call.failed/call.ended can race the dispatch transaction's commit
            # exactly like call.answered does — distinguish "not committed yet"
            # from "genuine voice-lab room" the same way.
            async with tenant_session(self._sessionmaker, room_ref.tenant_id) as session:
                row = (
                    await session.execute(select(Call.id).where(Call.id == room_ref.call_id))
                ).scalar_one_or_none()
            if row is None:
                _retry_young_or_drop(room_name, ts)
                return  # voice-lab room
        closed = await close_call(
            self._sessionmaker, self._audit, room_name, status, trigger=trigger
        )
        if closed is not None:
            ref, applied = closed  # applied may be CANCELED (user-requested end wins)
            await finalize_transcript(self._sessionmaker, self._call_stream, ref, room_name)
            if applied in (CallStatus.COMPLETED, CallStatus.CANCELED):
                # Both park the form in AI_PROCESSING; resolve the lifecycle's
                # next system edge (EXCEPTION_REVIEW or low-completion
                # auto-requeue — suppressed for a canceled call) before
                # refilling — either way a slot is freed.
                await resolve_ai_processing(
                    self._sessionmaker,
                    self._audit,
                    ref,
                    trigger=trigger,
                    auto_retry_enabled=self._form_auto_retry_enabled,
                )
            await run_dispatch_pass(
                self._sessionmaker,
                ref.tenant_id,
                self._livekit,
                self._kms,
                self._audit,
                plan_service=self._call_plans,
            )
