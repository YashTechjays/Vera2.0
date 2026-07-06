"""_seed_prompts against a real Postgres: it binds each prompt to its target
schema's published version, publishes exactly one prompt_version, is idempotent
on re-run, skips cleanly when the target schema has no published version, and the
partial unique index rejects a second published version per prompt. Skips without
a reachable DB (see conftest)."""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas, _seed_prompts
from vera_core.models import FormSchema, Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value


async def _wipe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Remove the infertility_treatment prompt + schema families (global catalog,
    no tenant scope). prompt_version.schema_version_id is RESTRICT, so delete the
    prompt rows (which CASCADE their versions) before the schema_versions."""
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
            await session.execute(delete(Prompt).where(Prompt.schema_id.in_(schema_ids)))
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids))
            )
            await session.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))


@pytest.fixture
async def clean_prompts(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(admin_sessionmaker)
    yield
    await _wipe(admin_sessionmaker)


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    """(prompt, total prompt_version, published prompt_version) for the family."""
    prompts = (
        await session.execute(
            select(func.count())
            .select_from(Prompt)
            .join(FormSchema, Prompt.schema_id == FormSchema.id)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).scalar_one()
    versions = (
        await session.execute(
            select(
                func.count(), func.count().filter(PromptVersion.status == VersionStatus.PUBLISHED)
            )
            .select_from(PromptVersion)
            .join(Prompt, PromptVersion.prompt_id == Prompt.id)
            .join(FormSchema, Prompt.schema_id == FormSchema.id)
            .where(FormSchema.insurance_type == INSURANCE_TYPE)
        )
    ).one()
    total, published = versions  # the single (count(), count().filter(...)) row
    return prompts, total, published


async def _published_schema_version_id(session: AsyncSession) -> UUID:
    return (
        await session.execute(
            select(SchemaVersion.id)
            .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
            .where(
                FormSchema.insurance_type == INSURANCE_TYPE,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one()


async def test_seed_binds_published_schema_and_is_idempotent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_prompts: None,
) -> None:
    # Schemas first (the prompt binds to the schema's published version).
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
        await _seed_prompts(session)

    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)  # one prompt, one version, published
        version = (
            await session.execute(
                select(PromptVersion)
                .join(Prompt, PromptVersion.prompt_id == Prompt.id)
                .join(FormSchema, Prompt.schema_id == FormSchema.id)
                .where(FormSchema.insurance_type == INSURANCE_TYPE)
            )
        ).scalar_one()
        assert version.version == 1
        # Bound to the target schema's published version.
        assert version.schema_version_id == await _published_schema_version_id(session)

    # Re-run with unchanged JSON: no new version, no duplicate prompt.
    async with admin_sessionmaker() as session, session.begin():
        await _seed_prompts(session)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (1, 1, 1)


async def test_skips_when_no_published_schema(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_prompts: None,
) -> None:
    # A schema family with no PUBLISHED version → nothing for the generated
    # prompt to bind to. The seed must skip with a warning rather than crash,
    # and create no prompt rows for that family.
    async with admin_sessionmaker() as session, session.begin():
        exists = (
            await session.execute(
                select(FormSchema).where(FormSchema.insurance_type == INSURANCE_TYPE)
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(FormSchema(insurance_type=INSURANCE_TYPE, name="Infertility"))
    async with admin_sessionmaker() as session, session.begin():
        summary = await _seed_prompts(session)
    assert any(line.startswith(INSURANCE_TYPE) and "skipped" in line for line in summary)
    async with admin_sessionmaker() as session:
        assert await _counts(session) == (0, 0, 0)


async def test_second_published_version_rejected(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_prompts: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
        await _seed_prompts(session)

    with pytest.raises(IntegrityError):
        async with admin_sessionmaker() as session, session.begin():
            prompt = (
                await session.execute(
                    select(Prompt)
                    .join(FormSchema, Prompt.schema_id == FormSchema.id)
                    .where(FormSchema.insurance_type == INSURANCE_TYPE)
                )
            ).scalar_one()
            session.add(
                PromptVersion(
                    prompt_id=prompt.id,
                    schema_version_id=await _published_schema_version_id(session),
                    version=2,
                    composite_json={"name": "dupe"},
                    status=VersionStatus.PUBLISHED,
                )
            )
            await session.flush()
