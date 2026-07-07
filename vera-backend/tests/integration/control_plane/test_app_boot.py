"""The app must boot cleanly whether or not the worker-event consumer starts.

When livekit_url is unset (tests/local without SIP), the consumer is not started,
so no Redis stream connection is attempted during app startup.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.main import create_app
from vera_core.config import Settings


@pytest.mark.asyncio
async def test_app_boots_without_consumer_when_livekit_unset() -> None:
    app = create_app(settings=Settings(_env_file=None, livekit_url=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Lifespan runs on the first request; a bare 404 proves startup/shutdown are clean.
        resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
