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
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

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
from vera_core.call_health import MAX_REASON_LEN
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
    CallHealthEvent,
    IvrExitedEvent,
    PostCallJob,
    PostCallJobBus,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)
from vera_core.forms.conditions import (
    alternative_fills,
    alternative_index,
    alternative_pairs,
    is_v2,
    routing_branch_fills,
)
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import normalize_percent_value, percent_leaf_paths
from vera_core.forms.review import dispute_view
from vera_core.models import Call, CallEvent, PatientForm, SchemaVersion
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AnswerSource, CallEventType, CallHealthFlag, CallStatus
from vera_core.models.field_answer import CallFormSnapshot
from vera_core.notifications import (
    TYPE_INTERVENTION_NEEDED,
    Notification,
    NotificationAudience,
    NotificationService,
)
from vera_core.observability.correlation import parse_room_name
from vera_core.plan_store import CallPlanService
from vera_core.services.field_answers import (
    baseline_value,
    current_values_by_path,
    recompute_form_projection,
    record_answer,
)

if TYPE_CHECKING:
    from vera_core.observability.correlation import RoomRef
    from vera_core.services.recordings import RecordingConfig

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


# Close events resolve the form (closeout + retry decision) and must not overtake an
# earlier same-room event still pending after a failed handler (stale-projection re-dial).
_CLOSE_EVENT_TYPES: frozenset[str] = frozenset({"call.ended", "call.failed"})
_PENDING_SCAN = 256  # bounded pending-set scan per close-ordering check


def _stream_id_key(stream_id: str) -> tuple[int, int]:
    """(ms, seq) numeric sort key for a Redis stream id — a lexical compare misorders
    across a millisecond digit boundary ("100-0" sorts below "99-0")."""
    ms, _, seq = stream_id.partition("-")
    return (int(ms), int(seq or 0))


def _retry_young_or_drop(room_name: str, ts_ms: int) -> None:
    """A canonical-room event whose Call row isn't there yet: retry if the event is
    young (the dispatcher dials inside the dispatch transaction, so a fast answer/
    failure/end can race the row's commit), else let the caller drop it as a genuine
    voice-lab room. Raises `_RetryEventLater` in the retry case; returns otherwise."""
    if _event_is_young(ts_ms):
        raise _RetryEventLater(room_name)


