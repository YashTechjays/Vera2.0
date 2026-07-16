"""Unit tests for GET/PATCH /api/v1/tenant/config/retention (Task 10).

Exercised without a database: heavyweight deps are stubbed via
dependency_overrides and app.state injection, so only the endpoint logic in
tenant_config.py is under test.

Cases:
  1. GET fresh tenant → retention_days=None, default_days=90
  2. PATCH {retention_days:30} → 200, persisted, auth-audit meta correct
  3. PATCH {retention_days:0} → 422 (Pydantic ge=1 violation)
  4. Caller without recordings:manage → 403
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

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
_PATH = "/api/v1/tenant/config/retention"
_MANAGE = frozenset({"recordings:manage"})

# Invariant across every test — hoisted so the app factory doesn't re-allocate them
# (Settings() re-parses the environment on each construction).
_SETTINGS = Settings()  # all fields have defaults; default_days=90
_FAKE_IDENTITY = VerifiedIdentity(
    user_id=_USER_ID,
    subject="admin@example.com",
    email="admin@example.com",
    tenant_id=_TENANT_ID,
    account_type=AccountType.TENANT,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTenant:
    """Minimal Tenant stand-in — only the column the endpoints touch."""

    def __init__(self, recording_retention_days: int | None = None) -> None:
        self.recording_retention_days = recording_retention_days


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
    """A client whose caller holds recordings:manage — the happy-path fixture."""
    app = _build_app(permissions=_MANAGE, tenant=tenant, spy=spy)
    async with _client(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_fresh_tenant_returns_none_and_default(client: httpx.AsyncClient) -> None:
    """GET on a fresh tenant (recording_retention_days IS NULL) returns
    retention_days=None and default_days from settings (90)."""
    resp = await client.get(_PATH)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["retention_days"] is None
    assert data["default_days"] == 90


async def test_patch_persists_and_audits_old_new(
    client: httpx.AsyncClient, tenant: _FakeTenant, spy: _SpyAuthAudit
) -> None:
    """PATCH 30 days → 200, tenant mutated, auth-audit records old/new days."""
    resp = await client.patch(_PATH, json={"retention_days": 30})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["retention_days"] == 30
    assert data["default_days"] == 90
    # The endpoint must mutate the tenant object (session.add is implicit via ORM tracking)
    assert tenant.recording_retention_days == 30
    # Auth-audit row must record the before/after
    assert any(
        r.event_type == "retention_policy_updated" and r.meta == {"old_days": None, "new_days": 30}
        for r in spy.records
    )


async def test_patch_zero_is_422(client: httpx.AsyncClient) -> None:
    """retention_days=0 violates ge=1 — Pydantic raises a validation error."""
    resp = await client.patch(_PATH, json={"retention_days": 0})

    assert resp.status_code == 422


async def test_caller_without_permission_gets_403(tenant: _FakeTenant, spy: _SpyAuthAudit) -> None:
    """A caller whose role does not hold recordings:manage is denied at the gate."""
    app = _build_app(permissions=frozenset(), tenant=tenant, spy=spy)
    async with _client(app) as client:
        resp = await client.get(_PATH)

    assert resp.status_code == 403
