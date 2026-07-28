"""The tenant-concurrency-knobs migration's backfill line (`UPDATE tenant SET
max_concurrent_calls = max_agents_per_va WHERE max_concurrent_calls IS NULL`) is
the one statement in `UPGRADE_STATEMENTS` a fresh CI DB can never exercise —
`0001`'s `create_all` already gives every row a non-NULL `max_concurrent_calls`
(the model's `nullable=False` default), so on a fresh DB the backfill always
runs against zero rows. This test forces the pre-migration shape (NULL) onto an
already-provisioned-DB tenant and proves both the backfill and its `IS NULL`
guard, importing `UPGRADE_STATEMENTS` from the migration module itself so the
test can't drift from what `upgrade()` runs. Everything happens inside one
never-committed transaction per test, so the real `tenant` table is never
observed with a nullable column, or with extra rows, by any other test. Skips
without a reachable DB (see conftest)."""

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import Tenant

# Random-hex prefix is minted at `just makemigration` time — glob, don't hardcode.
MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_tenant_concurrency_knobs.py"
    )
)


def _upgrade_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location(
        "migration_tenant_concurrency_knobs", MIGRATION_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: tuple[str, ...] = module.UPGRADE_STATEMENTS
    return statements


async def _run_upgrade(session: AsyncSession) -> None:
    for statement in _upgrade_statements():
        await session.execute(text(statement))


async def _max_concurrent_calls(session: AsyncSession, tenant_id: object) -> int:
    result = await session.execute(
        text("SELECT max_concurrent_calls FROM tenant WHERE id = :id"), {"id": tenant_id}
    )
    return int(result.scalar_one())


async def test_backfill_sets_max_concurrent_calls_from_max_agents_per_va_when_null(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates the exact provisioned-DB row this backfill exists for: the
    column already exists (this DB is already at head via conftest) but the
    value is NULL, the state every real pre-migration tenant row was in."""
    async with admin_sessionmaker() as session:
        await session.execute(
            text("ALTER TABLE tenant ALTER COLUMN max_concurrent_calls DROP NOT NULL")
        )
        tenant = Tenant(
            slug="concurrency-backfill-mig-null", name="Backfill Null", max_agents_per_va=5
        )
        session.add(tenant)
        await session.flush()
        await session.execute(
            text("UPDATE tenant SET max_concurrent_calls = NULL WHERE id = :id"),
            {"id": tenant.id},
        )

        await _run_upgrade(session)

        assert await _max_concurrent_calls(session, tenant.id) == 5
        # never committed — nothing here needs to persist, including the DROP NOT NULL


async def test_backfill_leaves_an_already_set_value_untouched(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The `WHERE max_concurrent_calls IS NULL` guard: a tenant whose value was
    already set independently of max_agents_per_va must not be overwritten."""
    async with admin_sessionmaker() as session:
        tenant = Tenant(
            slug="concurrency-backfill-mig-set",
            name="Backfill Set",
            max_agents_per_va=5,
            max_concurrent_calls=99,
        )
        session.add(tenant)
        await session.flush()

        await _run_upgrade(session)

        assert await _max_concurrent_calls(session, tenant.id) == 99
        # never committed — nothing here needs to persist