def _safe_doc(schema_json: Mapping[str, Any]) -> FormSchemaDoc | None:
    """The form's pinned document, or None for a v1 / unparseable schema.

    Fail-soft on purpose: both things this feeds on the answer path (percent
    canonicalization, the either/or fill) refine an answer that is already written, and
    raising here would leave the Redis Streams event unacked and stall the whole answer
    stream on reclaim. Logs the exception TYPE only — a pydantic ValidationError's message
    embeds the document verbatim."""
    if not is_v2(schema_json):
        return None
    try:
        return FormSchemaDoc.model_validate(schema_json)
    except Exception as exc:
        logger.warning(
            "answer normalization and alternatives fill skipped, schema invalid (%s)",
            type(exc).__name__,
        )
        return None


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
        max_close_deliveries: int = 3,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
        form_auto_retry_enabled: bool = False,
        recording: "RecordingConfig | None" = None,
        call_plans: CallPlanService | None = None,
        post_call_bus: PostCallJobBus | None = None,
        notifications: NotificationService | None = None,
    ) -> None:
        self._redis = redis
        self._livekit = livekit
        self._sessionmaker = sessionmaker
        self._kms = kms
        self._audit = audit
        self._call_stream = call_stream
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._max_close_deliveries = max_close_deliveries
        self._teardown_grace_ms = teardown_grace_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._form_auto_retry_enabled = form_auto_retry_enabled
        self._recording = recording
        self._call_plans = call_plans
        self._post_call_bus = post_call_bus
        self._notifications = notifications
        self._bus = WorkerEventBus(redis)
        self._handlers: dict[str, EventHandler] = {
            "call.failed": self._handle_call_failed,
            "call.answered": self._handle_call_answered,
            "ivr.exited": self._handle_ivr_exited,
            "call.ended": self._handle_call_ended,
            "call.answer_recorded": self._handle_call_answer_recorded,
            "call.health": self._handle_call_health,
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
                group_ready = False
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
        # Hold a close behind an earlier same-room event still pending from a prior window.
        room = getattr(event, "room_name", "")
        if event.type in _CLOSE_EVENT_TYPES and room and await self._close_blocked(entry_id, room):
            logger.info(
                "deferring %s (%s) for %s — an earlier same-room event is still pending",
                entry_id,
                event.type,
                room,
            )
            return  # unacked → XAUTOCLAIM redelivers after the predecessor is processed
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

    async def _close_blocked(self, entry_id: str, room_name: str) -> bool:
        """Whether a close event must wait for an earlier-id, same-room entry still
        pending — bounded so a poison predecessor past ``max_close_deliveries`` no longer
        wedges the room."""
        # Read the close's OWN delivery count directly (a single-id XPENDING), so the cap
        # trips no matter how deep the pending set is. If it is not pending we are somehow
        # not its delivery owner — fail OPEN (proceed): deferring a phantom would wedge.
        own = await self._redis.xpending_range(
            WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, min=entry_id, max=entry_id, count=1
        )
        if not own:
            return False
        delivered = int(own[0]["times_delivered"])
        if delivered > self._max_close_deliveries:
            logger.warning(
                "close %s for %s proceeding after %d deliveries despite a possible pending "
                "predecessor — resolution may run on an incomplete projection",
                entry_id,
                room_name,
                delivered,
            )
            return False
        # Earlier-id pending entries only (bounded to ids <= the close, lowest first, so the
        # oldest — the stuck predecessors — are the ones surfaced).
        earlier = await self._redis.xpending_range(
            WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, min="-", max=entry_id, count=_PENDING_SCAN
        )
        close_key = _stream_id_key(entry_id)
        for p in earlier:
            pid = str(p["message_id"])
            if _stream_id_key(pid) >= close_key:
                continue  # the close's own id is the range max — exclude it
            rows = cast(
                "list[tuple[str, dict[str, str]]]",
                await self._redis.xrange(WORKER_EVENTS_STREAM, min=pid, max=pid),
            )
            if rows and _entry_room(rows[0][1]) == room_name:
                return True
        return False

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
            if call.current_status in (CallStatus.ACTIVE.value, CallStatus.CRITICAL.value):
                # Idempotent redelivery (CRITICAL = already live AND health-flagged). But
                # `_handle_call_health`'s escalation branch can flip INITIATED/RINGING ->
                # CRITICAL before this event is processed (the answered-after-health race),
                # in which case started_at is still NULL — backfill it here so `end_call`'s
                # `started_at is None` pre-answer routing doesn't misclassify a live call.
                # No STATUS CallEvent added — stays a no-op redelivery otherwise.
                if call.started_at is None:
                    call.started_at = func.now()
                return
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

    async def _handle_ivr_exited(self, event: WorkerEvent) -> None:
        """Stamp the VR2-45 IVR Success numerator on the DB clock — the NULL guard
        makes redelivery a no-op."""
        if not isinstance(event, IvrExitedEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return  # non-vera / console room
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id).with_for_update())
            ).scalar_one_or_none()
            if call is None:
                _retry_young_or_drop(event.room_name, event.ts)
                return  # voice-lab room (or the Call row hasn't committed yet → retry)
            if call.ivr_exited_at is None:
                call.ivr_exited_at = func.now()

    async def _handle_call_ended(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallEndedEvent):
            return
        await self._close_and_refill(
            event.room_name, CallStatus.COMPLETED, trigger="call.ended", ts=event.ts
        )

    @staticmethod
    def _under_a_routing_branch(doc: FormSchemaDoc, path: str) -> bool:
        """Whether `path` sits under either side of a routing `alternatives`, in which case
        answering it may make the OTHER branch fillable."""
        return any(
            path.startswith(f"{member}.")
            for section in doc.sections.values()
            for alternatives in section.alternatives or []
            for member in alternatives.members
        )

    async def _fill_alternatives(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        form_id: UUID,
        call_id: UUID,
        answered: str,
        doc: FormSchemaDoc | None,
    ) -> None:
        """Record the unused side of an either/or the answer just satisfied — see
        `alternative_fills`, which owns what may be written and refuses anything unsafe.

        Written as `ai_call` because it is derived from a call answer, so `record_answer`'s replay
        and no-blank guards make a redelivery a no-op. The values written are the leaves' own
        authored `inapplicable_value`s, which are already canonical, so they need no normalizing.

        `doc` is None for a v1 schema (no `alternatives` concept) or one that will not validate —
        `_safe_doc` owns that fail-soft decision, since the caller needs the same document."""
        if doc is None:
            return
        # Pair membership needs no values, but a routing fill can be triggered by ANY leaf under a
        # branch, so the snapshot is only skippable when neither applies.
        in_pair = answered in alternative_index(alternative_pairs(doc))
        if not in_pair and not self._under_a_routing_branch(doc, answered):
            return
        values = await current_values_by_path(session, form_id)
        fills = dict(routing_branch_fills(doc, values))
        if in_pair:
            fills.update(alternative_fills(doc, values, answered))
        for path, value in fills.items():
            await record_answer(
                session,
                tenant_id=tenant_id,
                form_id=form_id,
                call_id=call_id,
                field_path=path,
                raw_value=value,
                source=AnswerSource.AI_CALL.value,
                confidence=None,
                evidence_seq=None,
            )

    async def _handle_call_answer_recorded(self, event: WorkerEvent) -> None:
        """Persist an Observer-extracted answer as an ai_call field_answer, then re-derive
        the form's promoted columns + completion_pct, then relay it onto the per-call SSE
        stream. Idempotent under redelivery (the writer no-ops an unchanged value); the form
        row lock serializes against a concurrent human resolve on the same form."""
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
            call_is_terminal = call.current_status in TERMINAL_VALUES
            form = (
                await session.execute(
                    select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
                )
            ).scalar_one_or_none()
            if form is None:
                return  # form deleted
            # Read BEFORE the write (it used to follow it) because the answer's canonical
            # form is schema-derived. The control plane owns `field_answer`, so it — not the
            # separately deployed worker — is where a value is canonicalized: a rolled-back
            # worker would otherwise persist uncanonical values forever, after the one-time
            # backfill migration has already run.
            version = (
                await session.execute(
                    select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
                )
            ).scalar_one()
            doc = _safe_doc(version.schema_json)
            # Canonicalized ONCE and reused by all three consumers below (the row, the live
            # dispute, the SSE frame). Normalizing only the row would leave Live Monitoring
            # showing a bare "20" against a "20%" row, and would have `dispute_view` compare
            # an uncanonical value against a canonical baseline — a false dispute.
            value = event.value
            if doc is not None and (rule := percent_leaf_paths(doc).get(event.field_path)):
                value = normalize_percent_value(value, rule)
            wrote = await record_answer(
                session,
                tenant_id=ref.tenant_id,
                form_id=form.id,
                call_id=call.id,
                field_path=event.field_path,
                raw_value=value,
                source=AnswerSource.AI_CALL.value,
                confidence=event.confidence,
                evidence_seq=event.evidence_seq,
            )
            if not wrote:
                return  # idempotent redelivery — value already current
            # Before the projection, so completion_pct counts the fills this answer closes out.
            await self._fill_alternatives(
                session,
                tenant_id=ref.tenant_id,
                form_id=form.id,
                call_id=call.id,
                answered=event.field_path,
                doc=doc,
            )
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
            # Closeout already deleted this call's stream; an XADD would recreate it and
            # pin every client to a dead call. The DB row is written — just skip the relay,
            # and skip the baseline read it would need. See the test for the full chain.
            if call_is_terminal:
                return
            # Everything the relay needs, read while the row is still live in this session.
            dispute = dispute_view(
                source=AnswerSource.AI_CALL.value,
                value=value,
                confidence=event.confidence,
                evidence=None,
                baseline_value=await baseline_value(session, form.id, event.field_path),
            )
            completion_pct = float(form.completion_pct)

        # Committed and unlocked — see the docstring.
        await self._call_stream.publish_field_answer(
            event.room_name,
            field_path=event.field_path,
            value=value,
            confidence=event.confidence,
            evidence_seq=event.evidence_seq,
            completion_pct=completion_pct,
            dispute=dispute,
            ts=event.ts,
        )

    async def _handle_call_health(self, event: WorkerEvent) -> None:
        """Persist one observer analysis (spec §4.3). Every surviving analysis
        updates the denormalized Call columns; a CallEvent(HEALTH) row, the
        ACTIVE<->CRITICAL flip, and a notification happen only on episode
        transitions — escalation immediately, recovery after 2 consecutive
        healthy results (asymmetric hysteresis, spec edge #4). Late results are
        DROPPED, never retried: unlike lifecycle events, a health frame is
        transient and superseded by the next analysis."""
        if not isinstance(event, CallHealthEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return
        analyzed_at = datetime.fromtimestamp(event.ts / 1000, tz=UTC)
        notification: Notification | None = None
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id).with_for_update())
            ).scalar_one_or_none()
            if call is None:
                return  # voice-lab room / dropped row
            if call.current_status in TERMINAL_VALUES:
                return  # analysis finished after the call ended
            if call.intervener_user_id is not None:
                return  # takeover raced the in-flight analysis
            if call.health_analyzed_at is not None and analyzed_at <= call.health_analyzed_at:
                return  # consumer-group redelivery / out-of-order duplicate
            prior_flag = call.health_flag
            in_episode = call.current_status == CallStatus.CRITICAL.value
            flagged = event.flag != CallHealthFlag.NONE.value
            call.health_score = event.score
            call.health_flag = event.flag
            # Defense in depth on the VARCHAR(500) column: the producer already
            # caps the reason, but an oversized event must degrade to truncation,
            # not a write error — a failing handler stays unacked and would be
            # redelivered forever (poison loop).
            call.health_reason = event.reason[:MAX_REASON_LEN]
            call.health_analyzed_at = analyzed_at
            detail: dict[str, object] = {
                "score": event.score,
                "reason": event.reason,
                "turn_count": event.turn_count,
            }
            transition_flag: str | None = None
            if flagged and not in_episode:
                transition_flag = event.flag  # open an episode (escalation: immediate)
                # Always flip, regardless of current_status: the terminal guard above
                # already dropped terminal statuses, and `in_episode` already excludes
                # CRITICAL, so whatever reaches here (normally ACTIVE, but also
                # INITIATED/RINGING/IVR/WAITING if call.health races call.answered's
                # commit) is safe to promote. This closes the health-before-answered
                # reorder race — `_handle_call_answered` already treats CRITICAL as
                # "already live" and skips the ACTIVE flip in that case.
                call.current_status = CallStatus.CRITICAL.value
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.CRITICAL.value,
                    )
                )
            elif flagged and in_episode:
                # Compare against the EPISODE category (the last HEALTH row), not
                # the per-analysis flag — a single healthy blip must not make the
                # same category read as a brand-new episode (spec §4.3).
                episode_flag = (
                    await session.execute(
                        select(CallEvent.event_value)
                        .where(
                            CallEvent.call_id == call.id,
                            CallEvent.event_type == CallEventType.HEALTH.value,
                        )
                        .order_by(CallEvent.created_at.desc(), CallEvent.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if event.flag != episode_flag:
                    transition_flag = event.flag  # category change while flagged
            elif in_episode and prior_flag == CallHealthFlag.NONE.value:
                # Second consecutive healthy result — close the episode. The
                # (prior_flag == none AND status CRITICAL) pair IS the 2-streak;
                # no counter column needed. No notification on recovery.
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.HEALTH.value,
                        event_value=CallHealthFlag.NONE.value,
                        detail=detail,
                    )
                )
                call.current_status = CallStatus.ACTIVE.value
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.ACTIVE.value,
                    )
                )
            if transition_flag is not None:
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.HEALTH.value,
                        event_value=transition_flag,
                        detail=detail,
                    )
                )
                # Routing rule (spec §4.4): unpublished -> owner only; published
                # or ownerless (tenant-visible in the list) -> tenant-wide.
                audience = (
                    NotificationAudience(kind="tenant")
                    if call.published or call.initiated_by_id is None
                    else NotificationAudience(kind="user", user_id=str(call.initiated_by_id))
                )
                notification = Notification(
                    type=TYPE_INTERVENTION_NEEDED,
                    audience=audience,
                    # Minimum-necessary (2026-07-18 final-review amendment): `reason` is
                    # dropped here — no consumer reads it (the toast shows flag+score
                    # only) — but stays in CallEvent.detail for reporting.
                    data={
                        "call_id": str(call.id),
                        "score": event.score,
                        "flag": event.flag,
                    },
                    ts=event.ts,
                )
        # After the transaction committed — receivers who refetch see the new state.
        if notification is not None and self._notifications is not None:
            try:
                await self._notifications.publish(ref.tenant_id, notification)
            except Exception as exc:  # payload is PHI — type name only
                logger.warning(
                    "intervention notification publish failed for %s (%s)",
                    event.room_name,
                    type(exc).__name__,
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
                # Both park the form in AI_PROCESSING. When the post-call eval
                # consumer is wired, hand it the resolution: snapshot the form's
                # pre-eval answers and enqueue a job — evaluate_call owns the
                # transition out of AI_PROCESSING (and its own dispatch pass).
                # Without it (no GCP project), resolve synchronously as before;
                # either way the sweeper still covers a crash in between.
                if self._post_call_bus is not None:
                    await self._enqueue_post_call_eval(ref)
                else:
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
                recording=self._recording,
                plan_service=self._call_plans,
            )

    async def _enqueue_post_call_eval(self, ref: "RoomRef") -> None:
        """Write the pre-eval CallFormSnapshot and enqueue the eval job.

        Redelivery-safe: a second closeout of the same call finds the form no
        longer in AI_PROCESSING inside evaluate_call, which skips it."""
        assert self._post_call_bus is not None
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id))
            ).scalar_one_or_none()
            if call is None:
                return  # voice-lab room — no pipeline form
            form_id = call.form_id
            existing = (
                await session.execute(
                    select(CallFormSnapshot.id).where(CallFormSnapshot.call_id == ref.call_id)
                )
            ).scalar_one_or_none()
            if existing is None:  # redelivered closeout → snapshot already taken
                session.add(
                    CallFormSnapshot(
                        tenant_id=ref.tenant_id,
                        call_id=ref.call_id,
                        before_state=await current_values_by_path(session, form_id),
                        after_state={},
                    )
                )
        await self._post_call_bus.emit(
            PostCallJob(tenant_id=ref.tenant_id, form_id=form_id, call_id=ref.call_id)
        )
