"""The tenant-auto-retry-config migration's backfill line (`BACKFILL_THRESHOLD`) is
the one statement a fresh CI DB can never exercise — `0001`'s `create_all` already
gives every row the model's new `nullable=False` default of 0.50, so on a fresh DB
the backfill always runs against zero rows. This test forces the pre-migration
value (0.95) onto an already-provisioned-DB tenant and proves both the backfill and
its equality guard, importing `BACKFILL_THRESHOLD` from the migration module itself
so the test can't drift from what `upgrade()` runs. It also exercises the
`platform_set_tenant_retry_config` SECURITY DEFINER function the migration installs,
mirroring how `test_platform_mfa_enroll.py` establishes the `app.platform` GUC
before calling a definer fn. Everything happens inside one never-committed
transaction per test, so the real `tenant` table is never observed with an extra
row by any other test. Skips without a reachable DB (see conftest)."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.db import uuid7
from vera_core.models import Tenant

# Random-hex prefix is minted at `just makemigration` time — glob, don't hardcode.
MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_tenant_auto_retry_config.py"
    )
)


def _backfill_statement() -> str:
    spec = importlib.util.spec_from_file_location(
        "migration_tenant_auto_retry_config", MIGRATION_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statement: str = module.BACKFILL_THRESHOLD
    return statement


BACKFILL_THRESHOLD = _backfill_statement()


async def _retry_fill_threshold(session: AsyncSession, tenant_id: UUID) -> Decimal:
    result = await session.execute(
        text("SELECT retry_fill_threshold FROM tenant WHERE id = :id"), {"id": tenant_id}
    )
    threshold: Decimal = result.scalar_one()
    return threshold


async def test_threshold_backfill_rewrites_untouched_default(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates the exact provisioned-DB row this backfill exists for: a tenant
    still sitting at the never-admin-settable old default (0.95)."""
    async with admin_sessionmaker() as session:
        tenant = Tenant(
            slug="retry-backfill-mig-untouched",
            name="Backfill Untouched",
            retry_fill_threshold=0.95,
        )
        session.add(tenant)
        await session.flush()

        await session.execute(text(BACKFILL_THRESHOLD))

        assert await _retry_fill_threshold(session, tenant.id) == Decimal("0.50")
        # never committed — nothing here needs to persist


async def test_threshold_backfill_leaves_deliberate_value(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The `WHERE retry_fill_threshold = 0.95` guard: a tenant whose value was
    deliberately set to something else must not be overwritten."""
    async with admin_sessionmaker() as session:
        tenant = Tenant(
            slug="retry-backfill-mig-deliberate",
            name="Backfill Deliberate",
            retry_fill_threshold=0.80,
        )
        session.add(tenant)
        await session.flush()

        await session.execute(text(BACKFILL_THRESHOLD))

        assert await _retry_fill_threshold(session, tenant.id) == Decimal("0.80")
        # never committed — nothing here needs to persist


async def test_definer_fn_partial_update(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`platform_set_tenant_retry_config` treats a NULL param as "leave unchanged":
    each call flips exactly the column it was given a non-NULL value for, and an
    unknown tenant id returns false without raising."""
    async with admin_sessionmaker() as session:
        tenant = Tenant(slug="retry-definer-mig", name="Definer Fn", auto_retry_enabled=False)
        session.add(tenant)
        await session.flush()
        # SET LOCAL-equivalent: set_config(..., true) scopes to this (never-committed) transaction.
        await session.execute(text("SELECT set_config('app.platform', 'on', true)"))

        flipped_flag = (
            await session.execute(
                text("SELECT platform_set_tenant_retry_config(CAST(:id AS uuid), true, NULL)"),
                {"id": tenant.id},
            )
        ).scalar_one()
        assert flipped_flag is True
        await session.refresh(tenant)
        assert tenant.auto_retry_enabled is True
        assert await _retry_fill_threshold(session, tenant.id) == Decimal("0.50")

        set_threshold = (
            await session.execute(
                text(
                    "SELECT platform_set_tenant_retry_config(CAST(:id AS uuid), NULL, :threshold)"
                ),
                {"id": tenant.id, "threshold": Decimal("0.42")},
            )
        ).scalar_one()
        assert set_threshold is True
        await session.refresh(tenant)
        assert tenant.auto_retry_enabled is True
        assert await _retry_fill_threshold(session, tenant.id) == Decimal("0.42")

        unknown = (
            await session.execute(
                text(
                    "SELECT platform_set_tenant_retry_config(CAST(:id AS uuid), true, :threshold)"
                ),
                {"id": uuid7(), "threshold": Decimal("0.10")},
            )
        ).scalar_one()
        assert unknown is False
        # never committed — nothing here needs to persist
