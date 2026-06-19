"""Room-name correlation.

One verification call = one LiveKit room. Both processes derive the SAME
trace/session identifiers from the room name, so a control-plane request, the
worker's pipeline spans, and the Langfuse session line up without sharing any
state: the room name IS the correlation key.

Raw PHI never goes into span attributes; tenant/call UUIDs are fine.
"""

from typing import NamedTuple
from uuid import UUID

_PREFIX = "call"
_SEP = "--"


class RoomRef(NamedTuple):
    tenant_id: UUID
    call_id: UUID


def room_name_for_call(tenant_id: UUID, call_id: UUID) -> str:
    """Canonical room name: call--<tenant uuid>--<call uuid>."""
    return f"{_PREFIX}{_SEP}{tenant_id}{_SEP}{call_id}"


def parse_room_name(room_name: str) -> RoomRef | None:
    """Inverse of room_name_for_call; None for foreign/non-call rooms."""
    parts = room_name.split(_SEP)
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    try:
        return RoomRef(tenant_id=UUID(parts[1]), call_id=UUID(parts[2]))
    except ValueError:
        return None


def call_trace_attributes(room_name: str) -> dict[str, str]:
    """Span/trace attributes shared by every span belonging to this call.
    langfuse.session.id groups all of a call's traces into one Langfuse
    session view."""
    ref = parse_room_name(room_name)
    if ref is None:
        return {"vera.room": room_name}
    return {
        "vera.room": room_name,
        "vera.tenant_id": str(ref.tenant_id),
        "vera.call_id": str(ref.call_id),
        "langfuse.session.id": room_name,
    }
