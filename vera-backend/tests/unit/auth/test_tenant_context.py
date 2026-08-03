"""Unit tests for the tenant_context resolver (Task 3).

`tenant_context` returns the operating tenant from the verified session:
- TENANT users: pins to their own tenant_id (no DB hit).
- PLATFORM operators: looks up their single active elevation grant; 403 if none.
Invariant mismatches (wrong nullability for account_type) raise 401 fail-closed.

All 5 cases run without a database — the elevation lookup is monkeypatched at the
`control_plane.deps` module boundary, matching the pattern used in test_tenant_guard.py.
"""

from contextlib import asynccontextmanager
from datetime import UTC
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.elevation import ElevationGrant
from control_plane.auth.identity import VerifiedIdentity
from control_plane.deps import TenantContext, tenant_context
from vera_core.models.enums import AccountType

from .conftest import make_request

TENANT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
TARGET_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000bb")
USER_ID = UUID("00000000-0000-0000-0000-0000000000cc")
GRANT_ID = UUID("00000000-0000-0000-0000-0000000000dd")

_NO_SESSIONMAKER = cast(Any, None)

# A fake sessionmaker whose sessions are never actually used (for PLATFORM tests where the
# grant lookup is monkeypatched — the session arg is passed through but never executed).
_FAKE_SESSION = cast(AsyncSession, object())


class _FakeSessionmaker:
    """Callable that returns an async context manager yielding a stub session.
    Matches the `async with sessionmaker() as session:` call pattern in tenant_context."""

    def __call__(self) -> Any:
        @asynccontextmanager
        async def _cm() -> Any:
            yield _FAKE_SESSION

        return _cm()


_PLATFORM_SESSIONMAKER = cast(Any, _FakeSessionmaker())


def _tenant_identity(tenant_id: UUID | None = TENANT_ID) -> VerifiedIdentity:
    return VerifiedIdentity(
        user_id=USER_ID,
        subject="user@example.com",
        email="user@example.com",
        tenant_id=tenant_id,
        account_type=AccountType.TENANT,
        session_id=uuid4(),
    )


def _platform_identity(tenant_id: UUID | None = None) -> VerifiedIdentity:
    return VerifiedIdentity(
        user_id=USER_ID,
        subject="admin@example.com",
        email="admin@example.com",
        tenant_id=tenant_id,
        account_type=AccountType.PLATFORM,
        session_id=uuid4(),
    )


def _make_grant() -> ElevationGrant:
    from datetime import datetime

    now = datetime.now(tz=UTC)
    return ElevationGrant(
        id=GRANT_ID,
        super_admin_user_id=USER_ID,
        target_tenant_id=TARGET_TENANT_ID,
        reason="support",
        granted_at=now,
        expires_at=now,
        ended_at=None,
    )


# ---------------------------------------------------------------------------
# Test 1: tenant user with tenant_id set → TenantContext(tenant_id, None)
# ---------------------------------------------------------------------------


async def test_tenant_user_returns_tenant_context() -> None:
    request = make_request(cast(Any, None))
    result = await tenant_context(request, _tenant_identity(), _NO_SESSIONMAKER)
    assert result == TenantContext(tenant_id=TENANT_ID, elevation_grant_id=None)


# ---------------------------------------------------------------------------
# Test 2: tenant user with tenant_id=None → 401 (invariant broken)
# ---------------------------------------------------------------------------


async def test_tenant_user_with_null_tenant_id_raises_401() -> None:
    request = make_request(cast(Any, None))
    with pytest.raises(HTTPException) as exc:
        await tenant_context(request, _tenant_identity(tenant_id=None), _NO_SESSIONMAKER)
    assert exc.value.status_code == 401
    assert "malformed" in exc.value.detail


# ---------------------------------------------------------------------------
# Test 3: platform operator with active grant → TenantContext + vera_elevation stamped
# ---------------------------------------------------------------------------


async def test_platform_operator_with_grant_returns_context_and_stamps_elevation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant = _make_grant()

    async def _fake_grant(_session: Any, *, operator: UUID) -> ElevationGrant | None:
        return grant

    monkeypatch.setattr("control_plane.deps.active_grant_for_operator", _fake_grant)

    request = make_request(cast(Any, None))
    result = await tenant_context(request, _platform_identity(), _PLATFORM_SESSIONMAKER)

    assert result == TenantContext(tenant_id=TARGET_TENANT_ID, elevation_grant_id=GRANT_ID)
    assert request.state.vera_elevation == GRANT_ID


# ---------------------------------------------------------------------------
# Test 4: platform operator with no active grant → 403
# ---------------------------------------------------------------------------


async def test_platform_operator_with_no_grant_raises_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_grant(_session: Any, *, operator: UUID) -> ElevationGrant | None:
        return None

    monkeypatch.setattr("control_plane.deps.active_grant_for_operator", _no_grant)

    request = make_request(cast(Any, None))
    with pytest.raises(HTTPException) as exc:
        await tenant_context(request, _platform_identity(), _PLATFORM_SESSIONMAKER)
    assert exc.value.status_code == 403
    assert "elevation" in exc.value.detail


# ---------------------------------------------------------------------------
# Test 5: platform operator with non-null tenant_id → 401 (invariant broken)
# ---------------------------------------------------------------------------


async def test_platform_operator_with_non_null_tenant_id_raises_401() -> None:
    request = make_request(cast(Any, None))
    with pytest.raises(HTTPException) as exc:
        await tenant_context(request, _platform_identity(tenant_id=TENANT_ID), _NO_SESSIONMAKER)
    assert exc.value.status_code == 401
    assert "malformed" in exc.value.detail
