"""Unit tests for the control-plane worker-event consumer (no live Redis, no live DB).

The DB seam (`tenant_session`, imported into both `worker_events` and
`call_closeout`) and the post-closeout `run_dispatch_pass` refill are
monkeypatched per-test via `_consumer()`, which routes queries through a
`_FakeSession` keyed by target entity — mirroring `FakeSession` in
`tests/unit/services/test_queue_dispatcher.py`.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

import control_plane.call_closeout as call_closeout
import control_plane.post_call as post_call
import control_plane.transcript_finalizer as transcript_finalizer
import control_plane.worker_events as worker_events
from control_plane.call_closeout import TERMINAL_VALUES
from control_plane.livekit_gateway import LiveKitGateway
from control_plane.worker_events import WorkerEventConsumer
from vera_core.audit import AuditRecord
from vera_core.call_stream import CallStreamEvent
from vera_core.events import (
    CallAnsweredEvent,
    CallAnswerRecordedEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
    IvrExitedEvent,
)
from vera_core.models import Call, PatientForm, SchemaVersion, Tenant
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer
from vera_core.observability.correlation import room_name_for_call

_VALID_ROOM = f"call--{uuid4()}--{uuid4()}"


class _FakeLiveKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_metadata = False

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        if self.fail_metadata:
            raise RuntimeError("boom")
        self.calls.append(("meta", (room_name, metadata)))

    async def delete_room(self, room_name: str) -> None:
        self.calls.append(("delete", room_name))


class _FakeRedis:
    def __init__(self) -> None:
        self.acked: list[str] = []
        # Close-ordering guard reads the group's pending set + individual entries.
        # `pending`: redis-py xpending_range shape; `entries`: id -> fields for xrange.
        self.pending: list[dict[str, object]] = []
        self.entries: dict[str, dict[str, str]] = {}
        # Configured per-test to mimic redis-py's decoded XREADGROUP/XAUTOCLAIM shapes.
        self.xreadgroup_response: object = None
        self.xautoclaim_response: object = ("0-0", [], [])
        # When set, xreadgroup raises it — used to mimic redis-py turning a BLOCK
        # window with no new entries into a raised TimeoutError, or a CancelledError
        # (a BaseException) to break out of the run loop in a test.
        self.xreadgroup_error: BaseException | None = None
        # Group-bootstrap bookkeeping: count ensure_group() calls, and let a test
        # queue per-call xautoclaim errors (e.g. a one-shot NOGROUP) to exercise recovery.
        self.xgroup_create_calls = 0
        self.xautoclaim_errors: list[Exception] = []

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        return 1

    async def xpending_range(
        self, name: str, groupname: str, min: str, max: str, count: int, **kw: object
    ) -> list[dict[str, object]]:
        def key(mid: str) -> tuple[int, int]:
            ms, _, seq = mid.partition("-")
            return (int(ms), int(seq or 0))

        lo = None if min == "-" else key(min)
        hi = None if max == "+" else key(max)
        out = [
            p
            for p in self.pending
            if (lo is None or key(str(p["message_id"])) >= lo)
            and (hi is None or key(str(p["message_id"])) <= hi)
        ]
        return out[:count]

    async def xrange(
        self, name: str, min: str = "-", max: str = "+", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        return [(min, self.entries[min])] if min in self.entries else []

    async def xgroup_create(
        self, name: str, groupname: str, id: str = "0", mkstream: bool = False
    ) -> bool:
        self.xgroup_create_calls += 1
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> object:
        if self.xreadgroup_error is not None:
            raise self.xreadgroup_error
        return self.xreadgroup_response

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> object:
        if self.xautoclaim_errors:
            raise self.xautoclaim_errors.pop(0)
        return self.xautoclaim_response


# ---------------------------------------------------------------------------
# DB seam fakes — a minimal AsyncSession stand-in routed by target entity, and
# an AuditSink spy. Both plug into `tenant_session` for the consumer/closeout.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, scalar: Any) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar


class _FakeSession:
    """Routes `execute()` by the statement's target entity (Call / PatientForm /
    Tenant) for SELECTs, or records it for the finalizer's transcript INSERT.
    Defaults to "no Call row" — matches a voice-lab room whose synthetic call id
    never made it into the table."""

    def __init__(
        self,
        *,
        call: Any = None,
        form: Any = None,
        tenant: Any = None,
        schema_version: Any = None,
    ) -> None:
        self.call = call
        self.form = form
        self.tenant = tenant
        self.schema_version = schema_version
        self.added: list[Any] = []
        self.queried: list[type] = []
        self.inserted: list[Any] = []

    async def execute(self, stmt: Any) -> _Result | None:
        if isinstance(stmt, Insert):
            self.inserted.append(stmt)
            return None
        entity = stmt.column_descriptions[0]["entity"]
        self.queried.append(entity)
        if entity is Call:
            return _Result(self.call)
        if entity is PatientForm:
            return _Result(self.form)
        if entity is Tenant:
            return _Result(self.tenant)
        if entity is SchemaVersion:
            return _Result(self.schema_version)
        raise AssertionError(f"unexpected query entity {entity}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)


class _FakeCallStream:
    """In-memory call-stream double for the finalizer: read_all returns a fixed
    snapshot until clear() runs (mirrors a real Redis DEL). Also records the
    terminal-status announcements (publish_status/end) the closeout paths push
    for supervisors watching the live SSE."""

    def __init__(self, events: list[CallStreamEvent] | None = None) -> None:
        self._events = events or []
        self.cleared: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.ended: list[str] = []
        self.field_answers: list[dict[str, Any]] = []

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return self._events

    async def clear(self, room_name: str) -> None:
        self.cleared.append(room_name)
        self._events = []

    async def publish_status(self, room_name: str, status: str, *, ts: int) -> None:
        self.published.append((room_name, status))

    async def publish_field_answer(self, room_name: str, **kwargs: Any) -> None:
        self.field_answers.append({"room_name": room_name, **kwargs})

    async def end(self, room_name: str) -> None:
        self.ended.append(room_name)


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _SpyAudit:
    """AuditSink that records every emitted record for inspection."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


