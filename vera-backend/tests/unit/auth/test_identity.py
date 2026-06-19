"""Unit tests for session-based identity verification (step 1 of the chain).

Login mints an opaque token into a SessionStore; SessionVerifier resolves it to
a VerifiedIdentity. A pending-MFA challenge (mfa_passed=False, or living in the
"mfa" namespace) must never verify as a real session.
"""

from uuid import UUID

import pytest

from control_plane.auth.identity import InvalidTokenError, VerifiedIdentity
from control_plane.auth.session import (
    MFA_NS,
    SESSION_NS,
    InMemorySessionStore,
    SessionData,
    SessionVerifier,
)
from vera_core.models.enums import AccountType

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
USER = UUID("00000000-0000-0000-0000-0000000000cc")


def _data(*, mfa_passed: bool = True) -> SessionData:
    return SessionData(
        user_id=USER,
        tenant_id=TENANT,
        email="a@example.com",
        subject="a@example.com",
        provider_type="password",
        mfa_passed=mfa_passed,
        account_type="tenant",
        tenant_slug="acme",
    )


def test_session_data_json_roundtrip() -> None:
    data = _data()
    assert SessionData.from_json(data.to_json()) == data


async def test_verify_returns_identity_for_full_session() -> None:
    store = InMemorySessionStore()
    token = await store.put(SESSION_NS, _data(), 60)

    identity = await SessionVerifier(store).verify(token)

    assert identity == VerifiedIdentity(
        user_id=USER,
        subject="a@example.com",
        email="a@example.com",
        tenant_id=TENANT,
        account_type=AccountType.TENANT,
        tenant_slug="acme",
    )


async def test_verify_unknown_token_raises() -> None:
    with pytest.raises(InvalidTokenError):
        await SessionVerifier(InMemorySessionStore()).verify("no-such-token")


async def test_verify_rejects_session_without_mfa_passed() -> None:
    store = InMemorySessionStore()
    token = await store.put(SESSION_NS, _data(mfa_passed=False), 60)
    with pytest.raises(InvalidTokenError):
        await SessionVerifier(store).verify(token)


async def test_verify_rejects_mfa_challenge_token() -> None:
    store = InMemorySessionStore()
    challenge = await store.put(MFA_NS, _data(mfa_passed=False), 60)
    with pytest.raises(InvalidTokenError):
        await SessionVerifier(store).verify(challenge)


async def test_store_entry_expires() -> None:
    store = InMemorySessionStore()
    token = await store.put(SESSION_NS, _data(), 0)  # already expired
    assert await store.get(SESSION_NS, token) is None


async def test_store_delete_revokes() -> None:
    store = InMemorySessionStore()
    token = await store.put(SESSION_NS, _data(), 60)
    await store.delete(SESSION_NS, token)
    assert await store.get(SESSION_NS, token) is None


async def test_store_namespaces_are_isolated() -> None:
    store = InMemorySessionStore()
    token = await store.put(SESSION_NS, _data(), 60)
    assert await store.get(MFA_NS, token) is None
