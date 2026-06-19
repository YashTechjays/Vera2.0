"""Shared fakes for isolated auth-unit tests.

These let us exercise tenant_guard and require() without a database or a live
FastAPI request: a SpyAudit captures emitted AuditRecords, and make_request
builds the minimal Request shape the code reads (app.state.audit, url.path,
headers).
"""

from types import SimpleNamespace
from typing import Any

import pytest

from vera_core.audit import AuditRecord


class SpyAudit:
    """AuditSink that records every emitted record for inspection."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


@pytest.fixture
def spy_audit() -> SpyAudit:
    return SpyAudit()


def make_request(
    audit: SpyAudit,
    *,
    path: str = "/api/v1/calls",
    request_id: str = "req-1",
) -> Any:
    """Minimal stand-in for a Starlette Request: only the attributes the auth
    chain actually touches. `state` is the per-request bag (always present on a
    real Request) the elevation memoization reads/writes."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(audit=audit)),
        url=SimpleNamespace(path=path),
        headers={"x-request-id": request_id},
        state=SimpleNamespace(),
    )
