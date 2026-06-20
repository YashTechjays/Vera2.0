"""Platform-operator login schema, checked at the metadata level (no DB):
`user_identity.tenant_id` is now NULLABLE (platform operators have no tenant),
and the new single-row `platform_login_provider` carries the provider toggle
columns plus the provider_type CHECK + UNIQUE (ADR-0006 §D)."""

from vera_core.models.auth import PlatformLoginProvider, UserIdentity
from vera_core.models.enums import ProviderKind, check_in


def test_user_identity_tenant_id_is_nullable() -> None:
    table = UserIdentity.metadata.tables["user_identity"]
    assert table.c.tenant_id.nullable


def test_platform_login_provider_columns() -> None:
    table = PlatformLoginProvider.metadata.tables["platform_login_provider"]
    assert {"provider_type", "display_name", "enabled", "enforce_mfa"} <= set(table.c.keys())
    assert table.c.tenant_id.nullable
    assert not table.c.provider_type.nullable
    assert not table.c.enabled.nullable
    assert not table.c.enforce_mfa.nullable


def test_platform_login_provider_has_check_and_unique() -> None:
    table = PlatformLoginProvider.metadata.tables["platform_login_provider"]
    names = {c.name for c in table.constraints}
    assert "ck_platform_login_provider_provider_type_valid" in names
    assert "uq_platform_login_provider_provider_type" in names


def test_provider_type_check_lists_password() -> None:
    constraint = check_in("provider_type", ProviderKind)
    assert "password" in str(constraint.sqltext)
