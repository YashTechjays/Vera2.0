"""Confirms the active LLM model override rides Voice Lab's dispatch metadata (mirrors
test_ivr_playbooks.py's runtime-selection tests for add_active_playbook_metadata).
"""

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage

from .conftest import FakeLiveKit, RBACWorld


async def test_voice_lab_carries_active_llm_override_into_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(
            VoiceModelConfig(stage=VoiceModelStage.LLM, provider="google", model="gemini-3.5-flash")
        )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
            json={"mode": "browser", "enable_ivr_navigation": False},
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert meta["llm_model_override"] == "gemini-3.5-flash"
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))


async def test_voice_lab_omits_llm_model_override_when_never_set(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"mode": "browser", "enable_ivr_navigation": False},
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[-1]
    assert meta is not None
    assert "llm_model_override" not in meta


async def test_voice_lab_carries_thinking_override_into_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        s.add(
            VoiceModelConfig(
                stage=VoiceModelStage.LLM,
                provider="google",
                model="gemini-3.5-flash",
                extra_config={"thinking_level": "high"},
            )
        )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
            json={"mode": "browser", "enable_ivr_navigation": False},
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert meta["llm_thinking_override"] == {"thinking_level": "high"}
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(delete(VoiceModelConfig).where(VoiceModelConfig.stage == "llm"))
