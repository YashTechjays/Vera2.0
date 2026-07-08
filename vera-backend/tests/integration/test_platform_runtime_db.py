"""DB-layer tests for the platform runtime (ADR-0006 §C): the SECURITY DEFINER
write paths and the platform-readable identity RLS.

Everything here runs as the NON-superuser `vera_rls_test` role (via rls_sessionmaker)
so the assertions prove the app role's real, RLS-bound experience — it can only
touch the platform tier through the narrow functions, never directly. Setup/teardown
that must bypass RLS uses the superuser admin_sessionmaker."""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.db import platform_session, set_current_tenant, set_platform, tenant_session, uuid7
from vera_core.models import AppUser, Tenant, UserRole


class PlatformWorld:
    def __init__(self, tenant_id: UUID, platform_user_id: UUID, tenant_user_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.platform_user_id = platform_user_id
        self.tenant_user_id = tenant_user_id


@pytest.fixture
async def world(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[PlatformWorld]:
    """One tenant, one platform SUPER_ADMIN (tenant_id NULL), one tenant user.
    Built as superuser; torn down after."""
    tenant_id, platform_user_id, tenant_user_id = uuid7(), uuid7(), uuid7()
    async with admin_sessionmaker() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_admin = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        s.add(
            Tenant(
                id=tenant_id,
                slug=f"pr-{tenant_id.hex[:8]}",
                name=f"PR {tenant_id.hex[:8]}",
                status="active",
            )
        )
        await s.flush()
        s.add(
            AppUser(
                id=platform_user_id,
                tenant_id=None,
                account_type="platform",
                email="ops@vera.example",
                name="Platform Ops",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=tenant_user_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email="member@tenant.example",
                name="Member",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=platform_user_id, role_id=super_admin))

    yield PlatformWorld(tenant_id, platform_user_id, tenant_user_id)

    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("DELETE FROM tenant_elevation WHERE target_tenant_id = :t").bindparams(t=tenant_id)
        )
        await s.execute(
            text("DELETE FROM auth_audit_log WHERE app_user_id = :p").bindparams(p=platform_user_id)
        )
        await s.execute(
            text("DELETE FROM user_role WHERE app_user_id = :p").bindparams(p=platform_user_id)
        )
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:p, :u)").bindparams(
                p=platform_user_id, u=tenant_user_id
            )
        )
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))


# --- platform-readable identity RLS ---------------------------------------


