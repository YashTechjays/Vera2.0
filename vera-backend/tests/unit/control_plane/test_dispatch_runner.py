"""run_dispatch_pass — a self-contained, exception-safe dispatch pass."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

import control_plane.dispatch as dispatch_mod
from control_plane.dispatch import drain_pending, run_dispatch_pass, schedule_dispatch_pass


class _FakeSession:
    async def execute(self, stmt: Any) -> None:
        return None


class _FakeSessionCtx:
    def __init__(self) -> None:
        self.session = _FakeSession()

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_runs_try_dispatch_in_its_own_tenant_session(monkeypatch: Any) -> None:
    seen: dict[str, object] = {}
    ctx = _FakeSessionCtx()
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: ctx)

    async def fake_try_dispatch(
        session: Any,
        tenant_id: Any,
        livekit: Any,
        kms: Any,
        *,
        audit: Any = None,
        recording: Any = None,
    ) -> int:
        seen.update(session=session, tenant_id=tenant_id)
        return 1

    monkeypatch.setattr(dispatch_mod, "try_dispatch", fake_try_dispatch)
    tid = uuid4()
    await run_dispatch_pass(object(), tid, object(), object(), None)  # type: ignore
    assert seen == {"session": ctx.session, "tenant_id": tid}


@pytest.mark.asyncio
async def test_swallows_and_logs_dispatch_errors(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: _FakeSessionCtx())

    async def boom(
        session: Any,
        tenant_id: Any,
        livekit: Any,
        kms: Any,
        *,
        audit: Any = None,
        recording: Any = None,
    ) -> None:
        raise RuntimeError("livekit down")

    monkeypatch.setattr(dispatch_mod, "try_dispatch", boom)
    await run_dispatch_pass(object(), uuid4(), object(), object(), None)  # type: ignore  # must not raise
    assert any("dispatch pass failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_schedule_dispatch_pass_runs_detached(monkeypatch: Any) -> None:
    ran: list[object] = []
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: _FakeSessionCtx())

    async def fake_try_dispatch(
        session: Any,
        tenant_id: Any,
        livekit: Any,
        kms: Any,
        *,
        audit: Any = None,
        recording: Any = None,
    ) -> None:
        ran.append(tenant_id)

    monkeypatch.setattr(dispatch_mod, "try_dispatch", fake_try_dispatch)
    tid = uuid4()
    schedule_dispatch_pass(object(), tid, object(), object(), None)  # type: ignore
    await drain_pending()
    assert ran == [tid]


@pytest.mark.asyncio
async def test_pass_survives_caller_cancellation(monkeypatch: Any) -> None:
    """Cancelling the awaiting caller (consumer/sweeper teardown on a deploy)
    must NOT cancel the pass itself: dials happen inside the pass's DB
    transaction, and a mid-pass rollback would strand live SIP calls with no
    Call row — the next pass would redial the same payer. The pass runs
    detached in the shutdown-drained _PENDING set."""
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: _FakeSessionCtx())
    entered = asyncio.Event()
    release = asyncio.Event()
    completed: list[object] = []

    async def slow_try_dispatch(
        session: Any,
        tenant_id: Any,
        livekit: Any,
        kms: Any,
        *,
        audit: Any = None,
        recording: Any = None,
    ) -> None:
        entered.set()
        await release.wait()
        completed.append(tenant_id)

    monkeypatch.setattr(dispatch_mod, "try_dispatch", slow_try_dispatch)
    tid = uuid4()
    caller = asyncio.create_task(run_dispatch_pass(object(), tid, object(), object(), None))  # type: ignore
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await drain_pending()  # the lifespan's shutdown barrier — the pass finishes here
    assert completed == [tid]


@pytest.mark.asyncio
async def test_wait_for_form_barrier_runs_before_pass(monkeypatch: Any) -> None:
    """The row-lock barrier runs in its own session, opened and closed BEFORE the
    session try_dispatch receives — two sessions total, dispatch gets the second."""
    opened: list[object] = []

    def fake_tenant_session(sm: Any, tid: Any) -> _FakeSessionCtx:
        ctx = _FakeSessionCtx()
        opened.append(ctx.session)
        return ctx

    monkeypatch.setattr(dispatch_mod, "tenant_session", fake_tenant_session)

    dispatched_with: list[object] = []

    async def fake_try_dispatch(
        session: Any,
        tenant_id: Any,
        livekit: Any,
        kms: Any,
        *,
        audit: Any = None,
        recording: Any = None,
    ) -> None:
        dispatched_with.append(session)

    monkeypatch.setattr(dispatch_mod, "try_dispatch", fake_try_dispatch)
    await run_dispatch_pass(
        object(),  # type: ignore
        uuid4(),
        object(),
        object(),
        None,
        wait_for_form_id=uuid4(),
    )
    assert len(opened) == 2
    assert dispatched_with == [opened[1]]
