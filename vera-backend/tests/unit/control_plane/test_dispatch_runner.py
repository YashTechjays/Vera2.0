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
async def test_runs_the_pass_against_the_sessionmaker(monkeypatch: Any) -> None:
    """The pass owns its own transactions now (stage-commit-then-dial), so the
    runner hands it the sessionmaker rather than an open session."""
    seen: dict[str, object] = {}

    async def fake_pass(sessionmaker: Any, tenant_id: Any, *_a: Any, **_kw: Any) -> int:
        seen.update(sessionmaker=sessionmaker, tenant_id=tenant_id)
        return 1

    monkeypatch.setattr(dispatch_mod, "stage_and_dial", fake_pass)
    sm, tid = object(), uuid4()
    await run_dispatch_pass(sm, tid, object(), object(), None)  # type: ignore
    assert seen == {"sessionmaker": sm, "tenant_id": tid}


@pytest.mark.asyncio
async def test_forwards_plan_service_to_the_pass(monkeypatch: Any) -> None:
    seen: dict[str, object] = {}

    async def fake_pass(*_a: Any, plan_service: Any = None, **_kw: Any) -> int:
        seen["plan_service"] = plan_service
        return 0

    monkeypatch.setattr(dispatch_mod, "stage_and_dial", fake_pass)
    plans = object()
    await run_dispatch_pass(object(), uuid4(), object(), object(), None, plan_service=plans)  # type: ignore
    assert seen["plan_service"] is plans


@pytest.mark.asyncio
async def test_swallows_and_logs_dispatch_errors(monkeypatch: Any, caplog: Any) -> None:
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("livekit down")

    monkeypatch.setattr(dispatch_mod, "stage_and_dial", boom)
    await run_dispatch_pass(object(), uuid4(), object(), object(), None)  # type: ignore  # must not raise
    assert any("dispatch pass failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_schedule_dispatch_pass_runs_detached(monkeypatch: Any) -> None:
    ran: list[object] = []

    async def fake_pass(_sm: Any, tenant_id: Any, *_a: Any, **_kw: Any) -> None:
        ran.append(tenant_id)

    monkeypatch.setattr(dispatch_mod, "stage_and_dial", fake_pass)
    tid = uuid4()
    schedule_dispatch_pass(object(), tid, object(), object(), None)  # type: ignore
    await drain_pending()
    assert ran == [tid]


@pytest.mark.asyncio
async def test_pass_survives_caller_cancellation(monkeypatch: Any) -> None:
    """Cancelling the awaiting caller (consumer/sweeper teardown on a deploy)
    must NOT cancel the pass itself: a pass killed between its staging commit and
    its dials leaves committed INITIATED calls nobody ever rings, and one killed
    mid-dial abandons a live SIP leg before its bookkeeping ran. The pass runs
    detached in the shutdown-drained _PENDING set."""
    entered = asyncio.Event()
    release = asyncio.Event()
    completed: list[object] = []

    async def slow_pass(_sm: Any, tenant_id: Any, *_a: Any, **_kw: Any) -> None:
        entered.set()
        await release.wait()
        completed.append(tenant_id)

    monkeypatch.setattr(dispatch_mod, "stage_and_dial", slow_pass)
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
    """The row-lock barrier takes its own session, opened AND closed before the
    pass starts — so the pass never inherits the barrier's transaction."""
    order: list[str] = []

    def fake_tenant_session(sm: Any, tid: Any) -> _FakeSessionCtx:
        order.append("barrier")
        return _FakeSessionCtx()

    async def fake_pass(*args: Any, **kwargs: Any) -> None:
        order.append("pass")

    monkeypatch.setattr(dispatch_mod, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(dispatch_mod, "stage_and_dial", fake_pass)
    await run_dispatch_pass(
        object(),  # type: ignore
        uuid4(),
        object(),
        object(),
        None,
        wait_for_form_id=uuid4(),
    )
    assert order == ["barrier", "pass"]
