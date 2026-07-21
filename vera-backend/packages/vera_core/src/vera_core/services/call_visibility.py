"""Owner-or-published call visibility (call-recording-persistence spec decision 6).

One predicate, two consumers — the playback endpoint's 404 gate
(`api/v1/calls.py::_call_hidden_from`) and the call-attempt DTO's `recording`
enrichment (`api/v1/patient_forms.py`) — so the gates can never diverge.
"""

from uuid import UUID


def call_hidden_from(initiated_by_id: UUID | None, published: bool, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see the call (the caller maps this to the same
    404 as a missing row, so a private call is never revealed by enumeration).
    A non-owner sees it only when it is published or ownerless."""
    if initiated_by_id == user_id:
        return False
    return initiated_by_id is not None and not published
