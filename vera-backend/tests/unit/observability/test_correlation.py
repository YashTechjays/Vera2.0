from vera_core.db import uuid7
from vera_core.observability import (
    call_trace_attributes,
    parse_room_name,
    room_name_for_call,
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
