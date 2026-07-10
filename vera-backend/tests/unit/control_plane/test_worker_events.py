"""Unit tests for the control-plane worker-event consumer (no live Redis, no live DB).

The DB seam (`tenant_session`, imported into both `worker_events` and
`call_closeout`) and the post-closeout `run_dispatch_pass` refill are
monkeypatched per-test via `_consumer()`, which routes queries through a
`_FakeSession` keyed by target entity — mirroring `FakeSession` in
`tests/unit/services/test_queue_dispatcher.py`.
"""

import time
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import control_plane.call_closeout as call_closeout
import control_plane.worker_events as worker_events
from control_plane.livekit_gateway import LiveKitGateway
from control_plane.worker_events import WorkerEventConsumer
from vera_core.audit import AuditRecord
from vera_core.events import (
    CallAnsweredEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
)
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
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
        # Configured per-test to mimic redis-py's decoded XREADGROUP/XAUTOCLAIM shapes.
        self.xreadgroup_response: object = None
        self.xautoclaim_response: object = ("0-0", [], [])
        # When set, xreadgroup raises it — used to mimic redis-py turning a BLOCK
        # window with no new entries into a raised TimeoutError.
        self.xreadgroup_error: Exception | None = None

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        return 1

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
    Tenant). Defaults to "no Call row" — matches a voice-lab room whose
    synthetic call id never made it into the table."""

    def __init__(self, *, call: Any = None, form: Any = None, tenant: Any = None) -> None:
        self.call = call
        self.form = form
        self.tenant = tenant
        self.added: list[Any] = []
        self.queried: list[type] = []

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        self.queried.append(entity)
        if entity is Call:
            return _Result(self.call)
        if entity is PatientForm:
            return _Result(self.form)
        if entity is Tenant:
            return _Result(self.tenant)
        raise AssertionError(f"unexpected query entity {entity}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)


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
    dispatch_calls: list[tuple[Any, ...]] = field(default_factory=list)


def _consumer(
    monkeypatch: pytest.MonkeyPatch,
    redis: _FakeRedis,
    livekit: _FakeLiveKit,
    *,
    session: _FakeSession | None = None,
) -> _Wired:
    """Wire a consumer to a fake DB seam and a fake dispatch pass. `tenant_session`
    is monkeypatched in both `worker_events` (the answered handler) and
    `call_closeout` (the shared terminal closeout) to the SAME fake session, so a
    test can seed one Call/PatientForm/Tenant set and see it through both paths.
    """
    fake_session = session if session is not None else _FakeSession()
    fake_audit = _SpyAudit()
    dispatch_calls: list[tuple[Any, ...]] = []

    def _fake_tenant_session(sm: Any, tid: Any) -> _FakeSessionCtx:
        return _FakeSessionCtx(fake_session)

    monkeypatch.setattr(call_closeout, "tenant_session", _fake_tenant_session)
    monkeypatch.setattr(worker_events, "tenant_session", _fake_tenant_session)

    async def _fake_run_dispatch_pass(
        sessionmaker: Any, tenant_id: Any, lk: Any, kms: Any, aud: Any
    ) -> None:
        dispatch_calls.append((tenant_id, lk, kms, aud))

    monkeypatch.setattr(worker_events, "run_dispatch_pass", _fake_run_dispatch_pass)

    consumer = WorkerEventConsumer(
        cast(Redis, redis),
        cast(LiveKitGateway, livekit),
        cast("async_sessionmaker[AsyncSession]", object()),
        object(),
        fake_audit,
        teardown_grace_ms=0,
    )
    return _Wired(
        consumer=consumer, session=fake_session, audit=fake_audit, dispatch_calls=dispatch_calls
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
        "queue_expiry_hours": 48,
        "persona_tweak": {},
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
    assert redis.acked == ["1-0"]


@pytest.mark.asyncio
async def test_call_ended_completes_call_and_form_then_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.ACTIVE.value)
    form = _form_row(tenant_id, form_id, status=FormStatus.IN_CALL.value)
    tenant = _tenant(id=tenant_id, max_retries=3)
    session = _FakeSession(call=call, form=form, tenant=tenant)
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

    event = CallEndedEvent(room_name=room, ts=1)
    await wired.consumer._process("1-0", {"event": event.model_dump_json()})

    assert call.current_status == CallStatus.COMPLETED.value
    assert call.ended_at is not None
    assert form.status == FormStatus.COMPLETED.value
    status_events = [e for e in session.added if e.event_type == CallEventType.STATUS.value]
    assert len(status_events) == 1
    assert status_events[0].event_value == CallStatus.COMPLETED.value

    assert len(wired.audit.records) == 1
    record = wired.audit.records[0]
    assert record.event_type == AuditEvent.FORM_STATUS_CHANGE.value
    assert record.detail["from"] == FormStatus.IN_CALL.value
    assert record.detail["to"] == FormStatus.COMPLETED.value

    assert len(wired.dispatch_calls) == 1
    assert wired.dispatch_calls[0][0] == tenant_id  # refill ran for the freed tenant
    assert redis.acked == ["1-0"]


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
    redis, livekit = _FakeRedis(), _FakeLiveKit()
    wired = _consumer(monkeypatch, redis, livekit, session=session)

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
