"""The owner-or-published visibility predicate shared by the playback endpoint
and the call-attempt DTO enrichment (spec 2026-07-22-recording-playback-ui)."""

from uuid import uuid4

from vera_core.services.call_visibility import call_hidden_from

OWNER = uuid4()
OTHER = uuid4()


def test_owner_always_sees_their_call() -> None:
    assert call_hidden_from(OWNER, False, OWNER) is False
    assert call_hidden_from(OWNER, True, OWNER) is False


def test_non_owner_hidden_until_published() -> None:
    assert call_hidden_from(OWNER, False, OTHER) is True
    assert call_hidden_from(OWNER, True, OTHER) is False


def test_ownerless_call_is_tenant_visible() -> None:
    assert call_hidden_from(None, False, OTHER) is False
    assert call_hidden_from(None, False, None) is False
