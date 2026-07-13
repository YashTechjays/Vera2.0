from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import (
    DEFAULT_PERMISSIONS,
    PLATFORM_PERMISSIONS,
    SYSTEM_ROLES,
)


def test_calls_publish_permission_is_catalogued_and_granted() -> None:
    assert "calls:publish" in DEFAULT_PERMISSIONS
    # Supervisor is granted publish explicitly; Tenant Admin holds all DEFAULT_PERMISSIONS.
    assert "calls:publish" in SYSTEM_ROLES["SUPERVISOR"]
    assert "calls:publish" in SYSTEM_ROLES["TENANT_ADMIN"]


def test_calls_intervene_permission_is_catalogued_and_granted() -> None:
    assert "calls:intervene" in DEFAULT_PERMISSIONS
    assert "calls:intervene" in SYSTEM_ROLES["SUPERVISOR"]
    assert "calls:intervene" in SYSTEM_ROLES["TENANT_ADMIN"]
    # A VA can watch nothing and speak into nothing — sandbox only.
    assert "calls:intervene" not in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]


def test_insurance_provider_permissions_are_catalogued_and_super_admin_only() -> None:
    for code in ("platform:insurance_providers:read", "platform:insurance_providers:write"):
        assert code in PLATFORM_PERMISSIONS
        # Platform perms go only to SUPER_ADMIN (never a tenant role).
        assert code in SYSTEM_ROLES["SUPER_ADMIN"]
        assert code not in SYSTEM_ROLES["TENANT_ADMIN"]


def test_form_schemas_read_permission_is_catalogued_and_super_admin_only() -> None:
    assert "platform:form_schemas:read" in PLATFORM_PERMISSIONS
    assert "platform:form_schemas:read" in SYSTEM_ROLES["SUPER_ADMIN"]
    assert "platform:form_schemas:read" not in SYSTEM_ROLES["TENANT_ADMIN"]


def test_call_audit_events_exist() -> None:
    assert AuditEvent.CALL_PUBLISH.value == "call.publish"
    assert AuditEvent.CALL_INTERVENE_JOIN.value == "call.intervene.join"
    assert AuditEvent.CALL_END_SUPERVISOR.value == "call.end.supervisor"
