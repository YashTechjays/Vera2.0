"""initial v2 schema — full Vera 2.0 reconciled ERD, with RLS

Revision ID: 0001
Revises:
Create Date: 2026-06-15

Greenfield build (adr/vera2-database-design.md). The table DDL is materialized
from `Base.metadata` so the migration can never drift from the models — every
column, FK, CHECK, unique constraint, partial unique index, trigram index and the
§7 hot composites come straight from the mapped classes. This migration then
layers the security posture that models can't express:

- Row-Level Security on every tenant-scoped table (keyed on the per-transaction
  GUC `app.tenant_id`, fail-closed when unset) — the table list is derived from
  the metadata (any table carrying `tenant_id`), so new tenant tables join the
  policy automatically.
- `tenant` itself is keyed on `id` (a request sees only its own tenant row).
- `audit_log` and `auth_audit_log` get SELECT/INSERT-only policies under FORCE
  RLS — the absence of UPDATE/DELETE policies makes them immutable WORM logs for
  every non-BYPASSRLS connection, owner included.

PHI posture is plaintext-under-CMEK for Phase 1 (PHI columns carry `info=PHI_INFO`
in the models); application-level column encryption is a later retrofit, not here.
"""

from collections.abc import Sequence

from alembic import op

import vera_core.models  # noqa: F401 — registers every table on Base.metadata
from vera_core.db import Base
from vera_core.db.rls import catalog_rls_policy_ddl, drop_rls_policy_ddl, rls_policy_ddl

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# WORM audit logs — immutable SELECT/INSERT-only policies, not the generic one.
WORM_TABLES = ("audit_log", "auth_audit_log")

# Shared-catalog tables: tenant rows PLUS global (NULL-tenant) platform rows are
# readable, so they get the lenient catalog policy, not the strict uniform one.
CATALOG_TABLES = ("role", "role_permission")


def _tenant_scoped_tables() -> list[str]:
    """Tables that carry `tenant_id` and so join the standard RLS policy —
    excluding `tenant` (keyed on `id`), the WORM logs and the shared-catalog
    tables (which get special policies). `tenant_elevation` is excluded
    automatically because it no longer carries `tenant_id` (platform-governance
    table keyed on `target_tenant_id`)."""
    return [
        name
        for name, table in Base.metadata.tables.items()
        if "tenant_id" in table.columns and name not in WORM_TABLES and name not in CATALOG_TABLES
    ]


def _worm_policies(table: str, guc: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_tenant_select ON {table} FOR SELECT USING (tenant_id = {guc})",
        f"CREATE POLICY {table}_tenant_insert ON {table} FOR INSERT WITH CHECK (tenant_id = {guc})",
    ]


def upgrade() -> None:
    bind = op.get_bind()
    # pgvector (embeddings) + pg_trgm (patient_name fuzzy search) — must exist
    # before create_all builds the trigram GIN index.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    Base.metadata.create_all(bind=bind)

    # --- Row-Level Security -------------------------------------------------
    # tenant: a request sees only its own tenant row (keyed on id).
    for stmt in rls_policy_ddl("tenant", tenant_column="id"):
        op.execute(stmt)

    # Every tenant-scoped table: the standard tenant-isolation policy.
    for table in _tenant_scoped_tables():
        for stmt in rls_policy_ddl(table):
            op.execute(stmt)

    # Shared-catalog tables: tenant rows + global (NULL-tenant) rows are readable,
    # writes stay strictly tenant-scoped (ADR §3.5.9).
    for table in CATALOG_TABLES:
        for stmt in catalog_rls_policy_ddl(table):
            op.execute(stmt)

    # tenant_elevation: platform-governance table. An elevated session (whose GUC
    # equals the target tenant) reads its own grant; SELECT only. Grant creation
    # and the platform "all active elevations" oversight read are the deferred
    # platform runtime (ADR §3.5.9 / §3.5.4) and will land with a SECURITY DEFINER
    # function — no insert/update/delete/platform-read policy here.
    op.execute("ALTER TABLE tenant_elevation ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_elevation FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_elevation_elevated_read ON tenant_elevation "
        "FOR SELECT USING "
        "(target_tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    # WORM audit logs: SELECT + INSERT only => immutable under FORCE RLS.
    guc = "current_setting('app.tenant_id', true)::uuid"
    for table in WORM_TABLES:
        for stmt in _worm_policies(table, guc):
            op.execute(stmt)


def downgrade() -> None:
    for table in WORM_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_select ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # tenant_elevation bespoke policy.
    op.execute("DROP POLICY IF EXISTS tenant_elevation_elevated_read ON tenant_elevation")
    op.execute("ALTER TABLE tenant_elevation NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_elevation DISABLE ROW LEVEL SECURITY")

    # Shared-catalog tables: the policy name is shared with the strict policy,
    # so drop_rls_policy_ddl drops the catalog policy too.
    for table in CATALOG_TABLES:
        for stmt in drop_rls_policy_ddl(table):
            op.execute(stmt)

    for table in _tenant_scoped_tables():
        for stmt in drop_rls_policy_ddl(table):
            op.execute(stmt)
    for stmt in drop_rls_policy_ddl("tenant"):
        op.execute(stmt)

    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP TYPE IF EXISTS actor_type")
