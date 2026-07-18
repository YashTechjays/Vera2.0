"""_handle_call_health: guards, column updates, the episode state machine with
asymmetric hysteresis, and transition-only notifications (spec §4.3, §5)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

import control_plane.worker_events as worker_events
from control_plane.livekit_gateway import LiveKitGateway
from control_plane.worker_events import WorkerEventConsumer
from vera_core.events import CallHealthEvent
from vera_core.models import Call, CallEvent
from vera_core.models.enums import CallEventType, CallStatus
from vera_core.notifications import Notification
from vera_core.observability.correlation import room_name_for_call

_TENANT = uuid4()
_CALL = uuid4()
_ROOM = room_name_for_call(_TENANT, _CALL)


class _Result:
    def __init__(self, scalar: Any) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _FakeSession:
    """Routes execute() by target entity: the Call row lock, and the episode-flag
    lookup (select(CallEvent.event_value) ... limit(1))."""

    def __init__(self, *, call: Any = None, episode_flag: str | None = None) -> None:
        self.call = call
        self.episode_flag = episode_flag
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        if isinstance(stmt, Insert):  # pragma: no cover — handler uses session.add
            raise AssertionError("unexpected insert")
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Call:
            return _Result(self.call)
        if entity is CallEvent:
            return _Result(self.episode_flag)
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


class _SpyNotifications:
    def __init__(self) -> None:
        self.published: list[tuple[UUID, Notification]] = []

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        self.published.append((tenant_id, notification))


@dataclass
class _Wired:
    consumer: WorkerEventConsumer
    session: _FakeSession
    notifications: _SpyNotifications


def _wire(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> _Wired:
    notifications = _SpyNotifications()
    monkeypatch.setattr(worker_events, "tenant_session", lambda sm, tid: _FakeSessionCtx(session))
    consumer = WorkerEventConsumer(
        cast(Redis, object()),
        cast(LiveKitGateway, object()),
        cast("async_sessionmaker[AsyncSession]", object()),
        object(),
        cast(Any, object()),
        cast(Any, object()),
        notifications=cast(Any, notifications),
    )
    return _Wired(consumer=consumer, session=session, notifications=notifications)


def _call_row(**overrides: Any) -> Call:
    defaults: dict[str, Any] = {
        "id": _CALL,
        "tenant_id": _TENANT,
        "form_id": uuid4(),
        "current_status": CallStatus.ACTIVE.value,
        "published": False,
        "initiated_by_id": uuid4(),
        "intervener_user_id": None,
        "health_score": None,
        "health_flag": None,
        "health_analyzed_at": None,
    }
    defaults.update(overrides)
    return Call(**defaults)


def _event(*, score: int = 40, flag: str = "conversation_loop", ts: int = 2_000) -> CallHealthEvent:
    return CallHealthEvent(
        room_name=_ROOM, score=score, flag=flag, reason="looping", turn_count=8, ts=ts
    )


def _health_rows(session: _FakeSession) -> list[CallEvent]:
    return [
        e
        for e in session.added
        if isinstance(e, CallEvent) and e.event_type == CallEventType.HEALTH.value
    ]


def _status_rows(session: _FakeSession) -> list[CallEvent]:
    return [
        e
        for e in session.added
        if isinstance(e, CallEvent) and e.event_type == CallEventType.STATUS.value
    ]


@pytest.mark.asyncio
async def test_escalation_opens_episode_flips_critical_and_notifies_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    call = _call_row(initiated_by_id=owner)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    assert call.health_score == 40 and call.health_flag == "conversation_loop"
    assert call.current_status == CallStatus.CRITICAL.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["conversation_loop"]
    assert [r.event_value for r in _status_rows(wired.session)] == [CallStatus.CRITICAL.value]
    [(tenant_id, n)] = wired.notifications.published
    assert tenant_id == _TENANT
    assert n.audience.kind == "user" and n.audience.user_id == str(owner)  # unpublished -> owner
    assert n.data["call_id"] == str(_CALL) and n.data["flag"] == "conversation_loop"
    # Minimum-necessary (2026-07-18 final-review amendment): `reason` stays in
    # CallEvent.detail only — no consumer reads it off the notification wire.
    assert n.data.keys() == {"call_id", "score", "flag"}


@pytest.mark.asyncio
async def test_published_call_notifies_tenant_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _call_row(published=True)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    [(_tid, n)] = wired.notifications.published
    assert n.audience.kind == "tenant"


@pytest.mark.asyncio
async def test_ownerless_call_notifies_tenant_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _call_row(initiated_by_id=None, published=False)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    [(_tid, n)] = wired.notifications.published
    assert n.audience.kind == "tenant"  # ownerless -> tenant-visible in the list


@pytest.mark.asyncio
async def test_escalation_flips_from_non_active_non_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reorder race: call.health arrives before call.answered committed the ACTIVE
    flip, so current_status is still INITIATED when the episode opens. The flip
    guard must not require ACTIVE — any non-terminal, non-CRITICAL status is safe
    to promote straight to CRITICAL (spec amendment 2026-07-17)."""
    call = _call_row(current_status=CallStatus.INITIATED.value)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    assert call.current_status == CallStatus.CRITICAL.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["conversation_loop"]
    assert [r.event_value for r in _status_rows(wired.session)] == [CallStatus.CRITICAL.value]
    assert len(wired.notifications.published) == 1


