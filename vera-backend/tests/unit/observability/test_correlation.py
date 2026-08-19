from vera_core.db import uuid7
from vera_core.observability import (
    call_trace_attributes,
    parse_room_name,
    room_name_for_call,
)
from vera_core.observability.correlation import (
    CALL_TRACE_NAME,
    TRACE_NAME_ATTR,
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
    attrs = call_trace_attributes(room, in_call_trace=False)
    assert attrs["langfuse.session.id"] == room
    assert attrs["vera.tenant_id"] == str(tenant_id)
    assert attrs["vera.call_id"] == str(call_id)


def test_a_span_inside_the_call_trace_restates_its_name() -> None:
    """Langfuse re-derives a trace from EVERY span carrying langfuse.session.id, and on
    that path reads the name only from langfuse.trace.name — a child span omitting it
    writes the name as empty, which is how a whole call trace lands unnamed."""
    room = room_name_for_call(uuid7(), uuid7())
    assert call_trace_attributes(room, in_call_trace=True)[TRACE_NAME_ATTR] == CALL_TRACE_NAME


def test_a_span_in_another_trace_never_renames_it() -> None:
    # The dispatch span and a post-call span whose trace link expired both group into
    # the call's SESSION while rooting traces of their own; naming those job_entrypoint
    # would hide a broken trace link behind a trace that looks worker-rooted.
    room = room_name_for_call(uuid7(), uuid7())
    assert TRACE_NAME_ATTR not in call_trace_attributes(room, in_call_trace=False)


def test_foreign_room_still_gets_room_attribute() -> None:
    assert call_trace_attributes("lobby", in_call_trace=False)["vera.room"] == "lobby"


def test_a_foreign_room_names_no_trace() -> None:
    # A console mic test carries no session id, so nothing re-derives its trace.
    assert TRACE_NAME_ATTR not in call_trace_attributes("lobby", in_call_trace=True)


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
