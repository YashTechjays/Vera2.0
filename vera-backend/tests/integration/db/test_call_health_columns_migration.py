"""The call-health-columns migration's actual DDL (`UPGRADE_STATEMENTS`, imported
from the migration module itself so the test can't drift from what `upgrade()`
runs) against a real Postgres — not just the ORM model's Python-side view of it.
Covers the two states the repo's migration idempotency rule exists for: an
already-migrated DB (re-running must no-op) and a DB that predates this migration
(the ADD COLUMN/ADD CONSTRAINT statements must actually create them). Both tests
run inside one transaction that is never committed, so the real, already-migrated
`call` table is never observed missing these columns by any other test. Skips
without a reachable DB (see conftest)."""

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MIGRATION_FILE = next(
    (Path(__file__).resolve().parents[3] / "migrations" / "versions").glob(
        "*_call_health_columns.py"
    )
)
CONSTRAINT = "ck_call_health_flag_valid"
HEALTH_COLUMNS = ("health_score", "health_flag", "health_analyzed_at")


def _upgrade_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("migration_call_health_columns", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: tuple[str, ...] = module.UPGRADE_STATEMENTS
    return statements


async def _run_upgrade(session: AsyncSession) -> None:
    for statement in _upgrade_statements():
        await session.execute(text(statement))


async def _health_columns(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'call' AND column_name = ANY(:cols)"
        ),
        {"cols": list(HEALTH_COLUMNS)},
    )
    return set(rows.scalars().all())


async def _constraint_def(session: AsyncSession) -> str | None:
    result: str | None = await session.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = :name AND conrelid = 'call'::regclass"
        ),
        {"name": CONSTRAINT},
    )
    return result


async def _constraint_validated(session: AsyncSession) -> bool | None:
    result: bool | None = await session.scalar(
        text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
        {"name": CONSTRAINT},
    )
    return result


async def test_upgrade_is_a_no_op_on_an_already_migrated_db(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The dedicated test DB is already at head (conftest) — columns and constraint
    exist. Re-running the exact statements `upgrade()` issues must not raise (the
    already-provisioned-DB case the repo's idempotency rule exists for)."""
    async with admin_sessionmaker() as session:
        await _run_upgrade(session)  # would raise if IF NOT EXISTS / duplicate_object failed

        assert await _health_columns(session) == set(HEALTH_COLUMNS)
        assert await _constraint_validated(session) is True
        # never committed — nothing here needs to persist


async def test_upgrade_from_a_pre_migration_table_adds_columns_and_validated_constraint(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Simulate a DB that predates this migration by dropping the columns/constraint
    it adds, then run the real upgrade statements and confirm they come back with
    the right CHECK expression, fully validated. Everything — the drop AND the
    upgrade — happens inside one never-committed transaction, so no other test
    (or process, mid-suite) ever sees the real `call` table missing these columns."""
    async with admin_sessionmaker() as session:
        await session.execute(text(f"ALTER TABLE call DROP CONSTRAINT {CONSTRAINT}"))
        for col in HEALTH_COLUMNS:
            await session.execute(text(f"ALTER TABLE call DROP COLUMN {col}"))
        assert await _health_columns(session) == set()

        await _run_upgrade(session)

        assert await _health_columns(session) == set(HEALTH_COLUMNS)
        definition = await _constraint_def(session)
        assert definition is not None
        for flag in (
            "none",
            "supervisor_requested",
            "repeated_questions",
            "hallucination",
            "conversation_loop",
            "long_silence",
            "off_script",
            "low_confidence",
            "other",
        ):
            assert flag in definition
        assert await _constraint_validated(session) is True
        # never committed — the drop is rolled back along with the re-add
