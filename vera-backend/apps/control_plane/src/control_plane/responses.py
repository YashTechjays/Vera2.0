"""The single response envelope every endpoint returns (API Contract §7.1).

Clients branch on the application-level `status`/`error_code`, not on the HTTP
status alone. Success payloads ride under `data` in a `ResponseModel[T]`; failures
are serialized into `ErrorResponse` by the handlers in `exceptions.py`.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

DEFAULT_SUCCESS_MESSAGE = "Operation completed successfully."


class ResponseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"


class ResponseModel[T](BaseModel):
    """Success envelope. `error_code`/`description` are always null on success."""

    data: T | None = None
    status: ResponseStatus = ResponseStatus.SUCCESS
    message: str = DEFAULT_SUCCESS_MESSAGE
    error_code: str | None = None
    description: str | None = None


class ErrorResponse(BaseModel):
    """Failure envelope. `data` may carry extra context (e.g. offending field
    paths) on some errors; it is null otherwise."""

    data: Any = None
    status: ResponseStatus = ResponseStatus.FAIL
    message: str
    error_code: str
    description: str | None = None


def ok[T](data: T, message: str = DEFAULT_SUCCESS_MESSAGE) -> ResponseModel[T]:
    """Build a SUCCESS envelope around `data` (status defaults to SUCCESS)."""
    return ResponseModel[T](data=data, message=message)
