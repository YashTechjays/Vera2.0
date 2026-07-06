"""Row-Level Security helpers.

RLS is the backstop, not the primary check: the authz chain (token verify ->
tenant guard -> RBAC) runs first in application code. These helpers make sure
that even a buggy query can only ever see the current tenant's rows.

The policies key on the per-transaction GUC `app.tenant_id`, set via
`set_config(..., is_local => true)` (the parameterizable form of SET LOCAL) at
the start of every request transaction.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_GUC = "app.tenant_id"

# Set to 'on' only by a verified platform (SUPER_ADMIN) session. It widens the
# read side of the platform-readable policy to global (NULL-tenant) identity/RBAC
# rows — never any tenant PHI row, which stays gated on a matching TENANT_GUC.
PLATFORM_GUC = "app.platform"

# A syntactically-valid sentinel for TENANT_GUC in platform-only sessions. Postgres
# custom GUCs are registered per-connection, not per-transaction: once ANY
# transaction on a pooled connection calls set_config(TENANT_GUC, ..., true), that
# connection's backend remembers app.tenant_id as a known parameter for the rest of
# its life — a LATER transaction that never sets it again no longer reads back NULL
# from current_setting(TENANT_GUC, true); it reads back '' (the custom GUC's own
# reset value). Every RLS policy casts that read with `::uuid`, which raises on ''
# but not on NULL. A platform session must therefore pin TENANT_GUC to a value that
# always casts cleanly and can never match a real tenant row, rather than leaving it
# unset and hoping the connection was never previously touched by a tenant session.
#
# Pinning the sentinel also changes strict WITH CHECK semantics from "always deny" (an
# unset/NULL GUC can never equal any tenant_id) to "deny unless a row's tenant column
# literally equals the nil UUID" — closed off by FK anchoring on every tenant_id column,
# since no real tenant row can ever have id = NIL_TENANT_ID; `tenant.id` itself is the
# one tenant_id-shaped column with no FK anchor, so it must never be seeded with this value.
NIL_TENANT_ID = UUID(int=0)


async def set_current_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """Apply SET LOCAL app.tenant_id for the session's current transaction.

    Must be called inside an open transaction — `is_local => true` scopes the
    setting to the transaction, so it cannot leak across pooled connections.
    """
    await session.execute(select(func.set_config(TENANT_GUC, str(tenant_id), True)))


async def set_platform(session: AsyncSession) -> None:
    """Apply SET LOCAL app.platform = 'on' for the current transaction — the flag
    a platform-readable policy keys on to expose global (NULL-tenant) rows."""
    await session.execute(select(func.set_config(PLATFORM_GUC, "on", True)))


@asynccontextmanager
async def tenant_session(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> AsyncGenerator[AsyncSession]:
    """A session whose transaction is pinned to `tenant_id` for RLS purposes."""
    async with sessionmaker() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        yield session


@asynccontextmanager
async def platform_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """A SUPER_ADMIN's no-tenant session for global catalog/identity reads.

    Sets app.platform='on' and pins TENANT_GUC to NIL_TENANT_ID (never a real
    tenant), so platform-readable policies expose the global (NULL-tenant) rows of
    `app_user`/`user_role`/`role`/`role_permission` and nothing else: every
    PHI/tenant table's strict policy compares against a tenant GUC that can never
    match a real row (fail-closed → zero tenant rows) instead of an unset one.
    Cross-tenant PHI access requires `elevated_session` and an active grant."""
    async with sessionmaker() as session, session.begin():
        await set_current_tenant(session, NIL_TENANT_ID)
        await set_platform(session)
        yield session


@asynccontextmanager
async def elevated_session(
    sessionmaker: async_sessionmaker[AsyncSession], target_tenant_id: UUID
) -> AsyncGenerator[AsyncSession]:
    """A SUPER_ADMIN acting INSIDE one tenant under an active elevation grant.

    The normal tenant GUC pins RLS to `target_tenant_id` (full tenant isolation is
    in force — no RLS bypass), while app.platform='on' additionally lets the
    operator's own global RBAC grant resolve. Callers must have validated the grant
    (active, unexpired) for THIS transaction and stamp `audit_log.elevation_session_id`
    on every PHI read."""
    async with sessionmaker() as session, session.begin():
        await set_current_tenant(session, target_tenant_id)
        await set_platform(session)
        yield session


def rls_policy_ddl(table: str, *, tenant_column: str = "tenant_id") -> list[str]:
    """DDL statements that enable tenant-isolation RLS on `table`.

    Used by Alembic migrations — policies live IN migrations so the DB schema
    is fully reproducible from `alembic upgrade head`.

    `current_setting(..., true)` returns NULL when the GUC is unset, so a
    connection that never called set_current_tenant sees zero rows (fail
    closed). FORCE makes the policy apply to the table owner too, which is
    what the app role is on Cloud SQL.

    Caveat: on a pooled connection previously touched by a tenant session, the GUC is
    no longer unset but '' (its reset value), and `::uuid` raises rather than reading
    NULL — still fail-closed, but as an error, not zero rows. `platform_session` avoids
    this by pinning the GUC to `NIL_TENANT_ID` instead of leaving it unset.
    """
    policy = f"{table}_tenant_isolation"
    using = f"{tenant_column} = current_setting('{TENANT_GUC}', true)::uuid"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy} ON {table} USING ({using}) WITH CHECK ({using})",
    ]


def catalog_rls_policy_ddl(table: str, *, tenant_column: str = "tenant_id") -> list[str]:
    """DDL for a shared-catalog tenant-isolation policy on `table`.

    Like `rls_policy_ddl`, but the read (USING) clause ALSO matches global rows
    whose tenant column is NULL — the platform-identity tier. A tenant session
    therefore sees its own rows PLUS the global catalog, while WITH CHECK stays
    strict equality so tenants still cannot write (or own) global rows.

    The `OR {tenant_column} IS NULL` read leniency is confined to global-catalog
    tables (ADR §3.5.9 security rule), e.g. `role` / `role_permission`. It must
    NEVER be used on a PHI or data table — there a NULL tenant_id would silently
    expose a row to every tenant. Use `rls_policy_ddl` (strict equality) there.

    Reuses the SAME policy name (`{table}_tenant_isolation`) as `rls_policy_ddl`,
    so `drop_rls_policy_ddl(table)` drops this policy too.
    """
    policy = f"{table}_tenant_isolation"
    strict = f"{tenant_column} = current_setting('{TENANT_GUC}', true)::uuid"
    using = f"{strict} OR {tenant_column} IS NULL"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy} ON {table} USING ({using}) WITH CHECK ({strict})",
    ]


def platform_readable_rls_policy_ddl(table: str, *, tenant_column: str = "tenant_id") -> list[str]:
    """Strict tenant isolation PLUS a platform-session read of global rows.

    Used for the identity/RBAC tables that carry a NULLABLE tenant_id and whose
    global (NULL-tenant) rows a platform SUPER_ADMIN must resolve — `app_user` and
    `user_role` (ADR §3.5.9 platform runtime). The read (USING) clause matches a
    NULL-tenant row ONLY when app.platform='on', so:

      * a tenant session (flag unset) sees ONLY `tenant_id = GUC` — NULL rows stay
        invisible to it, preserving the NullableTenantColumnMixin fail-closed rule;
      * a platform session (`platform_session`: flag on, tenant GUC pinned to
        `NIL_TENANT_ID` rather than left unset — see that sentinel's docstring) sees
        ONLY the NULL-tenant rows;
      * an `elevated_session` (flag on + tenant GUC) sees both — the operator's own
        global grant and the target tenant's rows.

    WITH CHECK stays strict equality, so NULL-tenant (platform) rows are NEVER
    written on this path — they are seeded with privilege or written via a
    SECURITY DEFINER function. Reuses the `{table}_tenant_isolation` policy name so
    `drop_rls_policy_ddl(table)` drops it.
    """
    policy = f"{table}_tenant_isolation"
    strict = f"{tenant_column} = current_setting('{TENANT_GUC}', true)::uuid"
    platform = f"{tenant_column} IS NULL AND current_setting('{PLATFORM_GUC}', true) = 'on'"
    using = f"({strict}) OR ({platform})"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy} ON {table} USING ({using}) WITH CHECK ({strict})",
    ]


def drop_rls_policy_ddl(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]
