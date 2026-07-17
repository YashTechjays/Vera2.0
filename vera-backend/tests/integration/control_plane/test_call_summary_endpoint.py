"""Integration tests for GET /calls/{call_id}/summary — live RLS Postgres,
in-memory call stream + injected stub summarizer (conftest authz_app; the
app.state seams are overridden per-test)."""

from collections.abc import Generator
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import RBACWorld, seed_call
from tests.integration.control_plane.test_calls import _auth, seeded_form_id  # noqa: F401
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.llm import LLMUnavailableError
from vera_core.models import AuditLog, Transcript
from vera_core.observability.correlation import room_name_for_call


class _StubSummaryLLM:
    def __init__(self, text: str = "handoff summary", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0

    async def complete(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self.fail:
            raise LLMUnavailableError
        return self.text


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, room_name: str) -> str | None:
        return self.data.get(room_name)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        self.data[room_name] = payload


@pytest.fixture
def stub_llm(authz_app: FastAPI) -> Generator[_StubSummaryLLM]:
    """Swap the app's summarizer seams for deterministic fakes, restore after."""
    llm, cache = _StubSummaryLLM(), _DictCache()
    prior_llm = authz_app.state.summary_llm
    prior_cache = authz_app.state.summary_cache
    authz_app.state.summary_llm = llm
    authz_app.state.summary_cache = cache
    yield llm
    authz_app.state.summary_llm = prior_llm
    authz_app.state.summary_cache = prior_cache


async def _publish_turns(call_stream_service: CallStreamService, room_name: str) -> None:
    await call_stream_service.publish_turn(room_name, "agent", "Hello, verifying benefits.", ts=1)
    await call_stream_service.publish_turn(room_name, "user", "Sure, member ID please.", ts=2)


@pytest.mark.asyncio
async def test_summary_ready_cached_audited_no_store(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    await _publish_turns(call_stream_service, room)

    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()["data"]
    assert body["status"] == "ready"
    assert body["summary"] == "handoff summary"
    assert body["turn_count"] == 2

    # Second hit within the cache TTL: served from cache, no second LLM call.
    resp2 = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
    )
    assert resp2.status_code == 200
    assert stub_llm.calls == 1

    # PHI disclosure audited with the call_summary resource type.
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.resource_type == "call_summary",
                        AuditLog.resource_id == str(call_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 2
    assert all(r.decision == "allow" for r in rows)


@pytest.mark.asyncio
async def test_summary_pending_for_quiet_call(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"
    assert stub_llm.calls == 0


@pytest.mark.asyncio
async def test_summary_terminal_call_uses_db_transcript(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="completed",
    )
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        session.add(
            Transcript(
                tenant_id=rbac_world.tenant_id,
                call_id=call_id,
                seq=1,
                source="bot",
                role="agent",
                message="Hello, verifying benefits.",
            )
        )
        session.add(
            Transcript(
                tenant_id=rbac_world.tenant_id,
                call_id=call_id,
                seq=2,
                source="rep",
                role="user",
                message="Sure, member ID please.",
            )
        )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ready"
    assert stub_llm.calls == 1


@pytest.mark.asyncio
async def test_summary_authz_denied_and_hidden(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,  # private to admin
        status="active",
    )
    # No calls:read on an ownerless/published call -> 403; here the call is
    # PRIVATE to admin, so a non-owner (even with calls:read) gets 404.
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.listener_token)
    )
    assert resp.status_code == 404
    # norole caller on an unknown call id -> 404 as well.
    resp = await client.get(
        "/api/v1/calls/00000000-0000-0000-0000-000000000000/summary",
        headers=_auth(rbac_world.norole_token),
    )
    assert resp.status_code == 404
    assert stub_llm.calls == 0


@pytest.mark.asyncio
async def test_summary_403_when_visible_but_unpermitted(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id, published=True, status="active"
    )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403

    # PHI-access audit recorded the denial too (same resource, decision="deny").
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.resource_type == "call_summary",
                        AuditLog.resource_id == str(call_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any(r.decision == "deny" for r in rows)


@pytest.mark.asyncio
async def test_summary_llm_unavailable_returns_503(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # noqa: F811
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    authz_app: FastAPI,
) -> None:
    llm, cache = _StubSummaryLLM(fail=True), _DictCache()
    prior_llm, prior_cache = authz_app.state.summary_llm, authz_app.state.summary_cache
    authz_app.state.summary_llm, authz_app.state.summary_cache = llm, cache
    try:
        call_id = await seed_call(
            admin_sessionmaker,
            rbac_world.tenant_id,
            seeded_form_id,
            initiated_by_id=rbac_world.admin_id,
            status="active",
        )
        room = room_name_for_call(rbac_world.tenant_id, call_id)
        await _publish_turns(call_stream_service, room)
        resp = await client.get(
            f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
        )
        assert resp.status_code == 503
        assert resp.json()["error_code"] == "SERVICE_UNAVAILABLE"
    finally:
        authz_app.state.summary_llm, authz_app.state.summary_cache = prior_llm, prior_cache
