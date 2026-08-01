"""Unit tests for `call_authz` — the visibility and owner-OR-permission rules
shared by Intervene (join_token) and Coaching. No DB/session needed: the
resolver is faked, and `Call` is a plain in-memory ORM instance (no query,
no flush)."""

from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver
from control_plane.call_authz import authorize_or_403, authorize_publish, call_hidden_from
from control_plane.exceptions import CustomAPIException
from tests.unit.auth.conftest import SpyAudit, make_request
from vera_core.models import Call
from vera_core.models.enums import AccountType, CallStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _caller(user_id: UUID | None = None) -> VerifiedIdentity:
    return VerifiedIdentity(
        user_id=user_id or uuid4(),
        subject="a@example.com",
        email="a@example.com",
        tenant_id=uuid4(),
        account_type=AccountType.TENANT,
        session_id=uuid4(),
    )


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


@pytest.mark.parametrize(
    ("published", "status", "hidden"),
    [
        (False, CallStatus.ACTIVE, False),  # unclaimed dispatcher call stays visible
        (False, CallStatus.COMPLETED, True),  # terminal: nothing left to claim (VR2-62)
        (True, CallStatus.COMPLETED, False),  # published trumps ownerless-terminal
    ],
)
def test_ownerless_call_visibility(published: bool, status: CallStatus, hidden: bool) -> None:
    """Ownerless visibility is a live-queue affordance; once terminal (owner
    deleted — SET NULL) the call goes private unless it was published."""
    call = Call(initiated_by_id=None, published=published, current_status=status.value)
    assert call_hidden_from(call, uuid4()) is hidden


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
async def test_authorize_or_403_audits_a_denial_regardless_of_audit_log_allows() -> None:
    """A denial is rare and security-relevant — it must reach the WORM
    audit_log even when the caller (e.g. coaching) asked to skip the noisy
    per-allow write."""
    call = Call(initiated_by_id=uuid4())  # someone else's call
    resolver = _FakeResolver(frozenset())
    spy_audit = SpyAudit()
    request = make_request(spy_audit)

    with pytest.raises(CustomAPIException):
        await authorize_or_403(
            call,
            uuid4(),
            _caller(),
            cast("AsyncSession", None),
            cast(PermissionResolver, resolver),
            spy_audit,
            request,
            audit_log_allows=False,
        )

    assert len(spy_audit.records) == 1
    assert spy_audit.records[0].decision == "deny"


@pytest.mark.asyncio
async def test_authorize_or_403_skips_the_worm_log_on_allow_when_disabled() -> None:
    """Coaching's per-message trail already lives in InterventionEvent — a WORM
    row per coaching message would be exactly the noise that ledger avoids."""
    owner_id = uuid4()
    call = Call(initiated_by_id=owner_id)
    resolver = _FakeResolver(frozenset())
    spy_audit = SpyAudit()
    request = make_request(spy_audit)

    await authorize_or_403(
        call,
        uuid4(),
        _caller(owner_id),
        cast("AsyncSession", None),
        cast(PermissionResolver, resolver),
        spy_audit,
        request,
        audit_log_allows=False,
    )

    assert spy_audit.records == []


@pytest.mark.asyncio
async def test_authorize_or_403_audits_an_allow_by_default() -> None:
    """join_token's intervene branch relies on the default: a session-level
    grant is low-frequency and worth the WORM record."""
    owner_id = uuid4()
    call = Call(initiated_by_id=owner_id)
    resolver = _FakeResolver(frozenset())
    spy_audit = SpyAudit()
    request = make_request(spy_audit)

    await authorize_or_403(
        call,
        uuid4(),
        _caller(owner_id),
        cast("AsyncSession", None),
        cast(PermissionResolver, resolver),
        spy_audit,
        request,
    )

    assert len(spy_audit.records) == 1
    assert spy_audit.records[0].decision == "allow"


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
