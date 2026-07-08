from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import DEFAULT_PERMISSIONS, SYSTEM_ROLES


def test_calls_publish_permission_is_catalogued_and_granted() -> None:
    assert "calls:publish" in DEFAULT_PERMISSIONS
    # Supervisor is granted publish explicitly; Tenant Admin holds all DEFAULT_PERMISSIONS.
    assert "calls:publish" in SYSTEM_ROLES["SUPERVISOR"]
    assert "calls:publish" in SYSTEM_ROLES["TENANT_ADMIN"]


def test_call_audit_events_exist() -> None:
    assert AuditEvent.CALL_PUBLISH.value == "call.publish"
    assert AuditEvent.CALL_INTERVENE_JOIN.value == "call.intervene.join"
    assert AuditEvent.CALL_INTERVENE_REVOKE.value == "call.intervene.revoke"