@dataclass
class _Wired:
    consumer: WorkerEventConsumer
    session: _FakeSession
    audit: _SpyAudit
    call_stream: _FakeCallStream
    dispatch_calls: list[tuple[Any, ...]] = field(default_factory=list)


def _consumer(
    monkeypatch: pytest.MonkeyPatch,
    redis: _FakeRedis,
    livekit: _FakeLiveKit,
    *,
    session: _FakeSession | None = None,
    call_stream: _FakeCallStream | None = None,
    form_auto_retry_enabled: bool = False,
    post_call_bus: Any = None,
) -> _Wired:
    """Wire a consumer to a fake DB seam and a fake dispatch pass. `tenant_session`
    is monkeypatched in `worker_events` (the answered handler), `call_closeout` (the
    shared terminal closeout), and `transcript_finalizer` (the post-closeout
    finalizer) to the SAME fake session, so a test can seed one Call/PatientForm/
    Tenant set and see it through every path.
    """
    fake_session = session if session is not None else _FakeSession()
    fake_audit = _SpyAudit()
    fake_call_stream = call_stream if call_stream is not None else _FakeCallStream()
    dispatch_calls: list[tuple[Any, ...]] = []

    def _fake_tenant_session(sm: Any, tid: Any) -> _FakeSessionCtx:
        return _FakeSessionCtx(fake_session)

    monkeypatch.setattr(call_closeout, "tenant_session", _fake_tenant_session)
    monkeypatch.setattr(worker_events, "tenant_session", _fake_tenant_session)
    monkeypatch.setattr(transcript_finalizer, "tenant_session", _fake_tenant_session)
    monkeypatch.setattr(post_call, "tenant_session", _fake_tenant_session)

    async def _fake_run_dispatch_pass(
        sessionmaker: Any,
        tenant_id: Any,
        lk: Any,
        kms: Any,
        aud: Any,
        *,
        recording: Any = None,
        plan_service: Any = None,
    ) -> None:
        dispatch_calls.append((tenant_id, lk, kms, aud))

    monkeypatch.setattr(worker_events, "run_dispatch_pass", _fake_run_dispatch_pass)

    consumer = WorkerEventConsumer(
        cast(Redis, redis),
        cast(LiveKitGateway, livekit),
        cast("async_sessionmaker[AsyncSession]", object()),
        object(),
        fake_audit,
        fake_call_stream,  # type: ignore[arg-type]
        teardown_grace_ms=0,
        form_auto_retry_enabled=form_auto_retry_enabled,
        post_call_bus=post_call_bus,
    )
    return _Wired(
        consumer=consumer,
        session=fake_session,
        audit=fake_audit,
        call_stream=fake_call_stream,
        dispatch_calls=dispatch_calls,
    )


def _event_fields(room: str = _VALID_ROOM) -> dict[str, str]:
    ev = CallFailedEvent(room_name=room, reason=CallFailureReason.NO_ANSWER, ts=1)
    return {"event": ev.model_dump_json()}


def _tenant(**overrides: Any) -> Tenant:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "name": "Test Tenant",
        "slug": f"tenant-{uuid4().hex[:8]}",
        "status": "active",
        "max_agents_per_va": 3,
        "max_retries": 3,
        "retry_fill_threshold": 0.95,
        "queue_expiry_hours": 48,
        "persona_tweak": {},
        "auto_retry_enabled": True,
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _call_row(tenant_id: UUID, call_id: UUID, form_id: UUID, **overrides: Any) -> Call:
    defaults: dict[str, Any] = {
        "id": call_id,
        "tenant_id": tenant_id,
        "form_id": form_id,
        "current_status": CallStatus.ACTIVE.value,
        "started_at": None,
        "ended_at": None,
    }
    defaults.update(overrides)
    return Call(**defaults)


def _form_row(tenant_id: UUID, form_id: UUID, **overrides: Any) -> PatientForm:
    defaults: dict[str, Any] = {
        "id": form_id,
        "tenant_id": tenant_id,
        "schema_version_id": uuid4(),
        "status": FormStatus.IN_CALL.value,
        "patient_name": "Jane Doe",
        "insurance_provider_phone_number": "+15551234567",
        "retry_count": 0,
        "completion_pct": 100.0,
        "enqueued_at": None,
    }
    defaults.update(overrides)
    return PatientForm(**defaults)


