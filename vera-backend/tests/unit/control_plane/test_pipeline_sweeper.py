"""Pipeline sweeper — which stuck calls get closed, and when dispatch re-runs."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import control_plane.pipeline_sweeper as sweeper_mod
from control_plane.pipeline_sweeper import PipelineSweeper, rooms_to_close
from vera_core.audit import AuditSink
from vera_core.observability.correlation import room_name_for_call


def test_room_gone_is_closed_without_room_delete() -> None:
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result = rooms_to_close([(call, False)], live_rooms=set(), tenant_id=tenant)
    assert result == [(room, False)]  # close it; no room left to delete


def test_live_room_within_cap_is_left_alone() -> None:
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    assert rooms_to_close([(call, False)], live_rooms={room}, tenant_id=tenant) == []


def test_live_room_past_cap_is_deleted_then_closed() -> None:
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result = rooms_to_close([(call, True)], live_rooms={room}, tenant_id=tenant)
    assert result == [(room, True)]  # wedged session: delete the room, then close


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
