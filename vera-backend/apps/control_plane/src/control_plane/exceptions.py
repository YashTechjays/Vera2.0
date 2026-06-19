"""Central error table + the global exception handlers (API Contract §7.1).

Every error_code maps to a fixed HTTP status here. Domain code raises a
`CustomAPIException` (or a thin subclass); a single set of handlers, registered by
`register_exception_handlers(app)`, serializes everything into the `ErrorResponse`
envelope. Routes document the errors they may raise via `CustomAPIResponse.custom(...)`.

PHI safety: handlers never serialize raw exception text or submitted request values
into a response (a request body may contain PHI). `message`/`description` are
developer-authored, non-PHI strings.
"""

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from control_plane.request_context import REQUEST_ID_HEADER, current_request_id
from control_plane.responses import ErrorResponse

logger = logging.getLogger("vera.control_plane")


class _ErrorCode(Enum):
    """Member-less Enum base carrying (http_status, message) per member, so both
    code enums share one interface. The machine `code` is the member name; each
    error_code maps to a fixed HTTP status (§7.1)."""

    def __init__(self, http_status: int, message: str) -> None:
        self._http_status = http_status
        self._message = message

    @property
    def code(self) -> str:
        return self.name

    @property
    def http_status(self) -> int:
        return self._http_status

    @property
    def default_message(self) -> str:
        return self._message


class DefaultExceptionCode(_ErrorCode):
    """Framework defaults from the §7.1 status table."""

    BAD_REQUEST = (400, "Bad request.")
    UNAUTHORIZED = (401, "Unauthorized.")
    FORBIDDEN = (403, "Forbidden.")
    NOT_FOUND = (404, "Resource not found.")
    CONFLICT = (409, "Conflict.")
    VALIDATION_ERROR = (422, "Validation error.")
    RATE_LIMIT_EXCEEDED = (429, "Rate limit exceeded.")
    INTERNAL_SERVER_ERROR = (500, "Internal server error.")


class ExceptionCode(_ErrorCode):
    """Application / auth codes. Domain features add their own codes here with the
    HTTP status their definition requires (§7.1 lists examples such as
    AGENT_ASSIGNMENT_FAILED, DUPLICATE_EXTERNAL_REF, …)."""

    INVALID_TOKEN = (401, "Invalid token")
    TOKEN_EXPIRED = (401, "Token expired")
    USER_NOT_FOUND = (404, "User not found")
    USER_ALREADY_EXISTS = (409, "User already exists")
    MISSING_IDEMPOTENCY_KEY = (400, "Idempotency-Key header is required")
    IDEMPOTENCY_CONFLICT = (409, "A request with this Idempotency-Key is in progress")


class CustomAPIException(Exception):
    """Raised by domain code; serialized to the `ErrorResponse` envelope by the
    global handler at `code.http_status`. May attach an optional `data` object to
    carry extra context, and optional response `headers` (e.g. WWW-Authenticate)."""

    def __init__(
        self,
        code: _ErrorCode,
        *,
        message: str | None = None,
        description: str | None = None,
        data: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message if message is not None else code.default_message
        self.description = description
        self.data = data
        self.headers = headers
        super().__init__(self.message)


class UnauthorizedError(CustomAPIException):
    def __init__(self, *, message: str | None = None, description: str | None = None) -> None:
        super().__init__(
            DefaultExceptionCode.UNAUTHORIZED,
            message=message,
            description=description,
            headers={"WWW-Authenticate": "Bearer"},
        )


class BadRequestError(CustomAPIException):
    def __init__(self, *, message: str | None = None, description: str | None = None) -> None:
        super().__init__(DefaultExceptionCode.BAD_REQUEST, message=message, description=description)


class NotFoundError(CustomAPIException):
    def __init__(self, *, message: str | None = None, description: str | None = None) -> None:
        super().__init__(DefaultExceptionCode.NOT_FOUND, message=message, description=description)


class CustomAPIResponse:
    """Builds the FastAPI `responses=` map that documents the error envelopes a
    route may emit, so they flow into OpenAPI alongside `response_model`."""

    @staticmethod
    def custom(*codes: _ErrorCode) -> dict[int | str, dict[str, Any]]:
        grouped: dict[int, list[str]] = {}
        for code in codes:
            grouped.setdefault(code.http_status, []).append(code.code)
        return {
            status_code: {"model": ErrorResponse, "description": ", ".join(names)}
            for status_code, names in grouped.items()
        }


# Map a bare Starlette HTTPException status onto a code, so any un-migrated
# `raise HTTPException(...)` (e.g. in deps.py) still produces the envelope. Derived
# from DefaultExceptionCode so the status->code mapping has a single source of truth
# and cannot drift (each default status is unique).
_CODE_BY_STATUS: dict[int, _ErrorCode] = {code.http_status: code for code in DefaultExceptionCode}


def _fail(
    request: Request,
    code: _ErrorCode,
    *,
    message: str | None = None,
    description: str | None = None,
    data: object | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    envelope = ErrorResponse(
        data=data,
        message=message if message is not None else code.default_message,
        error_code=code.code,
        description=description,
    )
    out_headers = dict(headers or {})
    out_headers[REQUEST_ID_HEADER] = current_request_id(request)
    return JSONResponse(
        status_code=code.http_status,
        content=envelope.model_dump(mode="json"),
        headers=out_headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CustomAPIException)
    async def _handle_custom(request: Request, exc: CustomAPIException) -> JSONResponse:
        return _fail(
            request,
            exc.code,
            message=exc.message,
            description=exc.description,
            data=exc.data,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths + reasons only — never the offending input value (may be PHI).
        fields = [
            {"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return _fail(
            request,
            DefaultExceptionCode.VALIDATION_ERROR,
            description="Request validation failed.",
            data={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, DefaultExceptionCode.INTERNAL_SERVER_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else None
        headers = exc.headers if isinstance(exc.headers, dict) else None
        return _fail(request, code, message=message, headers=headers)

    @app.exception_handler(Exception)
    async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log internally; never leak the exception text (PHI/secret safety).
        logger.exception("unhandled error on %s", request.url.path)
        return _fail(request, DefaultExceptionCode.INTERNAL_SERVER_ERROR)
