"""Call-content visibility (call-recording-persistence spec decision 6, amended VR2-177).

`call_hidden_from` is the LIVE-call visibility predicate; `call_content_visible`
adds the VR2-177 rule that a finished call's transcript/recording is tenant-visible;
`recording_playable` layers the two other recording gates (AVAILABLE +
`recordings:read`) on top so every call list computes `recording_available` the
same way and can never advertise a recording the playback endpoint would refuse.
Consumers: the playback endpoint's 404 gate (`api/v1/calls.py::get_recording_playback`),
the events/summary read gate (`_authorize_call_read`), and both call-list DTOs — the
per-form timeline (`api/v1/patient_forms.py`) and the tenant-wide history
(`api/v1/calls.py::list_call_history`).

The live rule's SQL twin is `call_authz.visible_to` — a change to `call_hidden_from`
must be mirrored there. The content rule needs no SQL twin: the only terminal
list enumeration (`list_calls` scope="history") selects terminal rows, where the
content rule is unconditionally true, so it applies no visibility WHERE at all.
"""

from uuid import UUID

from vera_core.models.call import TERMINAL_CALL_STATUS_VALUES


def call_hidden_from(initiated_by_id: UUID | None, published: bool, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see the call while it is LIVE (the caller maps
    this to the same 404 as a missing row, so a private call is never revealed by
    enumeration). A non-owner sees it only when it is published or ownerless."""
    if initiated_by_id == user_id:
        return False
    return initiated_by_id is not None and not published


def call_content_visible(
    initiated_by_id: UUID | None, published: bool, user_id: UUID | None, *, status: str
) -> bool:
    """Whether *user_id* may read the call's content (transcript, recording,
    events, summary) — the single rule behind every read surface (VR2-177):
    a finished call belongs to the tenant; a live one belongs to its owner
    until published."""
    if status in TERMINAL_CALL_STATUS_VALUES:
        return True
    return not call_hidden_from(initiated_by_id, published, user_id)


def recording_playable(
    *,
    has_recording: bool,
    initiated_by_id: UUID | None,
    published: bool,
    user_id: UUID | None,
    can_play: bool,
    status: str,
) -> bool:
    """Whether *user_id* may actually play the call's recording — the single source
    of the `recording_available` DTO flag across every call-list view.

    The invariant a list must uphold ("never advertise a recording the playback
    endpoint would refuse", `api/v1/calls.py::get_recording_playback`) is the
    conjunction of all three gates: an AVAILABLE recording exists, the call's
    content is visible to the caller, and the caller holds `recordings:read`
    (`can_play`). Keeping it here means a future authz change lands in one
    place, not three."""
    visible = call_content_visible(initiated_by_id, published, user_id, status=status)
    return has_recording and can_play and visible
