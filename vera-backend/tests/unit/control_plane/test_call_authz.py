"""Unit tests for `call_authz` — the visibility and owner-OR-permission rules
shared by Intervene (join_token) and Coaching. No DB/session needed: the
resolver is faked, and `Call` is a plain in-memory ORM instance (no query,
no flush)."""

from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from control_plane.auth.rbac import PermissionResolver
from control_plane.call_authz import authorize_publish, call_hidden_from
from vera_core.models import Call

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeResolver:
    def __init__(self, permissions: frozenset[str]) -> None:
        self._permissions = permissions
        self.calls = 0

    async def effective_permissions(
        self, session: object, tenant_id: object, user_id: object
    ) -> tuple[object, frozenset[str]]:
        self.calls += 1
        return user_id, self._permissions


def test_owner_never_hidden_from_themselves() -> None:
    owner_id = uuid4()
    call = Call(initiated_by_id=owner_id, published=False)
    assert call_hidden_from(call, owner_id) is False


def test_private_call_hidden_from_a_non_owner() -> None:
    call = Call(initiated_by_id=uuid4(), published=False)
    assert call_hidden_from(call, uuid4()) is True


def test_published_call_visible_to_a_non_owner() -> None:
    call = Call(initiated_by_id=uuid4(), published=True)
    assert call_hidden_from(call, uuid4()) is False


def test_ownerless_call_visible_to_anyone() -> None:
    call = Call(initiated_by_id=None, published=False)
    assert call_hidden_from(call, uuid4()) is False


@pytest.mark.asyncio
async def test_owner_is_authorized_without_consulting_the_resolver() -> None:
    owner_id = uuid4()
    call = Call(initiated_by_id=owner_id)
    resolver = _FakeResolver(frozenset())  # no permissions at all

    allowed, granted_via = await authorize_publish(
        call, uuid4(), owner_id, cast("AsyncSession", None), cast(PermissionResolver, resolver)
    )

    assert (allowed, granted_via) == (True, "owner")
    assert resolver.calls == 0  # ownership short-circuits the permission lookup


@pytest.mark.asyncio
async def test_non_owner_with_the_permission_is_authorized() -> None:
    call = Call(initiated_by_id=uuid4())  # someone else's call
    resolver = _FakeResolver(frozenset({"calls:intervene"}))

    allowed, granted_via = await authorize_publish(
        call, uuid4(), uuid4(), cast("AsyncSession", None), cast(PermissionResolver, resolver)
    )

    assert (allowed, granted_via) == (True, "permission")


@pytest.mark.asyncio
async def test_non_owner_without_the_permission_is_denied() -> None:
    call = Call(initiated_by_id=uuid4())
    resolver = _FakeResolver(frozenset())

    allowed, granted_via = await authorize_publish(
        call, uuid4(), uuid4(), cast("AsyncSession", None), cast(PermissionResolver, resolver)
    )

    assert (allowed, granted_via) == (False, "permission")


@pytest.mark.asyncio
async def test_ownerless_call_falls_through_to_the_permission_check() -> None:
    """A pre-dispatch call with no owner yet is never treated as "everyone owns it" —
    only the permission path can authorize it."""
    call = Call(initiated_by_id=None)
    resolver = _FakeResolver(frozenset({"calls:intervene"}))

    allowed, granted_via = await authorize_publish(
        call, uuid4(), uuid4(), cast("AsyncSession", None), cast(PermissionResolver, resolver)
    )

    assert (allowed, granted_via) == (True, "permission")
