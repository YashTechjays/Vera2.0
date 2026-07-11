"""Pipeline sweeper — which stuck calls get closed, and when dispatch re-runs."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import control_plane.pipeline_sweeper as sweeper_mod
from control_plane.pipeline_sweeper import PipelineSweeper, rooms_to_close
from vera_core.audit import AuditSink
from vera_core.call_stream import CallStreamService
from vera_core.models.enums import CallStatus
from vera_core.observability.correlation import room_name_for_call


def test_room_gone_first_sighting_is_deferred_not_closed() -> None:
    """Room-gone is also the healthy closeout's transient state (the worker
    deletes the room moments before close_call commits) — a first sighting must
    wait one tick instead of failing a normally completed call (→ auto-redial)."""
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, newly_gone = rooms_to_close(
        [(call, False, False)],
        live_rooms=set(),
        observer_only_rooms=set(),
        tenant_id=tenant,
        confirmed_gone=set(),
    )
    assert result == []
    assert newly_gone == {room}


def test_room_gone_two_consecutive_ticks_is_closed_without_room_delete() -> None:
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, newly_gone = rooms_to_close(
        [(call, False, False)],
        live_rooms=set(),
        observer_only_rooms=set(),
        tenant_id=tenant,
        confirmed_gone={room},
    )
    assert result == [(room, False, CallStatus.FAILED)]  # close it; no room left to delete
    assert newly_gone == set()


def test_live_room_within_cap_is_left_alone() -> None:
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, newly_gone = rooms_to_close(
        [(call, False, False)],
        live_rooms={room},
        observer_only_rooms=set(),
        tenant_id=tenant,
        confirmed_gone=set(),
    )
    assert result == []
    assert newly_gone == set()


def test_live_room_past_cap_is_deleted_then_closed() -> None:
    """A wedged session past the hard cap needs no two-tick patience — the room
    is LIVE, so this can't be the healthy closeout's delete-then-commit window."""
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, _ = rooms_to_close(
        [(call, True, False)],
        live_rooms={room},
        observer_only_rooms=set(),
        tenant_id=tenant,
        confirmed_gone=set(),
    )
    assert result == [(room, True, CallStatus.FAILED)]  # wedged session: delete, then close


def test_end_requested_closes_as_canceled() -> None:
    """A user asked to end this call (End Call in Live Monitoring) but the
    worker's event never landed — the sweeper must honor the intent: CANCELED,
    never FAILED (FAILED would auto-redial the number)."""
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, _ = rooms_to_close(
        [(call, False, True)],
        live_rooms=set(),
        observer_only_rooms=set(),
        tenant_id=tenant,
        confirmed_gone={room},
    )
    assert result == [(room, False, CallStatus.CANCELED)]


def test_observer_only_live_room_is_reaped_with_delete() -> None:
    """A room held open only by browser observers (no agent, no SIP callee) can
    never progress — the observers keep LiveKit's departure timeout from firing,
    so the sweeper deletes the room and closes the call."""
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result, _ = rooms_to_close(
        [(call, False, False)],
        live_rooms={room},
        observer_only_rooms={room},
        tenant_id=tenant,
        confirmed_gone=set(),
    )
    assert result == [(room, True, CallStatus.FAILED)]


@pytest.mark.asyncio
async def test_sweep_once_continues_past_a_failing_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tenant's sweep error must not starve the others (loop isolation)."""
    tenants = [uuid4(), uuid4()]
    swept: list[UUID] = []

    class _Values:
        def all(self) -> list[UUID]:
            return tenants

    class _Result:
        def scalars(self) -> "_Values":
            return _Values()

    class _Session:
        async def execute(self, *_a: Any) -> "_Result":
            return _Result()

    class _PlatformCtx:
        async def __aenter__(self) -> "_Session":
            return _Session()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(sweeper_mod, "platform_session", lambda sm: _PlatformCtx())
    sweeper = PipelineSweeper(
        cast("async_sessionmaker[AsyncSession]", object()),
        object(),
        object(),
        cast(AuditSink, object()),
        cast(CallStreamService, object()),
        interval_s=60,
        stuck_grace_s=300,
        max_call_duration_s=10_800,
    )

    async def fake_sweep_tenant(tenant_id: UUID) -> None:
        swept.append(tenant_id)
        if tenant_id == tenants[0]:
            raise RuntimeError("boom")

    monkeypatch.setattr(sweeper, "_sweep_tenant", fake_sweep_tenant)
    await sweeper.sweep_once()  # must not raise
    assert swept == tenants
