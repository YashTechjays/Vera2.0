"""X-Request-Id correlation (API Contract §7.1).

The *client* is the primary generator — the console/frontend mints one id per HTTP
request and sends it; the server only generates one when a caller omits it (Twilio
webhooks, cron, internal service calls), so the audit log and cross-service traces
always have a correlation id. One HTTP request = one id; correlating a *retry* to
the original operation is `Idempotency-Key`'s job, not this header's.

The id is stored on `request.state` (authoritative within a request) and mirrored
into a ContextVar for code paths that don't hold the request. Every response — both
the success envelope and the error envelopes from `exceptions.py` — echoes it.
"""

from contextvars import ContextVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from vera_core.db import uuid7

REQUEST_ID_HEADER = "X-Request-Id"

_request_id: ContextVar[str] = ContextVar("vera_request_id", default="")


def current_request_id(request: Request | None = None) -> str:
    """The correlation id for the current request. Prefers `request.state` (always
    set by the middleware) and falls back to the ContextVar."""
    if request is not None:
        rid: str = getattr(request.state, "request_id", "")
        if rid:
            return rid
    return _request_id.get()


def _new_request_id() -> str:
    return str(uuid7())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Resolve X-Request-Id (incoming or generated), bind it for the request, and
    echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or _new_request_id()
        request.state.request_id = rid
        token = _request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
