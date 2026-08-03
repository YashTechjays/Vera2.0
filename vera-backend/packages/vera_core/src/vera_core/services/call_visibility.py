"""Owner-or-published call visibility (call-recording-persistence spec decision 6).

`call_hidden_from` is the visibility predicate; `recording_playable` layers the two
other recording gates (AVAILABLE + `recordings:read`) on top of it so every call
list computes `recording_available` the same way and can never advertise a
recording the playback endpoint would refuse. Consumers: the playback endpoint's
404 gate (`api/v1/calls.py::get_recording_playback`) and both call-list DTOs — the
per-form timeline (`api/v1/patient_forms.py`) and the tenant-wide history
(`api/v1/calls.py::list_call_history`).

The SAME visibility rule also exists in SQL form for list enumeration
(`api/v1/calls.py::list_calls`'s WHERE clause) — a change here must be mirrored
there; a Python predicate can't be pushed into that query.
"""

from uuid import UUID


def call_hidden_from(initiated_by_id: UUID | None, published: bool, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see the call (the caller maps this to the same
    404 as a missing row, so a private call is never revealed by enumeration).
    A non-owner sees it only when it is published or ownerless."""
    if initiated_by_id == user_id:
        return False
    return initiated_by_id is not None and not published


def recording_playable(
    *,
    has_recording: bool,
    initiated_by_id: UUID | None,
    published: bool,
    user_id: UUID | None,
    can_play: bool,
) -> bool:
    """Whether *user_id* may actually play the call's recording — the single source
    of the `recording_available` DTO flag across every call-list view.

    The invariant a list must uphold ("never advertise a recording the playback
    endpoint would refuse", `api/v1/calls.py::get_recording_playback`) is the
    conjunction of all three gates: an AVAILABLE recording exists, the call is
    visible to the caller, and the caller holds `recordings:read` (`can_play`).
    Keeping it here means a future authz change lands in one place, not three."""
    return has_recording and can_play and not call_hidden_from(initiated_by_id, published, user_id)