@pytest.mark.asyncio
async def test_handle_call_failed_sets_metadata_then_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._process("1-0", _event_fields())
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["1-0"]
    # A supervisor already tailing the live SSE learns the call died: the mapped
    # terminal status is pushed onto the per-call event stream, then the stream
    # is ended so readers stop (the worker never publishes for a failed dial —
    # no session ever existed).
    assert wired.call_stream.published == [(_VALID_ROOM, "no_answer")]
    assert wired.call_stream.ended == [_VALID_ROOM]


@pytest.mark.asyncio
async def test_ignores_non_vera_room(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._process("2-0", _event_fields(room="lobby"))
    assert livekit.calls == []  # not torn down
    assert redis.acked == ["2-0"]  # but acked (nothing to retry)


@pytest.mark.asyncio
async def test_missing_event_field_is_acked_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._process("3-0", {"nope": "x"})
    assert livekit.calls == []
    assert redis.acked == ["3-0"]


@pytest.mark.asyncio
async def test_unparseable_event_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._process("4-0", {"event": "{not json"})
    assert livekit.calls == []
    assert redis.acked == ["4-0"]


@pytest.mark.asyncio
async def test_handler_failure_leaves_entry_unacked(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    livekit.fail_metadata = True
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._process("5-0", _event_fields())
    assert redis.acked == []  # left pending for XAUTOCLAIM to retry


@pytest.mark.asyncio
async def test_unknown_event_type_is_acked_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    wired.consumer._handlers = {}  # remove all handlers so call.failed has no handler
    await wired.consumer._process("6-0", _event_fields())
    assert livekit.calls == []  # no teardown
    assert redis.acked == ["6-0"]  # entry is acked despite no handler


@pytest.mark.asyncio
async def test_read_once_unpacks_xreadgroup_response_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives `_read_once` with a realistic decoded XREADGROUP shape (decode_responses=True,
    no `justid`): `[[stream_name, [(entry_id, fields), ...]]]`.
    """
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xreadgroup_response = [
        ["vera:worker-events", [("10-0", _event_fields())]],
    ]
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._read_once()
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["10-0"]


@pytest.mark.asyncio
async def test_reclaim_stale_unpacks_xautoclaim_response_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives `_reclaim_stale` with a realistic decoded XAUTOCLAIM shape:
    `(cursor, [(entry_id, fields), ...], deleted_ids)`.
    """
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xautoclaim_response = ("0-0", [("11-0", _event_fields())], [])
    wired = _consumer(monkeypatch, redis, livekit)
    await wired.consumer._reclaim_stale()
    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == ["11-0"]


@pytest.mark.asyncio
async def test_read_once_treats_block_timeout_as_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """redis-py turns an XREADGROUP BLOCK window with no new entries into a raised
    redis.exceptions.TimeoutError. That is a normal idle tick, not an error: _read_once
    must swallow it and return quietly (no teardown, no exception propagated to the
    run loop's generic RedisError back-off)."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    redis, livekit = _FakeRedis(), _FakeLiveKit()
    redis.xreadgroup_error = RedisTimeoutError("Timeout reading from localhost:6379")
    wired = _consumer(monkeypatch, redis, livekit)
    # Must not raise.
    await wired.consumer._read_once()
    assert livekit.calls == []
    assert redis.acked == []


@pytest.mark.asyncio
async def test_run_reensures_group_after_nogroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NOGROUP mid-loop (group dropped by a Redis flush, or the stream recreated by a
    producer's XADD before the group existed) must re-bootstrap the group, not spin on the
    error forever. The run loop resets group_ready so ensure_group re-runs next pass."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    # First reclaim raises NOGROUP; the second pass must call ensure_group again and
    # then break out of the loop via a cancel on the following read.
    redis.xautoclaim_errors = [ResponseError("NOGROUP No such key or consumer group")]
    redis.xreadgroup_error = asyncio.CancelledError()
    wired = _consumer(monkeypatch, redis, livekit)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await wired.consumer.run()

    # Once at startup, once after the NOGROUP reset — proving recovery.
    assert redis.xgroup_create_calls == 2


# ---------------------------------------------------------------------------
# Task 7: the consumer closes the call loop — DB writes + slot refill.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_answered_activates_call_and_stamps_started_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.INITIATED.value)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=_FakeSession(call=call))

    event = CallAnsweredEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.ACTIVE.value
    assert call.started_at is not None
    status_events = [e for e in wired.session.added if e.event_type == CallEventType.STATUS.value]
    assert len(status_events) == 1
    assert status_events[0].event_value == CallStatus.ACTIVE.value
    assert wired.dispatch_calls == []  # not a terminal event — no refill
    assert wired.call_stream.cleared == []  # not a closeout — finalizer never runs
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_ivr_exited_stamps_timestamp_once(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=_FakeSession(call=call))

    event = IvrExitedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    stamped = call.ivr_exited_at
    assert stamped is not None

    # Redelivery is a no-op: the first stamp survives.
    await wired.consumer._process("1-1", {"event": event.model_dump_json()})
    assert call.ivr_exited_at is stamped
    assert redis.acked == ["1-0", "1-1"]


@pytest.mark.asyncio
async def test_ivr_exited_ignores_non_vera_room(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    event = IvrExitedEvent(room_name="console-room", ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})
    assert wired.session.queried == []  # short-circuited before any DB hit
    assert redis.acked == ["1-0"]  # acked (nothing to retry)


@pytest.mark.asyncio
async def test_call_ended_routes_form_through_ai_processing_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle's happy path: call complete → AI_PROCESSING → (high
    completion) EXCEPTION_REVIEW. The form must never land on COMPLETED —
    that edge is reserved for a reviewer's manual approve."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, completion_pct=100.0)
    tenant = _tenant(id=tenant_id, max_retries=3)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.COMPLETED.value
    assert call.ended_at is not None
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    status_events = [e for e in session.added if e.event_type == CallEventType.STATUS.value]
    assert len(status_events) == 1
    assert status_events[0].event_value == CallStatus.COMPLETED.value

    # Two audited form transitions: IN_CALL → AI_PROCESSING (closeout), then
    # AI_PROCESSING → EXCEPTION_REVIEW (post-call resolution).
    assert [
        (r.detail["from"], r.detail["to"])
        for r in wired.audit.records
        if r.event_type == AuditEvent.FORM_STATUS_CHANGE.value
    ] == [
        (FormStatus.IN_CALL.value, FormStatus.AI_PROCESSING.value),
        (FormStatus.AI_PROCESSING.value, FormStatus.EXCEPTION_REVIEW.value),
    ]

    assert len(wired.dispatch_calls) == 1
    assert wired.dispatch_calls[0][0] == tenant_id  # refill ran for the freed tenant
    assert redis.acked == ["1-0"]


class _EmptyRows:
    """SELECT result with no rows — supports .all() and the scalar accessors."""

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> Any:
        return None


class _EvalSeamSession(_FakeSession):
    """Extends the entity router for the enqueue seam's FieldAnswer /
    CallFormSnapshot lookups (no rows: fresh form, no prior snapshot)."""

    async def execute(self, stmt: Any) -> Any:
        if not isinstance(stmt, Insert):
            entity = stmt.column_descriptions[0]["entity"]
            if entity in (FieldAnswer, CallFormSnapshot):
                return _EmptyRows()
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_call_ended_with_eval_bus_snapshots_and_enqueues_instead_of_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-call eval consumer is wired, closeout hands it the
    resolution: the form stays in AI_PROCESSING, a before-state CallFormSnapshot
    is written, exactly one PostCallJob is emitted, and the refill still runs."""

    class _SpyBus:
        def __init__(self) -> None:
            self.emitted: list[Any] = []

        async def emit(self, job: Any) -> None:
            self.emitted.append(job)

    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, completion_pct=40.0)
    tenant = _tenant(id=tenant_id, max_retries=3)
    session = _EvalSeamSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    bus = _SpyBus()
    wired = _consumer(monkeypatch, redis, livekit, session=session, post_call_bus=bus)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.COMPLETED.value
    # The eval consumer owns the transition out of AI_PROCESSING — closeout
    # must NOT resolve synchronously when the bus is wired.
    assert form.status == FormStatus.AI_PROCESSING.value
    snapshots = [obj for obj in session.added if isinstance(obj, CallFormSnapshot)]
    assert len(snapshots) == 1
    assert snapshots[0].after_state == {}
    assert [(j.tenant_id, j.form_id, j.call_id) for j in bus.emitted] == [
        (tenant_id, form_id, call_id)
    ]
    assert len(wired.dispatch_calls) == 1  # slotwise refill still runs
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_call_ended_low_completion_auto_requeues_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagram's "system auto-retry: low completion" edge (feature-gated,
    enabled here): a completed call whose form fill is below the tenant
    threshold goes back to IN_QUEUE."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, completion_pct=40.0)
    tenant = _tenant(id=tenant_id, max_retries=3, retry_fill_threshold=0.95)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session, form_auto_retry_enabled=True)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.COMPLETED.value
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1
    assert form.enqueued_at is not None
    assert len(wired.dispatch_calls) == 1  # the requeued form gets a dispatch pass
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_call_ended_low_completion_defaults_to_review_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default wiring (auto-retry flag OFF): low completion goes to
    EXCEPTION_REVIEW without consuming the retry budget — no form-filling
    mechanism exists yet, so a redial could never improve completion."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, completion_pct=40.0)
    tenant = _tenant(id=tenant_id, max_retries=3, retry_fill_threshold=0.95)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.retry_count == 0
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_call_ended_after_user_end_request_closes_as_canceled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live path of /calls/{id}/end stamps end-intent and deletes the room;
    the worker then emits a plain call.ended. The closeout must honor the stamp:
    CANCELED, never COMPLETED. The form still rides the post-call pipeline (the
    transcript may carry extractable data) and lands in EXCEPTION_REVIEW —
    never auto-requeued, even with the auto-retry flag on and low completion
    (a supervisor who ended the call does not want the payer redialed)."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(
        tenant_id,
        call_id,
        form_id,
        current_status=CallStatus.ACTIVE.value,
        end_requested_by_id=uuid4(),
    )
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, completion_pct=40.0)
    tenant = _tenant(id=tenant_id, max_retries=3, retry_fill_threshold=0.95)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session, form_auto_retry_enabled=True)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.CANCELED.value
    assert form.status == FormStatus.EXCEPTION_REVIEW.value  # for a human; NOT re-queued
    assert form.retry_count == 0
    status_events = [e for e in session.added if e.event_type == CallEventType.STATUS.value]
    assert [e.event_value for e in status_events] == [CallStatus.CANCELED.value]
    # Two audited form transitions: IN_CALL → AI_PROCESSING (closeout), then
    # AI_PROCESSING → EXCEPTION_REVIEW (the resolver's canceled gate refused
    # the low-completion auto-requeue).
    assert [
        (r.detail["from"], r.detail["to"])
        for r in wired.audit.records
        if r.event_type == AuditEvent.FORM_STATUS_CHANGE.value
    ] == [
        (FormStatus.IN_CALL.value, FormStatus.AI_PROCESSING.value),
        (FormStatus.AI_PROCESSING.value, FormStatus.EXCEPTION_REVIEW.value),
    ]
    assert len(wired.dispatch_calls) == 1  # the slot was still freed
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_call_ended_finalizes_transcript_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 16: on a `call.ended` closeout, the finalizer drains the room's event
    stream into `transcript` rows and clears the stream, BEFORE the dispatch pass
    that refills the freed slot."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value)
    tenant = _tenant(id=tenant_id, max_retries=3)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    call_stream = _FakeCallStream(
        [
            CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1),
            CallStreamEvent(type="transcript", data={"role": "agent", "text": "hello"}, ts=2),
        ]
    )
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session, call_stream=call_stream)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert len(session.inserted) == 1  # one multi-row transcript insert
    assert call_stream.cleared == [room]
    assert len(wired.dispatch_calls) == 1  # finalize ran before the refill, not instead of it


