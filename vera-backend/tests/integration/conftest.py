"""Real-Postgres fixtures. These tests run when a database is reachable
(docker-compose locally, a service container in CI) and skip otherwise.

The compose/CI user is a superuser, which BYPASSES row-level security — so the
fixtures create a dedicated non-superuser role and a second engine connected as
it; that engine is what the RLS assertions use.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vera_core.config import Settings

RLS_ROLE = "vera_rls_test"
RLS_PASSWORD = "vera_rls_test"


def _database_url() -> str:
    return Settings(_env_file=None).database_url


async def _can_connect(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with asyncio.timeout(2):
            async with engine.connect():
                return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if not asyncio.run(_can_connect(url)):
        pytest.skip("postgres not reachable — run `just up && just migrate`")
    return url


@pytest.fixture(scope="session")
def rls_database_url(database_url: str) -> str:
    """Create the non-superuser role + grants, return a URL connecting as it."""

    async def setup() -> None:
        engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            role_exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r").bindparams(r=RLS_ROLE)
            )
            if not role_exists:
                await conn.execute(text(f"CREATE ROLE {RLS_ROLE} LOGIN PASSWORD '{RLS_PASSWORD}'"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}"))
            await conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
                    f" TO {RLS_ROLE}"
                )
            )
        await engine.dispose()

    asyncio.run(setup())
    scheme, rest = database_url.split("://", 1)
    host_part = rest.split("@", 1)[1]
    return f"{scheme}://{RLS_ROLE}:{RLS_PASSWORD}@{host_part}"


@pytest.fixture
async def admin_engine(database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def rls_engine(rls_database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(rls_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def admin_sessionmaker(admin_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(admin_engine, expire_on_commit=False)


@pytest.fixture
async def rls_sessionmaker(rls_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(rls_engine, expire_on_commit=False)
