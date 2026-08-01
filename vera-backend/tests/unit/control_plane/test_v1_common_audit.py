"""Unit tests for `emit_phi_read_audit` (api/v1/common.py) — the single
construction point for a PHI-read `AuditRecord` shared by every display-path
endpoint (patient_forms, calls). Exercised without a database via SpyAudit.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from control_plane.api.v1.common import emit_phi_read_audit
from control_plane.auth.identity import VerifiedIdentity
from tests.unit.auth.conftest import SpyAudit, make_request
from vera_core.models.enums import AccountType

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
USER_ID = UUID("00000000-0000-0000-0000-0000000000cc")
ELEVATION_ID = UUID("00000000-0000-0000-0000-0000000000ee")


@pytest.fixture
def spy_audit() -> SpyAudit:
    return SpyAudit()


def _caller() -> VerifiedIdentity:
    return VerifiedIdentity(
        user_id=USER_ID,
        subject="a@example.com",
        email="a@example.com",
        tenant_id=TENANT,
        account_type=AccountType.TENANT,
        session_id=uuid4(),
    )


async def test_emit_phi_read_audit_sets_request_id_and_fields(spy_audit: SpyAudit) -> None:
    request = make_request(spy_audit)
    request.state.request_id = "req-1"  # current_request_id reads request.state, not the header

    await emit_phi_read_audit(
        spy_audit,
        request,
        tenant_id=TENANT,
        caller=_caller(),
        resource_type="patient_form",
        resource_id="list",
        fields=["patient_name", "chart_number"],
    )

    assert len(spy_audit.records) == 1
    record = spy_audit.records[0]
    assert record.event_type == "phi.access"
    assert record.tenant_id == TENANT
    assert record.actor_user_id == USER_ID
    assert record.resource_type == "patient_form"
    assert record.resource_id == "list"
    assert record.request_id == "req-1"
    assert record.detail == {"fields": ["patient_name", "chart_number"]}
    assert record.elevation_session_id is None  # no elevation on an ordinary tenant request


async def test_emit_phi_read_audit_links_an_elevated_session(spy_audit: SpyAudit) -> None:
    """A superadmin's elevated tenant access must be traceable from the PHI-read
    row back to the elevation grant — this is the gap the shared helper closes
    (the old per-endpoint AuditRecord constructions silently dropped it)."""
    request = make_request(spy_audit)
    request.state = SimpleNamespace(request_id="req-2", vera_elevation=ELEVATION_ID)

    await emit_phi_read_audit(
        spy_audit,
        request,
        tenant_id=TENANT,
        caller=_caller(),
        resource_type="patient_form",
        resource_id="list",
        fields=["patient_name"],
    )

    record = spy_audit.records[0]
    assert record.elevation_session_id == ELEVATION_ID