@pytest.mark.asyncio
async def test_reconfirmation_updates_columns_only(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(score=35, ts=3_000))
    assert call.health_score == 35
    assert wired.session.added == [] and wired.notifications.published == []


@pytest.mark.asyncio
async def test_category_change_notifies_but_stays_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(flag="long_silence", ts=3_000))
    assert call.current_status == CallStatus.CRITICAL.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["long_silence"]
    assert _status_rows(wired.session) == []
    assert len(wired.notifications.published) == 1


@pytest.mark.asyncio
async def test_recovery_needs_two_consecutive_healthy_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    # First healthy result: columns update, still CRITICAL, no rows, no notify.
    await wired.consumer._handle_call_health(_event(score=85, flag="none", ts=3_000))
    assert call.current_status == CallStatus.CRITICAL.value
    assert call.health_flag == "none" and wired.session.added == []
    # Second consecutive healthy: close the episode, back to ACTIVE, no notify.
    await wired.consumer._handle_call_health(_event(score=90, flag="none", ts=4_000))
    assert call.current_status == CallStatus.ACTIVE.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["none"]
    assert [r.event_value for r in _status_rows(wired.session)] == [CallStatus.ACTIVE.value]
    assert wired.notifications.published == []


@pytest.mark.asyncio
async def test_healthy_blip_then_same_flag_does_not_renotify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="none",  # a single healthy blip inside the episode
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(flag="conversation_loop", ts=3_000))
    # Same episode category re-asserting itself after a blip: no new episode.
    assert wired.session.added == [] and wired.notifications.published == []
    assert call.health_flag == "conversation_loop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_kwargs",
    [
        {"current_status": CallStatus.COMPLETED.value},  # terminal — late result
        {"intervener_user_id": uuid4()},  # takeover raced the observer
        {"health_analyzed_at": datetime.fromtimestamp(10.0, tz=UTC)},  # stale/redelivered
    ],
)
async def test_guards_drop_the_event(
    monkeypatch: pytest.MonkeyPatch, call_kwargs: dict[str, Any]
) -> None:
    call = _call_row(**call_kwargs)
    before = (call.health_score, call.health_flag, call.current_status)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event(ts=2_000))
    assert (call.health_score, call.health_flag, call.current_status) == before
    assert wired.session.added == [] and wired.notifications.published == []


@pytest.mark.asyncio
async def test_no_call_row_drops_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    wired = _wire(monkeypatch, _FakeSession(call=None))
    await wired.consumer._handle_call_health(_event())  # must not raise _RetryEventLater
    assert wired.notifications.published == []
