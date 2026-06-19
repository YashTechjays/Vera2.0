"""Unit tests for the in-memory invitation store + email sender (no Redis/SMTP)."""

from uuid import UUID

from control_plane.auth.invitations import (
    INVITE_NS,
    InMemoryInvitationStore,
    InviteData,
    _hashed,
    _key,
)
from control_plane.email import EmailMessage, InMemoryEmailSender

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
USER = UUID("00000000-0000-0000-0000-0000000000cc")


def _data() -> InviteData:
    return InviteData(tenant_id=TENANT, app_user_id=USER, email="invitee@example.com")


async def test_put_get_roundtrip() -> None:
    store = InMemoryInvitationStore()
    token = await store.put(INVITE_NS, _data(), 60)
    assert await store.get(INVITE_NS, token) == _data()


async def test_get_unknown_token_is_none() -> None:
    store = InMemoryInvitationStore()
    assert await store.get(INVITE_NS, "not-a-real-token") is None


async def test_single_use_delete() -> None:
    store = InMemoryInvitationStore()
    token = await store.put(INVITE_NS, _data(), 60)
    await store.delete(INVITE_NS, token)
    assert await store.get(INVITE_NS, token) is None


async def test_expired_token_is_none() -> None:
    store = InMemoryInvitationStore()
    token = await store.put(INVITE_NS, _data(), 0)  # already at the expiry horizon
    assert await store.get(INVITE_NS, token) is None


async def test_token_is_stored_hashed_not_raw() -> None:
    store = InMemoryInvitationStore()
    token = await store.put(INVITE_NS, _data(), 60)
    # The store key is derived from the HASH of the token, never the raw token.
    assert _key(INVITE_NS, token) in store._entries
    assert _hashed(token) != token


def test_invite_data_json_roundtrip() -> None:
    assert InviteData.from_json(_data().to_json()) == _data()


async def test_in_memory_email_sender_records() -> None:
    sender = InMemoryEmailSender()
    msg = EmailMessage(to="a@example.com", subject="hi", body="link")
    await sender.send(msg)
    assert sender.sent == [msg]
