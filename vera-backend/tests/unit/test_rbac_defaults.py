from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import (
    ALL_PERMISSIONS,
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
    assert "calls:intervene" in SYSTEM_ROLES["SUPER_ADMIN"]
    assert "calls:intervene" in SYSTEM_ROLES["SUPERVISOR"]
    assert "calls:intervene" in SYSTEM_ROLES["TENANT_ADMIN"]
    # VR2-77: VAs intervene on published ("Visible to All") calls.
    assert "calls:intervene" in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]


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


def test_llm_config_permissions_are_catalogued_and_super_admin_only() -> None:
    for code in ("platform:llm_config:read", "platform:llm_config:write"):
        assert code in PLATFORM_PERMISSIONS
        assert code in SYSTEM_ROLES["SUPER_ADMIN"]
        assert code not in SYSTEM_ROLES["TENANT_ADMIN"]


def test_virtual_assistant_has_live_monitoring_and_data_management_access() -> None:
    for code in ("calls:read", "calls:publish", "forms:read", "forms:write"):
        assert code in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]


def test_virtual_assistant_has_forms_export_and_recordings_read() -> None:
    for code in ("forms:export", "recordings:read"):
        assert code in DEFAULT_PERMISSIONS
        assert code in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]


def test_call_audit_events_exist() -> None:
    assert AuditEvent.CALL_PUBLISH.value == "call.publish"
    assert AuditEvent.CALL_LISTEN_ONLY_JOIN.value == "call.listen-only.join"
    # Publish-capable joins; the full intervention feature is still TODO.
    assert AuditEvent.CALL_INTERVENE_JOIN.value == "call.intervene.join"
    assert AuditEvent.CALL_END.value == "call.end"


def test_recordings_permissions_seeded() -> None:
    assert "recordings:read" in DEFAULT_PERMISSIONS
    assert "recordings:manage" in DEFAULT_PERMISSIONS
    assert "recordings:read" in SYSTEM_ROLES["SUPERVISOR"]
    assert "recordings:manage" not in SYSTEM_ROLES["SUPERVISOR"]
    assert "recordings:read" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "recordings:manage" in SYSTEM_ROLES["TENANT_ADMIN"]


def test_platform_users_permissions_are_seeded() -> None:
    assert "platform:users:invite" in PLATFORM_PERMISSIONS
    assert "platform:users:read" in PLATFORM_PERMISSIONS
    assert "platform:users:invite" in ALL_PERMISSIONS
    assert "platform:users:read" in ALL_PERMISSIONS


def test_super_admin_holds_the_new_platform_permissions() -> None:
    assert "platform:users:invite" in SYSTEM_ROLES["SUPER_ADMIN"]
    assert "platform:users:read" in SYSTEM_ROLES["SUPER_ADMIN"]


def test_platform_users_permissions_are_catalogued_and_super_admin_only() -> None:
    for code in ("platform:users:invite", "platform:users:read"):
        assert code in PLATFORM_PERMISSIONS
        # Platform perms go only to SUPER_ADMIN (never a tenant role).
        assert code in SYSTEM_ROLES["SUPER_ADMIN"]
        assert code not in SYSTEM_ROLES["TENANT_ADMIN"]


def test_reports_dashboard_permission_seeded() -> None:
    assert "reports:dashboard" in DEFAULT_PERMISSIONS
    assert "reports:dashboard" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "reports:dashboard" in SYSTEM_ROLES["SUPERVISOR"]
    # VAs reach Live Monitoring via calls:read; the dashboard is an explicit grant.
    assert "reports:dashboard" not in SYSTEM_ROLES["VIRTUAL_ASSISTANT"]
