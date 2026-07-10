"""Real-Postgres fixtures. These tests run when a database is reachable
(docker-compose locally, a service container in CI) and skip otherwise.

Tests run against a DEDICATED test database — `<dev db>_test` (e.g. `vera_test`),
derived from the configured URL — never the dev database itself. The session
fixture creates it on demand and migrates it to head, so suites can neither be
broken by developer data nor corrupt it.

The compose/CI user is a superuser, which BYPASSES row-level security — so the
fixtures create a dedicated non-superuser role and a second engine connected as
it; that engine is what the RLS assertions use.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

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
BACKEND_ROOT = Path(__file__).resolve().parents[2]


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
    """URL of the dedicated test database, created + migrated on demand."""
    base = _database_url()
    if not asyncio.run(_can_connect(base)):
        pytest.skip("postgres not reachable — run `just up`")
    head, _, dev_db = base.rpartition("/")
    test_db = dev_db if dev_db.endswith("_test") else f"{dev_db}_test"
    test_url = f"{head}/{test_db}"

    async def ensure_database() -> None:
        engine = create_async_engine(base, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :d").bindparams(d=test_db)
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{test_db}"'))
        await engine.dispose()

    asyncio.run(ensure_database())
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env={**os.environ, "VERA_DATABASE_URL": test_url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade of {test_db} failed:\n{result.stderr}")
    return test_url


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
            # vera_rls_test stands in for the deployed app role, which the definer
            # functions now grant EXECUTE to explicitly (migration f066c667ddc1 revokes
            # the PUBLIC default). Mirror that grant so the RLS role can still invoke them.
            await conn.execute(
                text(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {RLS_ROLE}")
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
