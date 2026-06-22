"""Idempotency of the platform-operator bootstrap (ADR-0006 §D) against live
Postgres. Runs as superuser (like the real script) so the NULL-tenant inserts and
the envelope-encrypted MFA seed bypass FORCE RLS. The first call seeds operator #1
and returns its otpauth:// URI; a second call is a no-op (None), leaving exactly one
platform operator. SUPER_ADMIN is seeded first via the same helpers the script needs.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import pyotp
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.bootstrap_platform_admin import bootstrap
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config.kms import LocalDevKMS
from vera_core.models import AppUser
from vera_core.models.enums import AccountType

PASSWORD = "correct horse battery staple"
_MASTER_KEY = b"a" * 32


async def _platform_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AppUser)
            .where(AppUser.account_type == AccountType.PLATFORM.value)
        )
    ).scalar_one()


async def _delete_platform_operators(session: AsyncSession) -> None:
    ids = (
        (
            await session.execute(
                select(AppUser.id).where(AppUser.account_type == AccountType.PLATFORM.value)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return
    for table in ("auth_audit_log", "user_role", "user_identity", "app_user"):
        col = "id" if table == "app_user" else "app_user_id"
        await session.execute(
            text(f"DELETE FROM {table} WHERE {col} = ANY(:ids)").bindparams(ids=ids)
        )


@pytest.fixture
async def bootstrap_world(
    database_url: str,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], LocalDevKMS, str]]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    email = "operator-bootstrap@vera.example"

    async with sessionmaker() as session, session.begin():
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)
        # Start from a clean slate so the first bootstrap is guaranteed to create #1.
        await _delete_platform_operators(session)

    yield sessionmaker, LocalDevKMS(master_key=_MASTER_KEY), email

    async with sessionmaker() as session, session.begin():
        await _delete_platform_operators(session)
    await engine.dispose()


async def test_bootstrap_is_idempotent(
    bootstrap_world: tuple[async_sessionmaker[AsyncSession], LocalDevKMS, str],
) -> None:
    sessionmaker, kms, email = bootstrap_world

    uri = await bootstrap(sessionmaker, kms, email=email, password=PASSWORD)
    assert uri is not None
    assert uri.startswith("otpauth://")
    # A scannable TOTP seed was enrolled.
    assert pyotp.parse_uri(uri).secret

    async with sessionmaker() as session:
        assert await _platform_count(session) == 1

    # Second run is a no-op: no new operator, returns None.
    again = await bootstrap(sessionmaker, kms, email=email, password=PASSWORD)
    assert again is None

    async with sessionmaker() as session:
        assert await _platform_count(session) == 1


async def test_bootstrap_grants_super_admin(
    bootstrap_world: tuple[async_sessionmaker[AsyncSession], LocalDevKMS, str],
) -> None:
    sessionmaker, kms, email = bootstrap_world
    await bootstrap(sessionmaker, kms, email=email, password=PASSWORD)

    async with sessionmaker() as session:
        user_id: UUID = (
            await session.execute(
                select(AppUser.id).where(
                    AppUser.account_type == AccountType.PLATFORM.value,
                    AppUser.email == email,
                )
            )
        ).scalar_one()
        role_name = (
            await session.execute(
                text(
                    "SELECT r.name FROM user_role ur JOIN role r ON r.id = ur.role_id "
                    "WHERE ur.app_user_id = :u"
                ).bindparams(u=user_id)
            )
        ).scalar_one()
        assert role_name == "SUPER_ADMIN"
