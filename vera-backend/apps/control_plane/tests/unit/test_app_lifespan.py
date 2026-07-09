"""Smoke test: create_app() factory and the post-call bus lifespan wiring.

Exercises the default TEST configuration (livekit_url unset) to confirm:
- create_app() builds without error.
- The lifespan enters/exits cleanly (no consumer started, no Redis/LiveKit needed).
- app.state.post_call_bus is set by the lifespan (even without LiveKit).

Backends that create_app() accepts as kwargs are injected as fakes; only the few
that must go through the module-level factories (engine + Redis pool) are patched.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_plane.main import create_app
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.events import PostCallJobBus


def _test_settings() -> Settings:
    """Minimal settings that don't require env vars or external services."""
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://fake:fake@localhost/fake",
        redis_url="redis://localhost:6379/0",
        livekit_url=None,  # consumer guard: disabled
        gcp_project=None,
        smtp_host="localhost",
        smtp_port=1025,
        email_from="noreply@example.com",
    )


def test_create_app_builds() -> None:
    """create_app() with a minimal settings object returns a FastAPI instance."""
    from fastapi import FastAPI

    app = create_app(_test_settings(), kms=LocalDevKMS(master_key=b"a" * 32))

    assert isinstance(app, FastAPI)


@pytest.mark.asyncio
async def test_lifespan_sets_post_call_bus_when_livekit_unset() -> None:
    """With livekit_url=None, the lifespan must set app.state.post_call_bus
    (an instance of PostCallJobBus) and NOT start the post-call consumer task."""
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    fake_redis = AsyncMock()
    fake_redis.aclose = AsyncMock()

    # Inject the backends create_app() accepts as kwargs; patch only the module-level
    # factories the lifespan calls directly (engine + Redis pool).
    app = create_app(
        _test_settings(),
        kms=LocalDevKMS(master_key=b"a" * 32),
        session_store=MagicMock(),
        permission_cache=MagicMock(),
        idempotency=MagicMock(),
        audit=MagicMock(),
        auth_audit=MagicMock(),
        email_sender=MagicMock(),
        invitation_store=MagicMock(),
        transcript_service=MagicMock(),
    )
    with (
        patch("control_plane.main.create_engine", return_value=fake_engine),
        patch("control_plane.main.create_sessionmaker", return_value=MagicMock()),
        patch("control_plane.main.create_redis", return_value=fake_redis),
    ):
        async with app.router.lifespan_context(app):
            # livekit_url is None → consumer not started, but bus MUST be set.
            assert hasattr(app.state, "post_call_bus")
            assert isinstance(app.state.post_call_bus, PostCallJobBus)
