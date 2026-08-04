"""Telephony-seam error types, shared by the control-plane LiveKit gateway (raiser)
and the vera_core queue dispatcher (catcher). vera_core must not import control_plane,
so the exception types live here."""


class TelephonyError(Exception):
    """Base for LiveKit-seam failures, carrying a PHI-safe diagnostic tag.

    `code`/`status` are LiveKit's own rejection (`not_found`, `resource_exhausted`, …
    plus an HTTP status) — a fixed server-authored vocabulary, safe to log. The SDK
    error's `message`/`metadata` are not: they can echo the request body, which
    carries `agent_context` and the dialed number. Raisers pass a developer-authored
    `detail` only, which is what makes `str(exc)` safe here too.
    """

    def __init__(
        self, detail: str = "", *, code: str | None = None, status: int | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status

    @property
    def diagnostic(self) -> str:
        """PHI-safe tag naming the rejection, for logs and spans.

        A LiveKit rejection renders `code=… status=…`; a failure that never reached
        LiveKit renders `transport=…` — an operator has to be able to tell a stale
        trunk from an unreachable SIP service at a glance.
        """
        if self.code is None:
            return "unknown"
        return f"code={self.code} status={self.status}" if self.status else f"transport={self.code}"

    def __str__(self) -> str:
        return f"{self.detail} ({self.diagnostic})" if self.code else self.detail


class LiveKitUnavailable(TelephonyError):
    """The LiveKit SIP service could not be reached (or errored) while we probed it —
    e.g. verifying a trunk id exists before storing the credential. Distinct from
    "trunk not found": this means we could not get an answer, so we fail closed."""


class OutboundDialError(TelephonyError):
    """Placing an outbound SIP call failed at the LiveKit / telephony seam — a
    bad/deleted trunk, the provider rejecting the call, or LiveKit being unreachable."""
