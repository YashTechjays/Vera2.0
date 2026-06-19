from vera_core.db import TENANT_GUC, rls_policy_ddl
from vera_core.db.rls import catalog_rls_policy_ddl, drop_rls_policy_ddl


def test_policy_ddl_enables_and_forces_rls() -> None:
    ddl = rls_policy_ddl("app_user")
    assert ddl[0] == "ALTER TABLE app_user ENABLE ROW LEVEL SECURITY"
    assert ddl[1] == "ALTER TABLE app_user FORCE ROW LEVEL SECURITY"


def test_policy_keys_on_tenant_guc_and_fails_closed() -> None:
    policy = rls_policy_ddl("audit_log")[2]
    # `true` -> current_setting returns NULL (not an error) when unset: zero rows.
    assert f"current_setting('{TENANT_GUC}', true)::uuid" in policy
    assert "WITH CHECK" in policy  # writes are constrained too, not just reads


def test_custom_tenant_column() -> None:
    policy = rls_policy_ddl("tenant", tenant_column="id")[2]
    assert "id = current_setting" in policy


def test_drop_ddl_is_inverse() -> None:
    ddl = drop_rls_policy_ddl("audit_log")
    assert ddl[0].startswith("DROP POLICY IF EXISTS audit_log_tenant_isolation")


def test_catalog_policy_reads_global_rows_but_writes_strict() -> None:
    ddl = catalog_rls_policy_ddl("role")
    assert ddl[0] == "ALTER TABLE role ENABLE ROW LEVEL SECURITY"
    assert ddl[1] == "ALTER TABLE role FORCE ROW LEVEL SECURITY"
    policy = ddl[2]
    strict = f"tenant_id = current_setting('{TENANT_GUC}', true)::uuid"
    # USING also matches global (NULL-tenant) catalog rows.
    assert f"USING ({strict} OR tenant_id IS NULL)" in policy
    # WITH CHECK stays strict equality — no NULL leniency on writes.
    assert f"WITH CHECK ({strict})" in policy
    assert "OR tenant_id IS NULL)" not in policy.split("WITH CHECK")[1]
    # Shared policy name so drop_rls_policy_ddl("role") removes it.
    assert "CREATE POLICY role_tenant_isolation ON role" in policy
