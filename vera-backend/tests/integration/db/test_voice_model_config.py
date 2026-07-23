"""DB-level coverage for voice_model_config: the stage / model-provider-pair CHECK
constraints, and "current effective value = newest row per stage" query behavior.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import VoiceModelConfig


@pytest.fixture(autouse=True)
async def cleanup(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    yield
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_rejects_unknown_stage(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(VoiceModelConfig(stage="bogus", provider="google", model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_rejects_model_without_provider(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_allows_explicit_reset_row(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model=None))
        await s.flush()  # no IntegrityError — both-null is the valid "reset" pairing


async def test_newest_row_per_stage_is_the_current_value(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-2.5-flash"))
    async with admin_sessionmaker() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-3.5-flash"))

    async with admin_sessionmaker() as s:
        current = (
            await s.execute(
                select(VoiceModelConfig)
                .where(VoiceModelConfig.stage == "llm")
                .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert current.model == "gemini-3.5-flash"


async def test_extra_config_column_stores_arbitrary_json(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        row = VoiceModelConfig(
            stage="llm",
            provider="google",
            model="gemini-3.5-flash",
            extra_config={"thinking_level": "low"},
        )
        s.add(row)
        await s.flush()
        row_id = row.id

    async with admin_sessionmaker() as s:
        fetched = (
            await s.execute(select(VoiceModelConfig).where(VoiceModelConfig.id == row_id))
        ).scalar_one()
        assert fetched.extra_config == {"thinking_level": "low"}


async def test_extra_config_defaults_to_null(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        row = VoiceModelConfig(stage="llm", provider="google", model="gemini-2.5-flash")
        s.add(row)
        await s.flush()
        row_id = row.id

    async with admin_sessionmaker() as s:
        fetched = (
            await s.execute(select(VoiceModelConfig).where(VoiceModelConfig.id == row_id))
        ).scalar_one()
        assert fetched.extra_config is None
