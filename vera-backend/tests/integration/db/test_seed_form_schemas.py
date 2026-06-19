"""_seed_form_schemas against a real Postgres: it publishes exactly one version,
is idempotent on re-run, and the partial unique index rejects a second published
version for the same schema. Skips without a reachable DB (see conftest)."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from vera_core.models import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Remove the infertility_treatment family (global catalog, no tenant scope)."""
    async with sessionmaker() as session, session.begin():
        schema_ids = (
            (
                await session.execute(
                    select(FormSchema.id).where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            )
            .scalars()
            .all()
        )
        if schema_ids:
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))


@pytest.fixture
async def clean_form_schemas(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(admin_sessionmaker)
    yield
    await _wipe(admin_sessionmaker)


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    """(form_schema, total schema_version, published schema_version) for the family."""
    schemas = (
        await session.execute(
            select(func.count())
            .select_from(FormSchema)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).scalar_one()
    versions = (
        await session.execute(
            select(
                func.count(), func.count().filter(SchemaVersion.status == VersionStatus.PUBLISHED)
            )
            .select_from(SchemaVersion)
            .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).one()
    return schemas, versions[0], versions[1]


async def test_seed_publishes_single_version_and_is_idempotent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_form_schemas: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)  # one schema, one version, published
        version = (
            await session.execute(
                select(SchemaVersion)
                .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
                .where(FormSchema.insurance_type == INSURANCE_TYPE)
            )
        ).scalar_one()
        assert version.version == 1
        assert version.published_at is not None

    # Re-run with unchanged JSON: no new version (no extra draft), no duplicate schema.
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)


async def test_second_published_version_rejected(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_form_schemas: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)

    with pytest.raises(IntegrityError):
        async with admin_sessionmaker() as session, session.begin():
            schema = (
                await session.execute(
                    select(FormSchema).where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            ).scalar_one()
            session.add(
                SchemaVersion(
                    schema_id=schema.id,
                    version=2,
                    schema_json={"name": "dupe"},
                    status=VersionStatus.PUBLISHED,
                )
            )
            await session.flush()
