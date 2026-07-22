from vera_core.models import ExportArtifact
from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import ALL_PERMISSIONS, SYSTEM_ROLES


def test_export_artifact_table_shape() -> None:
    cols = {c.name for c in ExportArtifact.__table__.columns}
    assert {
        "id",
        "tenant_id",
        "form_id",
        "format",
        "sha256",
        "gcs_uri",
        "exported_by",
        "created_at",
    } <= cols


def test_forms_export_permission_seeded_to_admin_and_supervisor() -> None:
    assert "forms:export" in ALL_PERMISSIONS
    assert "forms:export" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "forms:export" in SYSTEM_ROLES["SUPERVISOR"]


def test_form_exported_audit_event() -> None:
    assert AuditEvent.FORM_EXPORTED.value == "form.exported"
