"""The app must boot cleanly whether or not the worker-event consumer starts.

When livekit_url is unset (tests/local without SIP), the consumer is not started,
so no Redis stream connection is attempted during app startup. When livekit_url
IS set and a LiveKit gateway is available, the consumer must be constructed and
started. Both directions are proven by spying on the constructor rather than by
side-channel evidence, so a regression in the gate condition actually fails the
test.

Note: `httpx.ASGITransport` never sends ASGI lifespan events — it only relays
"http" scopes — so a bare request through it does NOT run `create_app`'s
lifespan (startup never fires; `app.state` stays unpopulated). Driving the
lifespan explicitly via `app.router.lifespan_context(app)` (as the real
integration fixtures in conftest.py do) is required to actually exercise the
consumer gate.
"""

from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

import control_plane.main as control_plane_main
from control_plane.livekit_gateway import LiveKitGateway
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS

# LocalDevKMS just needs *a* 32-byte key; injected directly so these tests never
# depend on the LOCAL_KMS_MASTER_KEY env var (see control_plane/CLAUDE.md).
_KMS = LocalDevKMS(master_key=b"a" * 32)


class _SpyWorkerEventConsumer:
    """Stands in for WorkerEventConsumer: records construction, never touches Redis."""

    instances: ClassVar[list["_SpyWorkerEventConsumer"]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _SpyWorkerEventConsumer.instances.append(self)

    async def run(self) -> None:
        """No-op: returns immediately so `asyncio.create_task(consumer.run())` never
        performs real Redis I/O (no XREADGROUP, no blocking)."""
        return


class _SpyPipelineSweeper:
    """Stands in for PipelineSweeper: records construction, never touches the DB."""

    instances: ClassVar[list["_SpyPipelineSweeper"]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _SpyPipelineSweeper.instances.append(self)

    async def run(self) -> None:
        """No-op: returns immediately so `asyncio.create_task(sweeper.run())` never
        performs real DB/LiveKit I/O."""
        return


@pytest.fixture(autouse=True)
def spy_consumer(monkeypatch: pytest.MonkeyPatch) -> list[_SpyWorkerEventConsumer]:
    _SpyWorkerEventConsumer.instances = []
    monkeypatch.setattr(control_plane_main, "WorkerEventConsumer", _SpyWorkerEventConsumer)
    return _SpyWorkerEventConsumer.instances


@pytest.fixture(autouse=True)
def spy_sweeper(monkeypatch: pytest.MonkeyPatch) -> list[_SpyPipelineSweeper]:
    _SpyPipelineSweeper.instances = []
    monkeypatch.setattr(control_plane_main, "PipelineSweeper", _SpyPipelineSweeper)
    return _SpyPipelineSweeper.instances


async def _boot_and_get(app: Any, path: str) -> int:
    """Drive the real lifespan (startup + shutdown), then issue one request."""
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.get(path)
    return resp.status_code


@pytest.mark.asyncio
async def test_app_boots_without_consumer_when_livekit_unset(
    spy_consumer: list[_SpyWorkerEventConsumer],
    spy_sweeper: list[_SpyPipelineSweeper],
) -> None:
    app = control_plane_main.create_app(
        settings=Settings(_env_file=None, livekit_url=None), kms=_KMS
    )
    status_code = await _boot_and_get(app, "/does-not-exist")
    assert status_code == 404
    assert spy_consumer == []
    assert spy_sweeper == []


@pytest.mark.asyncio
async def test_app_starts_consumer_when_livekit_configured(
    spy_consumer: list[_SpyWorkerEventConsumer],
    spy_sweeper: list[_SpyPipelineSweeper],
) -> None:
    settings = Settings(_env_file=None, livekit_url="ws://fake:7880")
    stub_livekit = LiveKitGateway(url="ws://fake:7880", api_key="fake", api_secret="fake")
    app = control_plane_main.create_app(settings=settings, kms=_KMS, livekit=stub_livekit)
    status_code = await _boot_and_get(app, "/does-not-exist")
    assert status_code == 404
    assert len(spy_consumer) == 1
    assert len(spy_sweeper) == 1
