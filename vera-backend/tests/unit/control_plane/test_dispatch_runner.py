"""run_dispatch_pass — a self-contained, exception-safe dispatch pass."""

from typing import Any
from uuid import uuid4

import pytest

import control_plane.dispatch as dispatch_mod
from control_plane.dispatch import run_dispatch_pass


class _FakeSessionCtx:
    def __init__(self) -> None:
        self.session = object()

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
        session: Any, tenant_id: Any, livekit: Any, kms: Any, *, audit: Any = None
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
        session: Any, tenant_id: Any, livekit: Any, kms: Any, *, audit: Any = None
    ) -> None:
        raise RuntimeError("livekit down")

    monkeypatch.setattr(dispatch_mod, "try_dispatch", boom)
    await run_dispatch_pass(object(), uuid4(), object(), object(), None)  # type: ignore  # must not raise
    assert any("dispatch pass failed" in r.message for r in caplog.records)
