"""Unit tests for GET /api/v1/calls/{call_id}/recording (Task 11).

All heavyweight deps are stubbed: no database, no GCS, no Redis.
recording_storage is injected via app.state; session is a queue-draining fake.

Cases:
  1. Owner + AVAILABLE recording → 200; signed URL; RECORDING_ACCESSED audited
  2. Non-owner + unpublished call → 404 (no enumeration)
  3. Non-owner + published call → 200
  4. Non-owner + revoked on a published call → 404
  5. Recording status PENDING → 409
  6. No recording row → 404
  7. Caller without recordings:read → 403
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI

from control_plane.api.v1.calls import router
from control_plane.auth.identity import VerifiedIdentity
from control_plane.deps import (
    current_identity,
    current_tenant_id,
    tenant_scoped_session,
)
from control_plane.exceptions import register_exception_handlers
from control_plane.recording_storage import InMemoryRecordingStorage
from control_plane.request_context import RequestIdMiddleware
from vera_core.audit import AuditRecord
from vera_core.config import Settings
from vera_core.models.enums import AccountType

# ──────────────────────────────────────────────────────────
# Fixed UUIDs (stable across all test runs)
# ──────────────────────────────────────────────────────────
_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_OWNER_ID = UUID("00000000-0000-0000-0000-000000000002")
_OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000003")
_CALL_ID = UUID("00000000-0000-0000-0000-000000000010")
_RECORDING_ID = UUID("00000000-0000-0000-0000-000000000020")

_SETTINGS = Settings()  # all fields have defaults; recording_signed_url_ttl_seconds=600
_READ = frozenset({"recordings:read"})

_OWNER_IDENTITY = VerifiedIdentity(
    user_id=_OWNER_ID,
    subject="owner@example.com",
    email="owner@example.com",
    tenant_id=_TENANT_ID,
    account_type=AccountType.TENANT,
)
_OTHER_IDENTITY = VerifiedIdentity(
    user_id=_OTHER_USER_ID,
    subject="other@example.com",
    email="other@example.com",
    tenant_id=_TENANT_ID,
    account_type=AccountType.TENANT,
)

_GCS_URI = "gs://test-bucket/tenants/t1/calls/c1.mp4"


# ──────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────


@dataclass
class _FakeCall:
    """Minimal Call stand-in — only the fields the endpoint touches."""

    id: UUID = _CALL_ID
    initiated_by_id: UUID | None = _OWNER_ID
    revoked_user_ids: list[str] = field(default_factory=list)
    published: bool = True


@dataclass
class _FakeRecording:
    """Minimal Recording stand-in."""

    id: UUID = _RECORDING_ID
    call_id: UUID = _CALL_ID
    status: str = "available"
    gcs_uri: str = _GCS_URI


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    """Drains results from a FIFO queue; extra execute calls return None."""

    def __init__(self, *results: object) -> None:
        self._queue = list(results)

    async def execute(self, _stmt: object) -> _FakeResult:
        value = self._queue.pop(0) if self._queue else None
        return _FakeResult(value)


class _FakeResolver:
    """Returns a fixed permission set without touching the DB."""

    def __init__(self, permissions: frozenset[str]) -> None:
        self._permissions = permissions

    async def effective_permissions(
        self, _session: object, _tenant_id: UUID | None, user_id: UUID
    ) -> tuple[UUID, frozenset[str]]:
        return user_id, self._permissions


class _SpyAudit:
    """Captures AuditRecords for assertion."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


# ──────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────


def _build_app(
    *,
    permissions: frozenset[str],
    call: object,
    recording: object = None,
    storage: object = None,
    spy: _SpyAudit | None = None,
    identity: VerifiedIdentity = _OWNER_IDENTITY,
) -> FastAPI:
    """Build a minimal FastAPI app wired to the calls router with all heavy deps stubbed."""
    if storage is None:
        storage = InMemoryRecordingStorage()
    if spy is None:
        spy = _SpyAudit()

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)

    app.state.permission_resolver = _FakeResolver(permissions)
    app.state.audit = spy
    app.state.settings = _SETTINGS
    app.state.recording_storage = storage

    async def _identity() -> VerifiedIdentity:
        return identity

    async def _tenant_id() -> UUID:
        return _TENANT_ID

    async def _session() -> AsyncGenerator[Any, None]:
        yield _FakeSession(call, recording)

    app.dependency_overrides[current_identity] = _identity
    app.dependency_overrides[current_tenant_id] = _tenant_id
    app.dependency_overrides[tenant_scoped_session] = _session

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


_PATH = f"/api/v1/calls/{_CALL_ID}/recording"


# ──────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────


async def test_owner_available_recording_returns_signed_url() -> None:
    """Owner with recordings:read + AVAILABLE recording → 200, signed URL, audit record."""
    spy = _SpyAudit()
    app = _build_app(permissions=_READ, call=_FakeCall(), recording=_FakeRecording(), spy=spy)

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["url"].startswith("https://storage.local/")
    assert "expires_at" in data
    assert resp.headers["cache-control"] == "no-store"

    # Audit: exactly one RECORDING_ACCESSED record (plus authz.allow from require)
    accessed = [r for r in spy.records if r.event_type == "recording.accessed"]
    assert len(accessed) == 1
    assert accessed[0].resource_id == str(_RECORDING_ID)


async def test_non_owner_unpublished_call_is_404() -> None:
    """Non-owner on an unpublished call → 404 (no-enumeration, same shape as missing call)."""
    app = _build_app(
        permissions=_READ,
        call=_FakeCall(published=False),
        identity=_OTHER_IDENTITY,
    )

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 404


async def test_non_owner_published_call_returns_200() -> None:
    """Non-owner on a published call with AVAILABLE recording → 200."""
    app = _build_app(
        permissions=_READ,
        call=_FakeCall(),  # published=True by default
        recording=_FakeRecording(),
        identity=_OTHER_IDENTITY,
    )

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["url"].startswith("https://storage.local/")


async def test_revoked_user_on_published_call_is_404() -> None:
    """A user in revoked_user_ids gets 404 even on a published call."""
    app = _build_app(
        permissions=_READ,
        call=_FakeCall(revoked_user_ids=[str(_OTHER_USER_ID)]),
        identity=_OTHER_IDENTITY,
    )

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 404


async def test_pending_recording_returns_409() -> None:
    """PENDING recording → 409 Conflict (not yet available)."""
    app = _build_app(
        permissions=_READ, call=_FakeCall(), recording=_FakeRecording(status="pending")
    )

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 409


async def test_no_recording_row_returns_404() -> None:
    """No recording row for the call → 404."""
    app = _build_app(permissions=_READ, call=_FakeCall())

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 404


async def test_missing_recordings_read_permission_returns_403() -> None:
    """Caller without recordings:read is denied at the RBAC gate."""
    app = _build_app(permissions=frozenset(), call=_FakeCall())

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 403


async def test_storage_unconfigured_returns_409() -> None:
    """Permissioned caller + visible call but storage=None → 409 config error."""
    app = _build_app(permissions=_READ, call=_FakeCall(), recording=_FakeRecording())
    app.state.recording_storage = None

    async with _client(app) as c:
        resp = await c.get(_PATH)

    assert resp.status_code == 409
