"""Telephony-seam error types, shared by the control-plane LiveKit gateway (raiser)
and the vera_core queue dispatcher (catcher). vera_core must not import control_plane,
so the exception types live here."""


class LiveKitUnavailable(Exception):
    """The LiveKit SIP service could not be reached (or errored) while we probed it —
    e.g. verifying a trunk id exists before storing the credential. Distinct from
    "trunk not found": this means we could not get an answer, so we fail closed."""


class OutboundDialError(Exception):
    """Placing an outbound SIP call failed at the LiveKit / telephony seam — a
    bad/deleted trunk, the provider rejecting the call, or LiveKit being unreachable."""
