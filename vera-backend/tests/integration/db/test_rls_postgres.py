"""Proves the RLS policies from migration 0001 actually isolate tenants and
make audit_log immutable — against a real Postgres, as a non-superuser role."""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import CursorResult, func, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.db import tenant_session, uuid7
from vera_core.models import AppUser, AuditLog, Permission, Role, RolePermission, Tenant
from vera_core.models.audit_log import ActorType


@pytest.fixture
async def two_tenants(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[UUID, UUID]]:
    """Two tenants, one user + one audit row each. Created/cleaned as superuser."""
    tenant_a, tenant_b = uuid7(), uuid7()
    async with admin_sessionmaker() as session, session.begin():
        for tid, slug in (
            (tenant_a, f"rls-a-{tenant_a.hex[:8]}"),
            (tenant_b, f"rls-b-{tenant_b.hex[:8]}"),
        ):
            session.add(Tenant(id=tid, slug=slug, name=f"RLS test {slug}", status="active"))
            await session.flush()  # no ORM relationships -> flush tenant before dependents
            session.add(
                AppUser(
                    tenant_id=tid,
                    gcip_uid=f"gcip-{slug}",
                    email=f"{slug}@example.com",
                    name=slug,
                    status="active",
                )
            )
            session.add(
                AuditLog(
                    tenant_id=tid,
                    actor_type=ActorType.SYSTEM,
                    event_type="test.fixture",
                    detail={},
                )
            )
    yield tenant_a, tenant_b
    async with admin_sessionmaker() as session, session.begin():
        for table in ("audit_log", "user_role", "role_permission", "role", "app_user"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)").bindparams(
                    a=tenant_a, b=tenant_b
                )
            )
        await session.execute(
            text("DELETE FROM tenant WHERE id IN (:a, :b)").bindparams(a=tenant_a, b=tenant_b)
        )


async def test_tenant_sees_only_its_own_rows(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_session(rls_sessionmaker, tenant_a) as session:
        users = (await session.execute(select(AppUser))).scalars().all()
        assert users, "tenant A should see its own user"
        assert {u.tenant_id for u in users} == {tenant_a}

        tenants = (await session.execute(select(Tenant))).scalars().all()
        assert [t.id for t in tenants] == [tenant_a]


async def test_no_guc_means_no_rows(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[UUID, UUID],
) -> None:
    # Fail closed: a transaction that never set app.tenant_id sees nothing.
    async with rls_sessionmaker() as session, session.begin():
        count = await session.scalar(select(func.count()).select_from(AppUser))
        assert count == 0
        count = await session.scalar(select(func.count()).select_from(Tenant))
        assert count == 0


async def test_cannot_write_into_another_tenant(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with tenant_session(rls_sessionmaker, tenant_a) as session:
            session.add(
                AppUser(
                    tenant_id=tenant_b,  # WITH CHECK must reject the cross-tenant write
                    gcip_uid="evil",
                    email="evil@example.com",
                    name="evil",
                    status="active",
                )
            )
            await session.flush()


async def test_audit_log_is_immutable_even_for_own_tenant(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, _ = two_tenants
    async with tenant_session(rls_sessionmaker, tenant_a) as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
        assert rows, "tenant A should read its own audit rows"
        # No UPDATE policy exists -> the row is invisible to UPDATE: 0 rows affected.
        result = await session.execute(
            update(AuditLog)
            .where(AuditLog.id == rows[0].id)
            .values(reason="tampered")
            .execution_options(synchronize_session=False)
        )
        assert isinstance(result, CursorResult) and result.rowcount == 0
    # Fresh session (empty identity map) proves the row is untouched in the DB.
    async with tenant_session(rls_sessionmaker, tenant_a) as session:
        reread = (await session.execute(select(AuditLog))).scalars().first()
        assert reread is not None and reread.reason != "tampered"


@pytest.fixture
async def global_role(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[UUID]:
    """A GLOBAL (tenant_id IS NULL) role + permission + grant, inserted as the
    superuser (which bypasses RLS — only privileged provisioning writes the shared
    catalog). Cleaned up the same way."""
    role_id, permission_id, rp_id = uuid7(), uuid7(), uuid7()
    name = f"GLOBAL_ROLE_{role_id.hex[:8]}"
    code = f"catalog:{role_id.hex[:8]}"
    async with admin_sessionmaker() as session, session.begin():
        session.add(Role(id=role_id, tenant_id=None, name=name, description="global"))
        session.add(Permission(id=permission_id, code=code, description="global"))
        await session.flush()
        session.add(
            RolePermission(id=rp_id, tenant_id=None, role_id=role_id, permission_id=permission_id)
        )
    yield role_id
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(text("DELETE FROM role_permission WHERE id = :i").bindparams(i=rp_id))
        await session.execute(
            text("DELETE FROM permission WHERE id = :i").bindparams(i=permission_id)
        )
        await session.execute(text("DELETE FROM role WHERE id = :i").bindparams(i=role_id))


async def test_catalog_role_is_globally_readable_but_not_writable(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[UUID, UUID],
    global_role: UUID,
) -> None:
    """The catalog RLS policy lets a tenant session READ global (NULL-tenant) rows
    but rejects WRITING one (WITH CHECK)."""
    tenant_a, _tenant_b = two_tenants
    # (a) catalog read: a tenant session sees the global role.
    async with tenant_session(rls_sessionmaker, tenant_a) as session:
        global_roles = (
            (await session.execute(select(Role).where(Role.tenant_id.is_(None)))).scalars().all()
        )
        assert global_role in {r.id for r in global_roles}, (
            "tenant session should READ global catalog roles"
        )

    # (b) catalog write: a tenant session may NOT insert a global (NULL-tenant) row.
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with tenant_session(rls_sessionmaker, tenant_a) as session:
            session.add(
                Role(tenant_id=None, name=f"EVIL_GLOBAL_{tenant_a.hex[:8]}", description="evil")
            )
            await session.flush()
