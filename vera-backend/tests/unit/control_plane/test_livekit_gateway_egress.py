"""Egress wrapper shapes: audio-only room composite → GCS, status mapping.

SDK deviation from brief: the installed livekit-api uses GCPUpload / field name
``gcp`` everywhere the brief writes GCSUpload / ``gcs``.  The test and
implementation both use the SDK's real names.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from livekit import api
from livekit.api.twirp_client import TwirpError

from control_plane.livekit_gateway import EgressStartError, LiveKitGateway


class _FakeEgress:
    def __init__(
        self,
        start_result: Any = None,
        list_items: list[Any] | None = None,
        raise_on_start: Exception | None = None,
        raise_on_stop: Exception | None = None,
    ) -> None:
        self.start_result = start_result
        self.list_items = list_items or []
        self.raise_on_start = raise_on_start
        self.raise_on_stop = raise_on_stop
        self.start_requests: list[Any] = []
        self.list_requests: list[Any] = []
        self.stopped: list[str] = []

    async def start_room_composite_egress(self, request: Any) -> Any:
        if self.raise_on_start:
            raise self.raise_on_start
        self.start_requests.append(request)
        return self.start_result

    async def list_egress(self, request: Any) -> Any:
        self.list_requests.append(request)
        return SimpleNamespace(items=self.list_items)

    async def stop_egress(self, request: Any) -> Any:
        if self.raise_on_stop:
            raise self.raise_on_stop
        self.stopped.append(request.egress_id)
        return SimpleNamespace()


def _gateway_with(egress: _FakeEgress) -> LiveKitGateway:
    gw = LiveKitGateway(url="ws://test", api_key="k", api_secret="s")

    @asynccontextmanager
    async def _client():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(egress=egress)

    gw._client = _client  # type: ignore[method-assign]
    return gw


async def test_start_room_audio_egress_shapes_request_and_returns_id() -> None:
    egress = _FakeEgress(start_result=SimpleNamespace(egress_id="EG_123"))
    gw = _gateway_with(egress)
    egress_id = await gw.start_room_audio_egress(
        "call--t--c", bucket="vera-recordings", object_path="recordings/t/c.ogg"
    )
    assert egress_id == "EG_123"
    req = egress.start_requests[0]
    assert req.room_name == "call--t--c"
    assert req.audio_only is True
    # SDK uses 'gcp' field (GCPUpload), not 'gcs' (GCSUpload) — see brief deviation note.
    assert req.file_outputs[0].gcp.bucket == "vera-recordings"
    assert req.file_outputs[0].filepath == "recordings/t/c.ogg"


async def test_start_failure_raises_domain_error() -> None:
    import aiohttp

    egress = _FakeEgress(raise_on_start=aiohttp.ClientError("boom"))
    gw = _gateway_with(egress)
    with pytest.raises(EgressStartError):
        await gw.start_room_audio_egress("r", bucket="b", object_path="p.ogg")


async def test_get_egress_status_maps_complete() -> None:
    item = SimpleNamespace(
        status=api.EgressStatus.EGRESS_COMPLETE,
        file_results=[SimpleNamespace(duration=90_000_000_000, size=1234)],  # 90s in ns
    )
    gw = _gateway_with(_FakeEgress(list_items=[item]))
    state = await gw.get_egress_status("EG_123")
    assert state is not None
    assert state.complete and not state.failed
    assert state.duration_ms == 90_000
    assert state.size_bytes == 1234


async def test_get_egress_status_unknown_id_returns_none() -> None:
    gw = _gateway_with(_FakeEgress(list_items=[]))
    assert await gw.get_egress_status("EG_GONE") is None


async def test_get_egress_status_maps_failed() -> None:
    item = SimpleNamespace(
        status=api.EgressStatus.EGRESS_FAILED,
        file_results=[],
    )
    gw = _gateway_with(_FakeEgress(list_items=[item]))
    state = await gw.get_egress_status("EG_F")
    assert state is not None
    assert state.failed and not state.complete
    assert state.duration_ms is None
    assert state.size_bytes is None


async def test_list_active_egresses_filters_active_and_maps_room_and_start() -> None:
    items = [
        SimpleNamespace(egress_id="EG_1", room_name="call--t--c", started_at=5_000_000_000),
        SimpleNamespace(egress_id="EG_2", room_name="call--t--d", started_at=0),
    ]
    egress = _FakeEgress(list_items=items)
    gw = _gateway_with(egress)
    active = await gw.list_active_egresses()
    # Only active egresses are requested (server-side filter).
    assert egress.list_requests[0].active is True
    assert [(e.egress_id, e.room_name, e.started_at_ms) for e in active] == [
        ("EG_1", "call--t--c", 5_000),  # ns → ms
        ("EG_2", "call--t--d", None),  # 0 → not-yet-reported
    ]


async def test_stop_egress_sends_request() -> None:
    egress = _FakeEgress()
    gw = _gateway_with(egress)
    await gw.stop_egress("EG_9")
    assert egress.stopped == ["EG_9"]


async def test_stop_egress_swallows_not_found() -> None:
    egress = _FakeEgress(raise_on_stop=TwirpError(code="not_found", msg="gone", status=404))
    gw = _gateway_with(egress)
    # Idempotent: stopping an already-gone egress is a no-op, not a raise.
    await gw.stop_egress("EG_GONE")