@pytest.mark.asyncio
async def test_call_failed_maps_reason_updates_rows_and_tears_room_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value, retry_count=0)
    tenant = _tenant(id=tenant_id, max_retries=3)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    call_stream = _FakeCallStream(
        [CallStreamEvent(type="transcript", data={"role": "user", "text": "hi"}, ts=1)]
    )
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session, call_stream=call_stream)

    event = CallFailedEvent(room_name=room, reason=CallFailureReason.NO_ANSWER, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    # Existing room-teardown behavior preserved.
    assert livekit.calls == [
        ("meta", (room, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", room),
    ]

    assert call.current_status == CallStatus.NO_ANSWER.value
    assert form.status == FormStatus.IN_QUEUE.value  # auto-requeued, retries remain
    assert form.retry_count == 1
    assert form.enqueued_at is not None
    status_events = [e for e in session.added if e.event_type == CallEventType.STATUS.value]
    assert status_events[0].event_value == CallStatus.NO_ANSWER.value

    assert len(session.inserted) == 1  # call.failed is a terminal closeout too — finalizes
    assert call_stream.cleared == [room]
    assert len(wired.dispatch_calls) == 1
    assert wired.dispatch_calls[0][0] == tenant_id
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_events_for_rooms_without_call_row_touch_no_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voice-lab room's call id never made it into `call` — every handler must
    no-op its DB work. `call.failed` still tears the room down."""
    room = f"call--{uuid4()}--{uuid4()}"  # parses fine, but no seeded Call row
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    failed = CallFailedEvent(room_name=room, reason=CallFailureReason.NO_ANSWER, ts=1)
    await wired.consumer._process("1-0", {"event": failed.model_dump_json()})

    assert livekit.calls == [
        ("meta", (room, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", room),
    ]
    assert wired.session.added == []
    assert wired.dispatch_calls == []

    answered = CallAnsweredEvent(room_name=room, ts=1)
    await wired.consumer._process("2-0", {"event": answered.model_dump_json()})
    assert wired.session.added == []

    ended = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("3-0", {"event": ended.model_dump_json()})
    assert wired.session.added == []
    assert wired.dispatch_calls == []
    assert redis.acked == ["1-0", "2-0", "3-0"]
    # close_call returned None for every event (no Call row) — the finalizer never ran.
    assert wired.session.inserted == []
    assert wired.call_stream.cleared == []


@pytest.mark.asyncio
async def test_terminal_events_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redelivered call.ended on an already-completed call is a no-op: the row
    lock's terminal short-circuit stops it before any write or refill."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.COMPLETED.value)
    session = _FakeSession(call=call)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.COMPLETED.value  # untouched
    assert session.added == []  # no CallEvent row
    # Two Call queries: _close_and_refill's pre-close existence check, then
    # close_call's own row-locking query, which short-circuits before PatientForm/Tenant.
    assert session.queried == [Call, Call]
    assert wired.dispatch_calls == []  # no slot freed — no refill
    assert session.inserted == []  # close_call returned None — the finalizer never ran
    assert wired.call_stream.cleared == []
    assert redis.acked == ["1-0"]


# ---------------------------------------------------------------------------
# Fix: worker events racing the dispatch transaction's commit. A fast
# call.answered/call.failed/call.ended can arrive before the Call row commits
# (the dispatcher dials inside the dispatch transaction) — a "young" event with
# no Call row must be retried (left unacked) rather than treated as voice-lab.
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.mark.asyncio
async def test_young_call_answered_with_no_row_is_retried_not_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call.answered for a room with no Call row yet, timestamped "now", must be
    left unacked (redelivered later) instead of being treated as a voice-lab room."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    event = CallAnsweredEvent(room_name=_VALID_ROOM, ts=_now_ms())
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert redis.acked == []  # left pending for XAUTOCLAIM to redeliver
    assert wired.session.added == []  # no DB mutation
    assert wired.dispatch_calls == []


@pytest.mark.asyncio
async def test_old_call_answered_with_no_row_is_acked_as_voice_lab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call.answered for a room with no Call row, timestamped long ago (past the
    retry window), is a genuine voice-lab room — acked normally, no DB mutation."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    event = CallAnsweredEvent(room_name=_VALID_ROOM, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert redis.acked == ["1-0"]
    assert wired.session.added == []
    assert wired.dispatch_calls == []


@pytest.mark.asyncio
async def test_young_call_ended_with_no_row_is_retried_not_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same race for call.ended, which goes through _close_and_refill."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    event = CallEndedEvent(room_name=_VALID_ROOM, ts=_now_ms())
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert redis.acked == []
    assert wired.session.added == []
    assert wired.dispatch_calls == []


@pytest.mark.asyncio
async def test_young_call_failed_with_no_row_tears_room_down_but_is_not_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call.failed always tears the room down regardless of the Call row's state,
    but the closeout retry logic still applies to the DB side via _close_and_refill:
    a young event with no row leaves the entry unacked for redelivery."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    event = CallFailedEvent(room_name=_VALID_ROOM, reason=CallFailureReason.NO_ANSWER, ts=_now_ms())
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert livekit.calls == [
        ("meta", (_VALID_ROOM, {"status": "call_failed", "reason": "no_answer"})),
        ("delete", _VALID_ROOM),
    ]
    assert redis.acked == []  # DB-side closeout retried; room teardown already ran
    assert wired.session.added == []
    assert wired.dispatch_calls == []


# ---------------------------------------------------------------------------
# call.answer_recorded — persist an Observer-extracted ai_call answer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_recorded_writes_recomputes_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id)
    form = _form_row(tenant_id, form_id)
    version = SchemaVersion(id=form.schema_version_id, schema_json={"dsl_version": "2.1"})
    session = _FakeSession(call=call, form=form, schema_version=version)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    seen: dict[str, Any] = {}

    async def _fake_record_answer(sess: Any, **kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    recomputed: list[Any] = []

    async def _fake_recompute(sess: Any, f: Any, schema_json: Any) -> None:
        recomputed.append(f)

    async def _fake_baseline(sess: Any, fid: Any, path: str) -> Any:
        return {"value": "No"}  # intake/human baseline the AI value diverges from

    monkeypatch.setattr(worker_events, "record_answer", _fake_record_answer)
    monkeypatch.setattr(worker_events, "recompute_form_projection", _fake_recompute)
    monkeypatch.setattr(worker_events, "baseline_value", _fake_baseline)

    event = CallAnswerRecordedEvent(
        room_name=room, field_path="sections.a.x", value="Yes", confidence=90, evidence_seq=3, ts=1
    )
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert seen["field_path"] == "sections.a.x"
    assert seen["raw_value"] == "Yes"
    assert seen["source"] == "ai_call"
    assert recomputed == [form]
    ai_audits = [r for r in wired.audit.records if r.event_type == AuditEvent.FORM_AI_ANSWER.value]
    assert len(ai_audits) == 1
    assert ai_audits[0].detail == {"field_path": "sections.a.x", "call_id": str(call_id)}
    assert "Yes" not in str(ai_audits[0].detail)  # never the value
    assert redis.acked == ["1-0"]
    # Relayed onto the per-call SSE stream so Live Monitoring updates without a poll,
    # carrying the dispute (AI "Yes" diverges from the "No" baseline).
    assert wired.call_stream.field_answers == [
        {
            "room_name": room,
            "field_path": "sections.a.x",
            "value": "Yes",
            "confidence": 90,
            "evidence_seq": 3,
            "completion_pct": form.completion_pct,
            "dispute": {
                "previous_value": "No",
                "current_value": "Yes",
                "confidence": 90,
                "evidence": None,
                "reasoning": None,
            },
            "ts": 1,
        }
    ]


def _wire_answer_relay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: Any = None,
    call_status: str = CallStatus.ACTIVE.value,
) -> tuple[_Wired, str, _FakeRedis, _FakeSession]:
    """A consumer wired for the answer-relay tests: a live Call + PatientForm + schema
    version, with the write/recompute/baseline collaborators stubbed so only the relay
    behavior is under test. `baseline` is what `baseline_value` resolves to."""
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    form = _form_row(tenant_id, form_id)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id, current_status=call_status),
        form=form,
        schema_version=SchemaVersion(id=form.schema_version_id, schema_json={"dsl_version": "2.1"}),
    )
    redis = _FakeRedis()
    wired = _consumer(monkeypatch, redis, _FakeLiveKit(), session=session)

    async def _wrote(sess: Any, **kwargs: Any) -> bool:
        return True

    async def _recompute(sess: Any, f: Any, schema_json: Any) -> None:
        return None

    async def _baseline(sess: Any, fid: Any, path: str) -> Any:
        return baseline

    monkeypatch.setattr(worker_events, "record_answer", _wrote)
    monkeypatch.setattr(worker_events, "recompute_form_projection", _recompute)
    monkeypatch.setattr(worker_events, "baseline_value", _baseline)
    return wired, room_name_for_call(tenant_id, call_id), redis, session


async def _process_answer(wired: _Wired, room: str) -> None:
    event = CallAnswerRecordedEvent(room_name=room, field_path="sections.a.x", value="Yes", ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})


@pytest.mark.asyncio
async def test_answer_recorded_publishes_null_dispute_when_matching_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AI answer that matches the intake/human baseline is not disputed — the SSE frame
    carries dispute=None so a live surface clears any earlier dispute for the field."""
    wired, room, _, _ = _wire_answer_relay(monkeypatch, baseline={"value": "Yes"})

    await _process_answer(wired, room)

    assert wired.call_stream.field_answers[0]["dispute"] is None


@pytest.mark.asyncio
async def test_answer_recorded_skips_publish_for_terminal_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redelivery reclaimed after closeout still persists, but must NOT publish: the
    stream was deleted by finalize_transcript, and an XADD would recreate it — flipping
    stream_call_events off its DB-replay branch onto an endless tail of a dead call."""
    wired, room, redis, _ = _wire_answer_relay(monkeypatch, call_status=CallStatus.COMPLETED.value)

    await _process_answer(wired, room)

    assert wired.call_stream.field_answers == []
    assert wired.audit.records != []  # still persisted + audited — only the relay is dropped
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_answer_recorded_does_not_publish_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay must not run until the transaction commits, or a failed commit leaves the
    supervisor looking at a value and completion_pct that were never persisted."""
    wired, room, redis, session = _wire_answer_relay(monkeypatch)

    class _FailingCommitCtx(_FakeSessionCtx):
        async def __aexit__(self, *exc: object) -> bool:
            raise RuntimeError("commit failed")

    monkeypatch.setattr(worker_events, "tenant_session", lambda sm, tid: _FailingCommitCtx(session))

    await _process_answer(wired, room)

    assert wired.call_stream.field_answers == []
    assert redis.acked == []  # _process leaves the failed entry unacked for reclaim


@pytest.mark.asyncio
async def test_answer_recorded_noop_skips_recompute_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id), form=_form_row(tenant_id, form_id)
    )
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    async def _noop_write(sess: Any, **kwargs: Any) -> bool:
        return False  # idempotent redelivery — value already current

    recomputed: list[Any] = []
    monkeypatch.setattr(worker_events, "record_answer", _noop_write)
    monkeypatch.setattr(
        worker_events,
        "recompute_form_projection",
        lambda *a, **k: recomputed.append(a),
    )

    event = CallAnswerRecordedEvent(room_name=room, field_path="sections.a.x", value="Yes", ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert recomputed == []
    assert wired.audit.records == []
    assert wired.call_stream.field_answers == []  # no SSE relay on an idempotent no-op
    assert redis.acked == ["1-0"]  # acked (successfully processed as a no-op)


@pytest.mark.asyncio
async def test_answer_recorded_young_without_call_row_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)  # default session: call=None

    event = CallAnswerRecordedEvent(
        room_name=_VALID_ROOM, field_path="sections.a.x", value="Yes", ts=_now_ms()
    )
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert redis.acked == []  # young + no row → left unacked for redelivery
    assert wired.audit.records == []


@pytest.mark.asyncio
async def test_dispatch_serializes_within_a_room_but_parallel_across(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two answers for the same room must process in stream order; different rooms
    are independent. Grouping is by the payload's room_name."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)

    order: list[str] = []

    async def _spy_process(entry_id: str, fields: dict[str, str]) -> None:
        order.append(entry_id)

    monkeypatch.setattr(wired.consumer, "_process", _spy_process)

    room_a, room_b = _VALID_ROOM, f"call--{uuid4()}--{uuid4()}"

    def _fields(room: str) -> dict[str, str]:
        ev = CallAnswerRecordedEvent(room_name=room, field_path="a", value="v", ts=1)
        return {"event": ev.model_dump_json()}

    entries = [("a1", _fields(room_a)), ("b1", _fields(room_b)), ("a2", _fields(room_a))]
    await wired.consumer._dispatch(entries)

    # within room A, a1 precedes a2 (stream order preserved)
    assert order.index("a1") < order.index("a2")
    assert set(order) == {"a1", "a2", "b1"}


def _pending(message_id: str, times_delivered: int = 1) -> dict[str, object]:
    return {
        "message_id": message_id,
        "consumer": "c",
        "time_since_delivered": 0,
        "times_delivered": times_delivered,
    }


def _appender(sink: list[str], label: str) -> Callable[[object], Awaitable[None]]:
    async def _handler(event: object) -> None:
        sink.append(label)

    return _handler


@pytest.mark.asyncio
async def test_close_defers_while_earlier_same_room_entry_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The staggered race: an earlier same-room answer failed and is pending in a PRIOR
    read window; call.ended (read in its own later window) must NOT resolve ahead of it —
    else post-call resolution runs on a stale projection and re-dials a filled form."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    room = _VALID_ROOM
    answer = CallAnswerRecordedEvent(room_name=room, field_path="a", value="v", ts=1)
    redis.entries["1-0"] = {"event": answer.model_dump_json()}
    redis.pending = [_pending("1-0"), _pending("2-0")]

    ran: list[str] = []
    wired.consumer._handlers["call.ended"] = _appender(ran, "ended")
    ended = CallEndedEvent(room_name=room, ts=2)
    await wired.consumer._process("2-0", {"event": ended.model_dump_json()})

    assert ran == []  # deferred — handler never ran
    assert redis.acked == []  # left unacked → reclaimed after the answer


@pytest.mark.asyncio
async def test_close_proceeds_when_no_earlier_same_room_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    redis.pending = [_pending("2-0")]  # only the close event itself is pending

    ran: list[str] = []
    wired.consumer._handlers["call.ended"] = _appender(ran, "ended")
    ended = CallEndedEvent(room_name=_VALID_ROOM, ts=2)
    await wired.consumer._process("2-0", {"event": ended.model_dump_json()})

    assert ran == ["ended"]
    assert redis.acked == ["2-0"]


@pytest.mark.asyncio
async def test_close_ignores_pending_entries_from_other_rooms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    other = CallAnswerRecordedEvent(
        room_name=f"call--{uuid4()}--{uuid4()}", field_path="a", value="v", ts=1
    )
    redis.entries["1-0"] = {"event": other.model_dump_json()}
    redis.pending = [_pending("1-0"), _pending("2-0")]

    ran: list[str] = []
    wired.consumer._handlers["call.ended"] = _appender(ran, "ended")
    ended = CallEndedEvent(room_name=_VALID_ROOM, ts=2)
    await wired.consumer._process("2-0", {"event": ended.model_dump_json()})

    assert ran == ["ended"]  # a different room's pending entry does not block this close
    assert redis.acked == ["2-0"]


@pytest.mark.asyncio
async def test_close_fails_open_when_its_own_entry_is_not_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: if the close entry is not in the group's pending set (so its
    delivery count can't be read and the cap could never trip), proceed rather than defer
    a phantom forever — even though an earlier same-room entry is pending."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    room = _VALID_ROOM
    answer = CallAnswerRecordedEvent(room_name=room, field_path="a", value="v", ts=1)
    redis.entries["1-0"] = {"event": answer.model_dump_json()}
    redis.pending = [_pending("1-0")]  # predecessor pending; the close (2-0) is NOT

    ran: list[str] = []
    wired.consumer._handlers["call.ended"] = _appender(ran, "ended")
    ended = CallEndedEvent(room_name=room, ts=2)
    await wired.consumer._process("2-0", {"event": ended.model_dump_json()})

    assert ran == ["ended"]  # fails open — proceeds instead of deferring indefinitely
    assert redis.acked == ["2-0"]


@pytest.mark.asyncio
async def test_close_proceeds_after_max_deliveries_despite_pending_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poison predecessor must not wedge the room forever: once the close event has been
    redelivered past the cap, it proceeds (logged) rather than deferring indefinitely."""
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit)
    room = _VALID_ROOM
    answer = CallAnswerRecordedEvent(room_name=room, field_path="a", value="v", ts=1)
    redis.entries["1-0"] = {"event": answer.model_dump_json()}
    # predecessor still pending, and the close event has been redelivered 4x (> cap 3).
    redis.pending = [_pending("1-0", times_delivered=9), _pending("2-0", times_delivered=4)]

    ran: list[str] = []
    wired.consumer._handlers["call.ended"] = _appender(ran, "ended")
    ended = CallEndedEvent(room_name=room, ts=2)
    await wired.consumer._process("2-0", {"event": ended.model_dump_json()})

    assert ran == ["ended"]  # bounded wait — proceeds despite the pending predecessor
    assert redis.acked == ["2-0"]


# ---------------------------------------------------------------------------
# Task 8: CRITICAL-interplay — the health observer's status flip must survive
# a redelivered call.answered, and a CRITICAL call must still close normally.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answered_redelivery_does_not_clobber_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.CRITICAL.value)
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=_FakeSession(call=call))
    ev = CallAnsweredEvent(
        room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000)
    )
    await wired.consumer._handle_call_answered(ev)
    assert call.current_status == CallStatus.CRITICAL.value  # health flip survives
    assert wired.session.added == []


@pytest.mark.asyncio
async def test_call_ended_closes_a_critical_call(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.CRITICAL.value)
    form = _form_row(tenant_id, form_id)
    session = _FakeSession(call=call, form=form, tenant=_tenant(id=tenant_id))
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=session)
    ev = CallEndedEvent(
        room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000)
    )
    await wired.consumer._handle_call_ended(ev)
    assert call.current_status in TERMINAL_VALUES  # CRITICAL closes like any active status


# ---------------------------------------------------------------------------
# Final-review fix: the answered-after-health race. `_handle_call_health`'s
# escalation branch can flip INITIATED/RINGING -> CRITICAL before call.answered
# is processed, so the early-return branch above must still backfill
# started_at — otherwise it stays NULL and `end_call`'s `started_at is None`
# pre-answer routing misclassifies a live call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answered_backfills_started_at_on_critical_early_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    call = _call_row(
        tenant_id, call_id, form_id, current_status=CallStatus.CRITICAL.value, started_at=None
    )
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=_FakeSession(call=call))
    ev = CallAnsweredEvent(
        room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000)
    )
    await wired.consumer._handle_call_answered(ev)
    assert call.current_status == CallStatus.CRITICAL.value  # unchanged
    assert call.started_at is not None  # backfilled
    assert wired.session.added == []  # idempotent: no new STATUS CallEvent on redelivery


@pytest.mark.asyncio
async def test_answered_redelivery_keeps_existing_started_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    original = datetime.fromtimestamp(1_700_000_000, tz=UTC)
    call = _call_row(
        tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value, started_at=original
    )
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=_FakeSession(call=call))
    ev = CallAnsweredEvent(
        room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000)
    )
    await wired.consumer._handle_call_answered(ev)
    assert call.started_at == original  # already-ACTIVE call's started_at is untouched
    assert wired.session.added == []
