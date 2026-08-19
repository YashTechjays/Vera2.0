"""Room-name correlation.

One verification call = one LiveKit room. Both processes derive the same
trace/session identifiers from the room name, so the room name is the correlation
key and no shared state is needed. Raw PHI never goes into span attributes.
"""

from typing import NamedTuple
from uuid import UUID

_PREFIX = "call"
_SEP = "--"

# Voice-session participant identities — a cross-process vocabulary the control
# plane mints and the agent worker reads. Observers (monitor, supervisor) are
# invisible for speaker resolution, so an observer leaving never ends the call.
CALLER_IDENTITY_PREFIX = "caller-"  # browser participant that publishes its mic
MONITOR_IDENTITY_PREFIX = "monitor-"  # listen-only browser observer (voice-lab outbound)
SUPERVISOR_IDENTITY_PREFIX = "supervisor-"  # /calls listen-only browser observer
SIP_CALLEE_IDENTITY = "phone-callee"  # outbound phone callee dialed in via SIP

# Participant-attribute key carrying a supervisor's join mode, stamped into the
# join token by the control plane and mirrored by the frontend.
PARTICIPANT_MODE_ATTR = "vera.mode"
PARTICIPANT_MODE_LISTENER = "listener"
PARTICIPANT_MODE_INTERVENER = "intervener"

# Span attribute naming how the callee reached the room, stamped by both processes so a
# browser-callee test call is never read as a real payer call in Langfuse.
TRANSPORT_ATTR = "vera.transport"
TRANSPORT_SIP = "sip"
TRANSPORT_BROWSER = "browser"

_OBSERVER_PREFIXES = (MONITOR_IDENTITY_PREFIX, SUPERVISOR_IDENTITY_PREFIX)

_SESSION_SEP = "~"  # supervisor identity: user id ~ session id

# Langfuse names a trace after its ROOT span, but it also re-derives the trace from
# EVERY span carrying a trace-level attribute — `langfuse.session.id` below is one —
# and on that path it reads the name from `langfuse.trace.name` alone. A child span
# that omits it therefore writes the trace's name as empty, and a call emits hundreds
# of them, so the trace reliably ends up unnamed. Spans inside the call's trace
# restate the name instead of leaving it to whichever write lands last.
TRACE_NAME_ATTR = "langfuse.trace.name"
CALL_TRACE_NAME = "job_entrypoint"  # = LiveKit's own root span name; keep them equal


def is_observer_identity(identity: str) -> bool:
    """True for a participant that observes the call (monitor, supervisor) rather
    than being its speaker. Even a supervisor publishing audio stays an observer;
    the call's speaker is always the callee."""
    return identity.startswith(_OBSERVER_PREFIXES)


def supervisor_identity(user_id: UUID, session_id: UUID) -> str:
    """LiveKit participant identity for a supervisor watching a call — one per browser
    session, because LiveKit allows a single participant per identity and force-
    disconnects the incumbent when a duplicate joins (a per-user identity made one
    supervisor's second browser evict their first). Stable within a session, so that
    browser's own reconnect still evicts its stale participant."""
    return f"{SUPERVISOR_IDENTITY_PREFIX}{user_id}{_SESSION_SEP}{session_id}"


def supervisor_user_id(identity: str) -> UUID | None:
    """The user id from an identity built by supervisor_identity, or None if *identity*
    isn't a supervisor's, or is malformed. Session-less identities still parse."""
    if not identity.startswith(SUPERVISOR_IDENTITY_PREFIX):
        return None
    try:
        return UUID(identity[len(SUPERVISOR_IDENTITY_PREFIX) :].split(_SESSION_SEP, 1)[0])
    except ValueError:
        return None


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


def call_trace_attributes(room_name: str, *, in_call_trace: bool) -> dict[str, str]:
    """Span/trace attributes shared by every span belonging to this call.
    langfuse.session.id groups all of a call's traces into one session view.

    Pass *in_call_trace* True only for a span that lands in the trace the worker rooted,
    which restates that trace's name (see TRACE_NAME_ATTR). False is for a span rooting
    a trace of its own — the dispatch span, or a post-call span whose trace link
    expired — where claiming the worker's name would disguise a broken link as a
    healthy call trace.
    """
    ref = parse_room_name(room_name)
    if ref is None:
        return {"vera.room": room_name}
    attributes = {
        "vera.room": room_name,
        "vera.tenant_id": str(ref.tenant_id),
        "vera.call_id": str(ref.call_id),
        "langfuse.session.id": room_name,
    }
    if in_call_trace:
        attributes[TRACE_NAME_ATTR] = CALL_TRACE_NAME
    return attributes