async def test_platform_session_sees_null_tenant_app_user(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    async with rls_sessionmaker() as s, s.begin():
        await set_platform(s)
        ids = set((await s.execute(text("SELECT id FROM app_user"))).scalars())
    assert world.platform_user_id in ids  # the SUPER_ADMIN's NULL-tenant row is visible
    assert world.tenant_user_id not in ids  # a tenant row never matches a no-tenant session


async def test_tenant_session_cannot_see_platform_user(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    async with rls_sessionmaker() as s, s.begin():
        await set_current_tenant(s, world.tenant_id)
        ids = set((await s.execute(text("SELECT id FROM app_user"))).scalars())
    assert world.tenant_user_id in ids
    assert world.platform_user_id not in ids  # NullableTenantColumnMixin invariant preserved


async def test_platform_session_resolves_super_admin_grant(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    """The whole point: a platform session can resolve the SUPER_ADMIN's global
    user_role -> role_permission -> permission chain (all NULL-tenant rows)."""
    async with rls_sessionmaker() as s, s.begin():
        await set_platform(s)
        codes = set(
            (
                await s.execute(
                    text(
                        "SELECT p.code FROM permission p "
                        "JOIN role_permission rp ON rp.permission_id = p.id "
                        "JOIN user_role ur ON ur.role_id = rp.role_id "
                        "WHERE ur.app_user_id = :u"
                    ).bindparams(u=world.platform_user_id)
                )
            ).scalars()
        )
    assert "calls:read" in codes and "audit:read" in codes  # SUPER_ADMIN holds all perms


async def test_platform_session_survives_prior_tenant_session_on_same_connection(
    rls_database_url: str, world: PlatformWorld
) -> None:
    """Regression for the GUC-contamination bug (e7bb96c): once a pooled connection's
    backend has registered `app.tenant_id` (via a prior tenant_session), a later
    platform_session on that SAME connection must still resolve — it must not hit the
    `''::uuid` cast error a bare "GUC left unset" platform session would raise on a
    contaminated connection. `pool_size=1, max_overflow=0` forces both sessions onto
    the one physical connection, so this only passes because platform_session pins
    TENANT_GUC to NIL_TENANT_ID instead of leaving it unset."""
    engine = create_async_engine(rls_database_url, pool_size=1, max_overflow=0)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        async with tenant_session(sessionmaker, world.tenant_id) as s:
            await s.execute(text("SELECT 1 FROM app_user"))  # registers app.tenant_id on the conn

        async with platform_session(sessionmaker) as s:
            ids = set((await s.execute(text("SELECT id FROM app_user"))).scalars())
        assert world.platform_user_id in ids  # platform-readable NULL-tenant row still resolves
        assert world.tenant_user_id not in ids
    finally:
        await engine.dispose()


# --- SECURITY DEFINER: elevation lifecycle --------------------------------


async def _create_grant(
    session: AsyncSession, *, admin_id: UUID, tenant_id: UUID, reason: str = "support ticket #42"
) -> UUID:
    grant_id: UUID = (
        await session.execute(
            text("SELECT create_elevation_grant(:a, :t, :r, :d)").bindparams(
                a=admin_id, t=tenant_id, r=reason, d=60
            )
        )
    ).scalar_one()
    return grant_id


async def test_create_and_read_active_grant(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    async with rls_sessionmaker() as s, s.begin():
        grant_id = await _create_grant(
            s, admin_id=world.platform_user_id, tenant_id=world.tenant_id
        )
        active = (
            await s.execute(
                text("SELECT id, reason FROM active_elevation_grants(:a, :t)").bindparams(
                    a=world.platform_user_id, t=world.tenant_id
                )
            )
        ).all()
    assert len(active) == 1
    assert active[0].id == grant_id
    assert active[0].reason == "support ticket #42"


async def test_app_role_cannot_insert_grant_directly(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    """tenant_elevation has no INSERT policy under FORCE RLS — the function is the
    ONLY write path for the RLS-bound app role."""
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with rls_sessionmaker() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO tenant_elevation "
                    "(id, super_admin_user_id, target_tenant_id, reason, granted_at, expires_at) "
                    "VALUES (:i, :a, :t, 'x', now(), now() + interval '1 hour')"
                ).bindparams(i=uuid7(), a=world.platform_user_id, t=world.tenant_id)
            )


async def test_one_active_grant_per_admin(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    from sqlalchemy.exc import DBAPIError

    async with rls_sessionmaker() as s, s.begin():
        await _create_grant(s, admin_id=world.platform_user_id, tenant_id=world.tenant_id)
    with pytest.raises(DBAPIError):  # UNIQUE(super_admin_user_id, ended_at) with ended_at NULL
        async with rls_sessionmaker() as s, s.begin():
            await _create_grant(s, admin_id=world.platform_user_id, tenant_id=world.tenant_id)


async def test_end_grant_deactivates(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    async with rls_sessionmaker() as s, s.begin():
        grant_id = await _create_grant(
            s, admin_id=world.platform_user_id, tenant_id=world.tenant_id
        )
        ended = (
            await s.execute(text("SELECT end_elevation_grant(:g)").bindparams(g=grant_id))
        ).scalar_one()
        assert ended is True
        again = (
            await s.execute(text("SELECT end_elevation_grant(:g)").bindparams(g=grant_id))
        ).scalar_one()
        assert again is False  # already ended → no-op
        remaining = (
            await s.execute(
                text("SELECT count(*) FROM active_elevation_grants(:a, :t)").bindparams(
                    a=world.platform_user_id, t=world.tenant_id
                )
            )
        ).scalar_one()
    assert remaining == 0


async def test_create_grant_rejects_empty_reason_and_nonpositive_duration(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with rls_sessionmaker() as s, s.begin():
            await _create_grant(
                s, admin_id=world.platform_user_id, tenant_id=world.tenant_id, reason="   "
            )
    with pytest.raises(DBAPIError):  # duration must be positive (now computed DB-side)
        async with rls_sessionmaker() as s, s.begin():
            await s.execute(
                text("SELECT create_elevation_grant(:a, :t, 'ok', 0)").bindparams(
                    a=world.platform_user_id, t=world.tenant_id
                )
            )


# --- SECURITY DEFINER: null-tenant auth audit -----------------------------


async def test_log_auth_event_null_tenant(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    world: PlatformWorld,
) -> None:
    async with rls_sessionmaker() as s, s.begin():
        event_id = (
            await s.execute(
                text(
                    "SELECT log_auth_event(NULL, :u, 'login_success', NULL, '{}'::jsonb)"
                ).bindparams(u=world.platform_user_id)
            )
        ).scalar_one()
    # Visible to the superuser (BYPASSRLS); tenant_id stored NULL.
    async with admin_sessionmaker() as s:
        row = (
            await s.execute(
                text("SELECT tenant_id, event_type FROM auth_audit_log WHERE id = :i").bindparams(
                    i=event_id
                )
            )
        ).one()
    assert row.tenant_id is None
    assert row.event_type == "login_success"


async def test_app_role_cannot_insert_null_tenant_auth_event_directly(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with rls_sessionmaker() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO auth_audit_log (id, tenant_id, app_user_id, event_type) "
                    "VALUES (:i, NULL, :u, 'login_success')"
                ).bindparams(i=uuid7(), u=world.platform_user_id)
            )


# --- elevated session: tenant GUC + platform flag together ----------------


async def test_elevated_session_sees_tenant_and_own_grant(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    """An elevated session pins the tenant GUC (full RLS) AND keeps the platform
    flag, so the operator reads the target tenant's rows and their own grant."""
    async with rls_sessionmaker() as s, s.begin():
        grant_id = await _create_grant(
            s, admin_id=world.platform_user_id, tenant_id=world.tenant_id
        )
    async with rls_sessionmaker() as s, s.begin():
        await set_current_tenant(s, world.tenant_id)
        await set_platform(s)
        # tenant_elevation SELECT policy: target_tenant_id = GUC → own grant readable
        grant_ids = set((await s.execute(text("SELECT id FROM tenant_elevation"))).scalars())
        app_user_ids = set((await s.execute(text("SELECT id FROM app_user"))).scalars())
    assert grant_id in grant_ids
    assert world.tenant_user_id in app_user_ids  # tenant rows visible
    assert world.platform_user_id in app_user_ids  # own NULL-tenant row visible too


# --- nil-tenant sentinel hardening -----------------------------------------


async def test_tenant_nil_id_rejected_by_check(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The DB refuses a tenant row with the nil-UUID sentinel id — even from a
    superuser (seed/fixture accidents), so platform_session's GUC pin can never
    match a real tenant."""
    async with admin_sessionmaker() as s:
        with pytest.raises(IntegrityError, match="ck_tenant_id_not_nil"):
            async with s.begin():
                s.add(
                    Tenant(
                        id=UUID(int=0),
                        slug="nil-tenant",
                        name="Nil Tenant",
                        status="active",
                    )
                )


async def test_platform_session_cannot_write_nil_tenant_row(
    rls_sessionmaker: async_sessionmaker[AsyncSession], world: PlatformWorld
) -> None:
    """Write side of the sentinel: a platform session's GUC equals the nil UUID,
    so strict WITH CHECK would admit a nil-tenant row — the FK to tenant.id must
    refuse it (no nil tenant can exist, enforced by ck_tenant_id_not_nil)."""
    async with rls_sessionmaker() as s:
        with pytest.raises((IntegrityError, ProgrammingError)):
            async with s.begin():
                await set_platform(s)
                await s.execute(
                    text(
                        "INSERT INTO role (id, tenant_id, name, description)"
                        " VALUES (:i, :t, 'NIL-ROLE', '')"
                    ).bindparams(i=uuid7(), t=UUID(int=0))
                )
