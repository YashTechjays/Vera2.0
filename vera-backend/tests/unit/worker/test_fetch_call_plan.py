"""fetch_call_plan fail-safe contract: any problem (no plan, Redis down) → None
and the entrypoint falls back to the static persona."""

import pytest

from agent_worker.main import fetch_call_plan
from vera_core.config.settings import Settings


@pytest.mark.asyncio
async def test_redis_unreachable_falls_back() -> None:
    # A closed port: connection fails fast; the helper must swallow and fall back.
    settings = Settings(redis_url="redis://127.0.0.1:1/0")
    assert await fetch_call_plan(settings, "call--t--c") is None
