"""Unit-level regression for `POST /patient-forms/{id}/export`'s loader wiring.

The real defect: the endpoint passed `authoritative_calls` to only one of
`load_call_attempts` / `load_field_provenance`; the other kept the loaders'
dataclass default (`authoritative=True`), so every row on that side claimed
payer-side proof regardless of the actual call (spec E7). The only existing
coverage is `tests/integration/control_plane/test_call_authoritative.py::
test_export_reports_a_call_that_captured_no_reference_number`, which needs a
live Postgres and cannot run green in this environment's shared-DB contention.
This test guards the same call-site wiring without a database, following the
app-with-overrides seam `tests/unit/http/test_recording_playback.py` already
uses: the two loaders are monkeypatched to capture the kwarg they receive.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Collection, Mapping
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from control_plane.api.v1 import patient_forms as patient_forms_mod
from control_plane.api.v1.patient_forms import router
from control_plane.auth.identity import VerifiedIdentity
from control_plane.deps import current_identity, current_tenant_id, tenant_scoped_session
from control_plane.exceptions import register_exception_handlers
from control_plane.request_context import RequestIdMiddleware
from vera_core.audit import AuditRecord
from vera_core.forms.dsl import PromotedFields
from vera_core.models import PatientForm, SchemaVersion
from vera_core.models.enums import AccountType, FormStatus
from vera_core.services.call_provenance import CallAttempt, FieldProvenance

_TENANT_ID = uuid4()
_USER_ID = uuid4()
_FORM_ID = uuid4()
_VERSION_ID = uuid4()
_AUTHORITATIVE_CALL_ID = uuid4()

_IDENTITY = VerifiedIdentity(
    user_id=_USER_ID,
    subject="reviewer@example.com",
    email="reviewer@example.com",
    tenant_id=_TENANT_ID,
    account_type=AccountType.TENANT,
    session_id=uuid4(),
)

# A minimal valid v2 document (same shape as test_export_workbook.py's `V2`) — just
# enough to satisfy the DSL validator so `_v2_doc` returns a real document and the
# endpoint's authoritative-lookup branch actually runs.
_V2_SCHEMA: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    "system_fields": {"network_status": "sections.patient_information.network_status"},
    "rep_call_reference_number_field": "sections.patient_information.network_status",
    "promoted_fields": dict.fromkeys(
        PromotedFields.model_fields, "sections.patient_information.network_status"
    ),
    "sections": {
        "patient_information": {
            "title": "Patient Information",
            "role": "collect",
            "fields": {
                "network_status": {
                    "type": "text",
                    "title": "Network status",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is the network status?"},
                },
            },
        },
    },
    "tasks": [{"task_key": "t1", "title": "Task 1", "sections": ["patient_information"]}],
}


class _FakeResult:
    """`.scalar_one()` / `.scalar_one_or_none()` over one queued row."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Answers the endpoint's two entity selects (the form, then its schema version)
    in order. Every field-answer reader the endpoint calls after that is monkeypatched
    out below, so no other `execute` call reaches this fake."""

    def __init__(self, form: PatientForm, version: SchemaVersion) -> None:
        self._queue: list[object] = [form, version]

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._queue.pop(0))

    def add(self, _obj: object) -> None:
        pass

    async def flush(self) -> None:
        pass


class _FakeResolver:
    def __init__(self, permissions: frozenset[str]) -> None:
        self._permissions = permissions

    async def effective_permissions(
        self, _session: object, _tenant_id: UUID | None, user_id: UUID
    ) -> tuple[UUID, frozenset[str]]:
        return user_id, self._permissions


class _SpyAudit:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


def _build_app(session: _FakeSession) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)
    app.state.permission_resolver = _FakeResolver(frozenset({"forms:export"}))
    app.state.audit = _SpyAudit()

    async def _identity() -> VerifiedIdentity:
        return _IDENTITY

    async def _tenant_id() -> UUID:
        return _TENANT_ID

    async def _session() -> AsyncGenerator[Any, None]:
        yield session

    app.dependency_overrides[current_identity] = _identity
    app.dependency_overrides[current_tenant_id] = _tenant_id
    app.dependency_overrides[tenant_scoped_session] = _session
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_export_passes_the_same_authoritative_calls_to_both_loaders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the real defect: `_authoritative` reads a `None` set as "every call
    is authoritative" (`call_provenance.py`), so a call site that forgot to forward the
    resolved set would silently pass. Both loaders must receive the SAME concrete set the
    endpoint resolved — not None, and not two different objects."""
    captured: dict[str, Collection[UUID] | None] = {}

    async def _fake_current_values(_session: object, _form_id: UUID) -> dict[str, Any]:
        return {}

    async def _fake_load_field_status(_session: object, _form_id: UUID) -> dict[str, Any]:
        return {}

    async def _fake_load_authoritative_call_ids(
        _session: object, _form_id: UUID, *, reference_field: str
    ) -> frozenset[UUID]:
        return frozenset({_AUTHORITATIVE_CALL_ID})

    async def _fake_load_call_attempts(
        _session: object, _form_id: UUID, *, authoritative_calls: Collection[UUID] | None = None
    ) -> list[CallAttempt]:
        captured["call_attempts"] = authoritative_calls
        return []

    async def _fake_load_field_provenance(
        _session: object,
        _form_id: UUID,
        _attempt_by_call: Mapping[UUID, tuple[int, str]],
        *,
        authoritative_calls: Collection[UUID] | None = None,
    ) -> dict[str, FieldProvenance]:
        captured["field_provenance"] = authoritative_calls
        return {}

    monkeypatch.setattr(patient_forms_mod, "current_values_by_path", _fake_current_values)
    monkeypatch.setattr(patient_forms_mod, "load_field_status", _fake_load_field_status)
    monkeypatch.setattr(
        patient_forms_mod, "load_authoritative_call_ids", _fake_load_authoritative_call_ids
    )
    monkeypatch.setattr(patient_forms_mod, "load_call_attempts", _fake_load_call_attempts)
    monkeypatch.setattr(patient_forms_mod, "load_field_provenance", _fake_load_field_provenance)

    form = PatientForm(
        id=_FORM_ID,
        tenant_id=_TENANT_ID,
        schema_version_id=_VERSION_ID,
        status=FormStatus.COMPLETED.value,
        completion_pct=100.0,
        retry_count=0,
    )
    version = SchemaVersion(id=_VERSION_ID, schema_json=_V2_SCHEMA)
    app = _build_app(_FakeSession(form, version))

    async with _client(app) as c:
        resp = await c.post(f"/api/v1/patient-forms/{_FORM_ID}/export")

    assert resp.status_code == 200, resp.text
    assert captured["call_attempts"] is not None
    assert captured["field_provenance"] is not None
    expected = frozenset({_AUTHORITATIVE_CALL_ID})
    assert captured["call_attempts"] == expected
    assert captured["field_provenance"] == expected
