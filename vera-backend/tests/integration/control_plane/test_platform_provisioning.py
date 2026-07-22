"""Integration tests for the platform-operator SECURITY DEFINER write helpers —
run against a real RLS-enforcing Postgres (not mocked), since the whole point of
these functions is to work around a real RLS restriction."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.platform_provisioning import (
    create_operator_invite,
    create_password_identity,
    set_operator_status,
)
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.db import platform_session, tenant_session, uuid7
from vera_core.models import AppUser, UserIdentity, UserRole

# No `pytestmark = pytest.mark.anyio` here: this repo is asyncio-only
# (`asyncio_mode = "auto"` in pyproject.toml) — anyio is a transitive dependency
# only (pulled in by httpx/starlette), never a marker to opt into (see CLAUDE.md's
# "asyncio is the single async runtime" rule). Every sibling integration test in
# this directory runs async tests unmarked under the same auto mode; the anyio
# marker instead activates a second, competing event-loop manager and produced a
# real "attached to a different loop" asyncpg failure under this suite.


@pytest.fixture
async def rls_sessionmaker(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """A plain RLS-bound sessionmaker (no HTTP app) for testing the definer-wrapper
    functions directly. `database_url` is the superuser connection, used only to
    seed the permission catalog + SUPER_ADMIN role before RLS-bound tests run against
    `rls_database_url` — mirrors test_platform_elevation.py's `world` fixture split
    between the two connection strings, minus the HTTP app layer this file doesn't need."""
    seed_engine = create_async_engine(database_url)
    seed_sm = async_sessionmaker(seed_engine, expire_on_commit=False)
    async with seed_sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)

    rls_engine = create_async_engine(rls_database_url)
    rls_sm = async_sessionmaker(rls_engine, expire_on_commit=False)
    yield rls_sm

    async with seed_sm() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM user_identity WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_role WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(text("DELETE FROM app_user WHERE account_type = 'platform'"))
    await rls_engine.dispose()
    await seed_engine.dispose()


async def test_create_operator_invite_creates_invited_platform_user_with_super_admin(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="new-operator@example.com", name="New Operator", invited_by=None
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one()
        assert user.account_type == "platform"
        assert user.tenant_id is None
        assert user.status == "invited"
        assert user.email == "new-operator@example.com"

        role_ids = (
            (await session.execute(select(UserRole.role_id).where(UserRole.app_user_id == user_id)))
            .scalars()
            .all()
        )
        role_names = (
            (
                await session.execute(
                    text("SELECT name FROM role WHERE id = ANY(:ids)").bindparams(
                        ids=list(role_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "SUPER_ADMIN" in role_names


async def test_plain_orm_insert_of_null_tenant_app_user_is_rejected_by_rls(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Proves the constraint this task exists to work around — a direct ORM insert
    of a NULL-tenant row must fail under RLS, confirming the definer function is
    actually necessary and not incidental complexity."""
    async with platform_session(rls_sessionmaker) as session:
        session.add(
            AppUser(
                tenant_id=None,
                account_type="platform",
                email="should-fail@example.com",
                name="",
                status="invited",
            )
        )
        with pytest.raises(DBAPIError):  # asyncpg raises a RLS policy violation
            await session.flush()


async def test_create_password_identity_then_set_status_active(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="accepts@example.com", name="", invited_by=None
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        identity_id = await create_password_identity(
            session, app_user_id=user_id, email="accepts@example.com", hashed_password="hashed"
        )
        await session.commit()

    async with platform_session(rls_sessionmaker) as session:
        identity = (
            await session.execute(select(UserIdentity).where(UserIdentity.id == identity_id))
        ).scalar_one()
        assert identity.app_user_id == user_id
        assert identity.tenant_id is None
        assert identity.hashed_password == "hashed"

        flipped = await set_operator_status(session, app_user_id=user_id, status="active")
        await session.commit()
        assert flipped is True

    async with platform_session(rls_sessionmaker) as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one()
        assert user.status == "active"


async def test_set_operator_status_rejects_invalid_status(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with platform_session(rls_sessionmaker) as session:
        user_id = await create_operator_invite(
            session, email="bad-status@example.com", name="", invited_by=None
        )
        await session.commit()
    async with platform_session(rls_sessionmaker) as session:
        with pytest.raises(DBAPIError):
            await set_operator_status(session, app_user_id=user_id, status="not-a-real-status")


async def test_create_operator_invite_rejected_outside_platform_session(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Regression test for the guard fix: `current_setting('app.platform', true)`
    reads SQL NULL (not 'off') on a connection that never opened a platform session,
    and plpgsql's `IF NOT (NULL)` is falsy — a bare `NOT (guard)` would silently skip
    the RAISE and let an ordinary tenant caller through. `tenant_session` is a real
    RLS-bound session that never sets `app.platform`, so calling a definer function
    from inside it exercises exactly that unset-GUC path and proves `IS NOT TRUE`
    fails closed instead of open."""
    async with tenant_session(rls_sessionmaker, uuid7()) as session:
        with pytest.raises(DBAPIError):
            await create_operator_invite(
                session, email="attacker@example.com", name="", invited_by=None
            )


async def test_set_operator_status_returns_false_for_nonexistent_user(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The documented "quiet no-op" contract: from within a genuine platform session
    (guard passes), an id matching no AppUser row updates zero rows and returns
    False rather than raising."""
    async with platform_session(rls_sessionmaker) as session:
        flipped = await set_operator_status(session, app_user_id=uuid7(), status="active")
        await session.commit()
        assert flipped is False
