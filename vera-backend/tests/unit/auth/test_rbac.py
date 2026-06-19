"""Unit tests for RBAC resolution and require() (step 3 of the authz chain).

Two pieces, both exercised without a database:
  * PermissionResolver.effective_permissions — the user lookup (by app_user.id,
    the key the session carries) + cache + grant query, driven by a fake session.
  * the require() dependency — the allow/deny decision and the audit record it
    writes either way.
"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.rbac import PermissionResolver, require
from vera_core.models.enums import AccountType

from .conftest import SpyAudit, make_request

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
USER_ID = UUID("00000000-0000-0000-0000-0000000000cc")


# --- fakes -------------------------------------------------------------------


class FakeResult:
    """Stands in for a SQLAlchemy Result: serves either a scalar row or a
    column of permission codes, whichever the caller asks for."""

    def __init__(self, *, scalar: object = None, codes: tuple[str, ...] = ()) -> None:
        self._scalar = scalar
        self._codes = codes

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def scalars(self) -> tuple[str, ...]:
        return self._codes


class FakeSession:
    """Returns queued FakeResults in order and counts execute() calls, so a
    test can assert the grant query was (or wasn't) run."""

    def __init__(self, results: list[FakeResult]) -> None:
        self._results = results
        self.execute_calls = 0

    async def execute(self, statement: object) -> FakeResult:
        self.execute_calls += 1
        return self._results.pop(0)


def _session(results: list[FakeResult]) -> AsyncSession:
    return cast(AsyncSession, FakeSession(results))


# --- PermissionResolver.effective_permissions --------------------------------


async def test_effective_permissions_unknown_user_returns_empty() -> None:
    resolver = PermissionResolver(InMemoryPermissionCache())
    session = _session([FakeResult(scalar=None)])  # no matching active user

    user_id, perms = await resolver.effective_permissions(session, TENANT, USER_ID)

    assert user_id is None
    assert perms == frozenset()


async def test_effective_permissions_queries_and_caches_on_miss() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    resolver = PermissionResolver(cache)
    user = SimpleNamespace(id=USER_ID)
    fake = FakeSession([FakeResult(scalar=user), FakeResult(codes=("calls:read", "phi:read"))])

    user_id, perms = await resolver.effective_permissions(cast(AsyncSession, fake), TENANT, USER_ID)

    assert user_id == USER_ID
    assert perms == frozenset({"calls:read", "phi:read"})
    assert fake.execute_calls == 2  # user lookup + grant query
    assert await cache.get(TENANT, str(USER_ID)) == frozenset({"calls:read", "phi:read"})


async def test_effective_permissions_uses_cache_and_skips_grant_query() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    await cache.set(TENANT, str(USER_ID), frozenset({"calls:read"}))
    resolver = PermissionResolver(cache)
    user = SimpleNamespace(id=USER_ID)
    fake = FakeSession([FakeResult(scalar=user)])  # only the user lookup is queued

    user_id, perms = await resolver.effective_permissions(cast(AsyncSession, fake), TENANT, USER_ID)

    assert user_id == USER_ID
    assert perms == frozenset({"calls:read"})
    assert fake.execute_calls == 1  # cache hit -> grant query never runs


async def test_invalidate_clears_cache_entry() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    await cache.set(TENANT, str(USER_ID), frozenset({"calls:read"}))
    resolver = PermissionResolver(cache)

    await resolver.invalidate(TENANT, USER_ID)

    assert await cache.get(TENANT, str(USER_ID)) is None


# --- require() dependency ----------------------------------------------------


class FakeResolver:
    def __init__(self, user_id: UUID | None, permissions: frozenset[str]) -> None:
        self._user_id = user_id
        self._permissions = permissions

    async def effective_permissions(
        self, session: AsyncSession, tenant_id: UUID, user_id: UUID
    ) -> tuple[UUID | None, frozenset[str]]:
        return self._user_id, self._permissions


def _identity() -> VerifiedIdentity:
    return VerifiedIdentity(
        user_id=USER_ID,
        subject="a@example.com",
        email="a@example.com",
        tenant_id=TENANT,
        account_type=AccountType.TENANT,
    )


async def _call_require(
    permission: str, resolver: FakeResolver, audit: SpyAudit, *, path: str = "/api/v1/x/calls"
) -> Any:
    """Invoke the dependency that require() wraps, with all five deps faked."""
    dep = require(permission)
    inner = dep.dependency
    assert inner is not None
    return await inner(
        request=make_request(audit, path=path),
        identity=_identity(),
        tenant_id=TENANT,
        session=cast(AsyncSession, SimpleNamespace()),
        resolver=cast(Any, resolver),
    )


async def test_require_allows_when_permission_held(spy_audit: SpyAudit) -> None:
    resolver = FakeResolver(USER_ID, frozenset({"calls:read"}))

    returned = await _call_require("calls:read", resolver, spy_audit, path="/api/v1/x/calls")

    assert returned == _identity()
    assert len(spy_audit.records) == 1
    record = spy_audit.records[0]
    assert record.event_type == "authz.allow"
    assert record.decision == "allow"
    assert record.reason == ""
    assert record.permission_key == "calls:read"
    assert record.resource_id == "/api/v1/x/calls"
    assert record.actor_user_id == USER_ID
    assert record.detail == {"scope": "tenant"}


async def test_require_denies_when_permission_missing(spy_audit: SpyAudit) -> None:
    resolver = FakeResolver(USER_ID, frozenset({"calls:write"}))  # known user, wrong grant

    with pytest.raises(HTTPException) as exc:
        await _call_require("calls:read", resolver, spy_audit)
    assert exc.value.status_code == 403
    assert exc.value.detail == "missing permission calls:read"

    record = spy_audit.records[0]
    assert record.event_type == "authz.deny"
    assert record.decision == "deny"
    assert record.reason == "not granted"


async def test_require_denies_unknown_user_with_distinct_reason(spy_audit: SpyAudit) -> None:
    resolver = FakeResolver(None, frozenset())  # user_id None => not provisioned

    with pytest.raises(HTTPException) as exc:
        await _call_require("calls:read", resolver, spy_audit)
    assert exc.value.status_code == 403

    record = spy_audit.records[0]
    assert record.decision == "deny"
    assert record.reason == "unknown user"
    assert record.actor_user_id is None
