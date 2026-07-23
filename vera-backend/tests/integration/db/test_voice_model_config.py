"""DB-level coverage for voice_model_config: the stage / model-provider-pair CHECK
constraints, and "current effective value = newest row per stage" query behavior.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vera_core.models import VoiceModelConfig


@pytest.fixture
async def sm(database_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def cleanup(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[None]:
    yield
    async with sm() as s, s.begin():
        await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_rejects_unknown_stage(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="bogus", provider="google", model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_rejects_model_without_provider(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model="gemini-2.5-flash"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_allows_explicit_reset_row(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider=None, model=None))
        await s.flush()  # no IntegrityError — both-null is the valid "reset" pairing


async def test_newest_row_per_stage_is_the_current_value(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-2.5-flash"))
    async with sm() as s, s.begin():
        s.add(VoiceModelConfig(stage="llm", provider="google", model="gemini-3.5-flash"))

    async with sm() as s:
        current = (
            await s.execute(
                select(VoiceModelConfig)
                .where(VoiceModelConfig.stage == "llm")
                .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert current.model == "gemini-3.5-flash"
