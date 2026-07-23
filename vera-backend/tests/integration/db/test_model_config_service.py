"""DB-backed coverage for model_config.py's get/save/reset/history and the dispatch
metadata helper — against real Postgres.
"""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import VoiceModelConfig
from vera_core.services import model_config
from vera_core.services.model_config import (
    add_llm_model_override_metadata,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)


@pytest.fixture(autouse=True)
async def cleanup(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    yield
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_get_active_returns_none_when_never_set(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s:
        assert await get_active_llm_config(s) is None


async def test_save_then_get_active_returns_the_saved_row(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        saved = await save_llm_model(s, " gemini-3.5-flash ", created_by_user_id=None)
        assert saved.model == "gemini-3.5-flash"
        assert saved.provider == "google"

    async with admin_sessionmaker() as s:
        current = await get_active_llm_config(s)
        assert current is not None
        assert current.model == "gemini-3.5-flash"


async def test_reset_when_never_set_is_a_noop(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        assert await reset_llm_model(s, created_by_user_id=None) is None


async def test_reset_after_save_clears_and_history_shows_it(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        await save_llm_model(s, "gemini-3.5-flash", created_by_user_id=None)
    async with admin_sessionmaker() as s, s.begin():
        reset_row = await reset_llm_model(s, created_by_user_id=None)
        assert reset_row is not None
        assert reset_row.model is None

    async with admin_sessionmaker() as s:
        assert await get_active_llm_config(s) is None  # newest row has model=None
        history = await list_llm_config_history(s)
        assert len(history) == 2
        assert history[0].model is None  # newest first: the reset
        assert history[1].model == "gemini-3.5-flash"


async def test_add_llm_model_override_metadata_sets_key_when_active(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        await save_llm_model(s, "gemini-3.6-flash", created_by_user_id=None)

    async with admin_sessionmaker() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {"llm_model_override": "gemini-3.6-flash"}


async def test_add_llm_model_override_metadata_omits_key_when_unset(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s:
        metadata: dict[str, object] = {}
        await add_llm_model_override_metadata(s, metadata)
        assert metadata == {}


async def test_add_llm_model_override_metadata_degrades_to_default_on_read_failure(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A broken config-table read must never block a call from being placed — it degrades
    # to the same missing-key ("use the hardcoded default") behavior, not an exception.
    async with admin_sessionmaker() as s:
        metadata: dict[str, object] = {}
        with patch.object(model_config, "get_active_llm_config", side_effect=RuntimeError("boom")):
            await add_llm_model_override_metadata(s, metadata)
        assert metadata == {}
