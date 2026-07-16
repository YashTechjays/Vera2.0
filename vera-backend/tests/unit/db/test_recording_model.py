"""Recording lifecycle columns + catalog values (Task 1 of call-recording plan)."""

from vera_core.models import Recording, Tenant
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import RecordingStatus, values_of


def test_recording_status_catalog() -> None:
    assert values_of(RecordingStatus) == (
        "pending",
        "available",
        "failed",
        "discarded",
        "deleted",
    )


def test_recording_lifecycle_columns_exist() -> None:
    cols = Recording.__table__.columns
    assert cols["status"].nullable is False
    for name in ("egress_id", "sha256", "size_bytes", "duration_ms", "deleted_at"):
        assert cols[name].nullable is True


def test_tenant_retention_knob_nullable() -> None:
    assert Tenant.__table__.columns["recording_retention_days"].nullable is True


def test_recording_audit_events_exist() -> None:
    assert AuditEvent.RECORDING_START_FAILED.value == "recording.start_failed"
    assert AuditEvent.RECORDING_FAILED.value == "recording.failed"
    assert AuditEvent.RECORDING_DISCARDED.value == "recording.discarded"
    assert AuditEvent.RECORDING_ACCESSED.value == "recording.accessed"
    assert AuditEvent.RECORDING_DELETED.value == "recording.deleted"
