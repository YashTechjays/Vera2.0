"""platform-operator login — nullable identity, platform-readable RLS, seeded provider

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-21

Platform-operator login (ADR-0006 §D). A SUPER_ADMIN (`account_type='platform'`,
`tenant_id IS NULL`) logs in tenant-lessly with local password + mandatory TOTP. The
login flow runs pre-auth in a `platform_session` (`app.platform='on'`, no tenant GUC),
so the rows it reads must be visible under the platform-readable RLS policy:

- `user_identity.tenant_id` becomes NULLABLE — a platform operator's password identity
  carries `tenant_id IS NULL`. (No-op on a fresh DB: 0001's `create_all` already built it
  nullable from the changed model; the explicit DROP NOT NULL covers DBs already at 0010.)
- A new single-row `platform_login_provider` (the tenant-less analogue of `sso_provider`)
  is created and seeded with an enabled, MFA-enforced `password` provider. On a fresh DB
  0001's `create_all` already materialized the table from `Base.metadata`; on an existing
  DB it is absent, so `create(checkfirst=True)` adds it idempotently.
- Both tables swap their strict tenant-isolation policy (0001 / fresh `create_all`) for the
  platform-readable variant: a tenant session still sees only `tenant_id = GUC`; a platform
  session sees the global NULL-tenant rows. WITH CHECK stays strict equality — NULL-tenant
  rows are written only with privilege (this seed) or a SECURITY DEFINER path, never by an
  RLS-bound session.

The seed INSERT runs on the privileged migration connection (not RLS-bound), so the strict
WITH CHECK on a NULL-tenant row does not block it. Bootstrapping operator #1 (the first
SUPER_ADMIN's `app_user` + password `user_identity`) is a separate script, not this migration.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.db import Base
from vera_core.db.rls import platform_readable_rls_policy_ddl, rls_policy_ddl
from vera_core.models.enums import ProviderKind

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Identity/login tables whose global (NULL-tenant) rows a platform login must read.
PLATFORM_READABLE_TABLES = ("user_identity", "platform_login_provider")

_PASSWORD = ProviderKind.PASSWORD.value


def upgrade() -> None:
    bind = op.get_bind()

    # platform_login_provider is a new model: 0001's create_all built it on a fresh DB,
    # but it is absent on a DB already at 0010 — checkfirst makes this idempotent either way.
    Base.metadata.tables["platform_login_provider"].create(bind=bind, checkfirst=True)

    # Platform operators have no tenant, so their identity row is NULL-tenant.
    op.execute("ALTER TABLE user_identity ALTER COLUMN tenant_id DROP NOT NULL")

    # Swap strict isolation for the platform-readable policy so a platform_session resolves
    # the NULL-tenant login rows; tenant sessions are unchanged (strict equality).
    for table in PLATFORM_READABLE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        for stmt in platform_readable_rls_policy_ddl(table):
            op.execute(stmt)

    # Seed the single global password login provider (enabled, MFA enforced). created_at /
    # updated_at fall to their server_default now(); id is supplied (the model PK default is
    # client-side). ON CONFLICT keeps it idempotent against the provider_type UNIQUE.
    op.execute(
        "INSERT INTO platform_login_provider "
        "(id, tenant_id, provider_type, display_name, enabled, enforce_mfa) "
        f"VALUES (gen_random_uuid(), NULL, '{_PASSWORD}', 'Password', true, true) "
        "ON CONFLICT (provider_type) DO NOTHING"
    )


def downgrade() -> None:
    # Dropping the table also drops its policy; only user_identity needs an explicit revert.
    op.execute("DROP TABLE IF EXISTS platform_login_provider")

    op.execute("DROP POLICY IF EXISTS user_identity_tenant_isolation ON user_identity")
    for stmt in rls_policy_ddl("user_identity"):
        op.execute(stmt)

    op.execute("ALTER TABLE user_identity ALTER COLUMN tenant_id SET NOT NULL")
