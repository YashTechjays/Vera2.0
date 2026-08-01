from vera_core.db import uuid7
from vera_core.observability import (
    call_trace_attributes,
    parse_room_name,
    room_name_for_call,
)
from vera_core.observability.correlation import (
    is_observer_identity,
    supervisor_identity,
    supervisor_user_id,
)


def test_room_name_round_trip() -> None:
    tenant_id, call_id = uuid7(), uuid7()
    ref = parse_room_name(room_name_for_call(tenant_id, call_id))
    assert ref is not None
    assert ref.tenant_id == tenant_id
    assert ref.call_id == call_id


def test_foreign_room_names_are_rejected() -> None:
    assert parse_room_name("playground-abc") is None
    assert parse_room_name("call--nope--nope") is None


def test_trace_attributes_carry_session_correlation() -> None:
    tenant_id, call_id = uuid7(), uuid7()
    room = room_name_for_call(tenant_id, call_id)
    attrs = call_trace_attributes(room)
    assert attrs["langfuse.session.id"] == room
    assert attrs["vera.tenant_id"] == str(tenant_id)
    assert attrs["vera.call_id"] == str(call_id)


def test_foreign_room_still_gets_room_attribute() -> None:
    assert call_trace_attributes("lobby")["vera.room"] == "lobby"


def test_supervisor_identity_is_per_session_not_per_user() -> None:
    """Two browsers on one account must present DIFFERENT identities: LiveKit
    force-disconnects the incumbent when a duplicate identity joins a room."""
    user_id = uuid7()
    first = supervisor_identity(user_id, uuid7())
    second = supervisor_identity(user_id, uuid7())

    assert first != second
    assert supervisor_user_id(first) == user_id
    assert supervisor_user_id(second) == user_id
    assert is_observer_identity(first)


def test_supervisor_identity_is_stable_for_one_session() -> None:
    """Same session ⇒ same identity, so a browser's own reconnect still evicts its
    stale participant instead of leaving a ghost in the room."""
    user_id, session_id = uuid7(), uuid7()
    assert supervisor_identity(user_id, session_id) == supervisor_identity(user_id, session_id)


def test_supervisor_user_id_reads_a_session_less_identity() -> None:
    """Tokens minted before the session suffix existed stay parseable."""
    user_id = uuid7()
    assert supervisor_user_id(f"supervisor-{user_id}") == user_id


def test_supervisor_user_id_rejects_foreign_and_malformed_identities() -> None:
    assert supervisor_user_id("phone-callee") is None
    assert supervisor_user_id(f"monitor-{uuid7()}") is None
    assert supervisor_user_id("supervisor-not-a-uuid") is None
