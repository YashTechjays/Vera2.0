"""Unit tests for GET/PATCH /api/v1/tenant/config/concurrency.

Same no-DB harness as test_retention_policy.py: heavyweight deps are stubbed via
dependency_overrides and app.state injection, so only tenant_config.py is under test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from control_plane.api.v1.tenant_config import router
from control_plane.auth.identity import VerifiedIdentity
from control_plane.deps import (
    current_identity,
    current_tenant_id,
    tenant_scoped_session,
)
from control_plane.exceptions import register_exception_handlers
from control_plane.request_context import RequestIdMiddleware
from vera_core.audit import AuditRecord, AuthAuditRecord
from vera_core.config import Settings
from vera_core.models.enums import AccountType

_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
_PATH = "/api/v1/tenant/config/concurrency"
_MANAGE = frozenset({"tenant:config:manage"})

# Invariant across every test — hoisted so the app factory doesn't re-allocate them
# (Settings() re-parses the environment on each construction).
_SETTINGS = Settings()  # all fields have defaults
_FAKE_IDENTITY = VerifiedIdentity(
    user_id=_USER_ID,
    subject="admin@example.com",
    email="admin@example.com",
    tenant_id=_TENANT_ID,
    account_type=AccountType.TENANT,
    session_id=uuid4(),
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTenant:
    """Minimal Tenant stand-in — only the columns the endpoints touch."""

    def __init__(self, max_agents_per_va: int = 3, max_concurrent_calls: int = 25) -> None:
        self.max_agents_per_va = max_agents_per_va
        self.max_concurrent_calls = max_concurrent_calls


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    def __init__(self, tenant: _FakeTenant) -> None:
        self._tenant = tenant

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._tenant)


class _FakeResolver:
    """Returns a fixed permission set without touching the DB."""

    def __init__(self, permissions: frozenset[str]) -> None:
        self._permissions = permissions

    async def effective_permissions(
        self, _session: object, _tenant_id: UUID | None, user_id: UUID
    ) -> tuple[UUID, frozenset[str]]:
        return user_id, self._permissions


class _SpyAuthAudit:
    """Captures AuthAuditRecords for assertion."""

    def __init__(self) -> None:
        self.records: list[AuthAuditRecord] = []

    async def emit(self, record: AuthAuditRecord) -> None:
        self.records.append(record)


class _NullAudit:
    """Drop-box for the PHI audit_log records that require() emits."""

    async def emit(self, _record: AuditRecord) -> None:
        pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    *,
    permissions: frozenset[str],
    tenant: _FakeTenant,
    spy: _SpyAuthAudit,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)

    # State-based deps (read inside dep functions via request.app.state)
    app.state.permission_resolver = _FakeResolver(permissions)
    app.state.auth_audit = spy
    app.state.audit = _NullAudit()
    app.state.settings = _SETTINGS

    async def _identity() -> VerifiedIdentity:
        return _FAKE_IDENTITY

    async def _tenant_id() -> UUID:
        return _TENANT_ID

    async def _session() -> AsyncGenerator[Any, None]:
        yield _FakeSession(tenant)

    # Override the auth/session deps that drag in Redis + Postgres
    app.dependency_overrides[current_identity] = _identity
    app.dependency_overrides[current_tenant_id] = _tenant_id
    app.dependency_overrides[tenant_scoped_session] = _session

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant() -> _FakeTenant:
    return _FakeTenant()


@pytest.fixture
def spy() -> _SpyAuthAudit:
    return _SpyAuthAudit()


@pytest.fixture
async def client(
    tenant: _FakeTenant, spy: _SpyAuthAudit
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """A client whose caller holds tenant:config:manage — the happy-path fixture."""
    app = _build_app(permissions=_MANAGE, tenant=tenant, spy=spy)
    async with _client(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_returns_both_knobs(client: httpx.AsyncClient) -> None:
    """GET returns both concurrency knobs from the tenant row."""
    resp = await client.get(_PATH)

    assert resp.status_code == 200
    assert resp.json()["data"] == {"max_agents_per_va": 3, "max_concurrent_calls": 25}


async def test_patch_one_knob_leaves_the_other(
    client: httpx.AsyncClient, tenant: _FakeTenant, spy: _SpyAuthAudit
) -> None:
    """PATCH one knob persists it, leaves the other unchanged, and audits old/new."""
    resp = await client.patch(_PATH, json={"max_agents_per_va": 5})

    assert resp.status_code == 200
    assert resp.json()["data"] == {"max_agents_per_va": 5, "max_concurrent_calls": 25}
    assert tenant.max_agents_per_va == 5
    assert tenant.max_concurrent_calls == 25
    assert any(
        r.event_type == "concurrency_config_updated"
        and r.meta
        == {
            "old": {"max_agents_per_va": 3, "max_concurrent_calls": 25},
            "new": {"max_agents_per_va": 5, "max_concurrent_calls": 25},
        }
        for r in spy.records
    )


async def test_patch_both_knobs(client: httpx.AsyncClient, tenant: _FakeTenant) -> None:
    """PATCH with both knobs set persists both."""
    resp = await client.patch(_PATH, json={"max_agents_per_va": 2, "max_concurrent_calls": 40})

    assert resp.status_code == 200
    assert tenant.max_agents_per_va == 2
    assert tenant.max_concurrent_calls == 40


@pytest.mark.parametrize(
    "body",
    [
        {"max_agents_per_va": 0},
        {"max_agents_per_va": 21},
        {"max_concurrent_calls": 0},
        {"max_concurrent_calls": 101},
    ],
)
async def test_out_of_bounds_is_422(client: httpx.AsyncClient, body: dict[str, int]) -> None:
    """A knob outside its Pydantic bounds is a validation error, not a 500."""
    resp = await client.patch(_PATH, json=body)

    assert resp.status_code == 422


async def test_caller_without_permission_gets_403(tenant: _FakeTenant, spy: _SpyAuthAudit) -> None:
    """A caller whose role does not hold tenant:config:manage is denied at the gate."""
    app = _build_app(permissions=frozenset(), tenant=tenant, spy=spy)
    async with _client(app) as client:
        resp = await client.get(_PATH)

    assert resp.status_code == 403
