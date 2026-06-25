"""Integration tests for the Voice Lab session endpoint.

No DB rows are written (the endpoint is persistence-free); the FakeLiveKit
injected by the authz_app fixture records room/dispatch/SIP calls so we assert on
the seam without a real LiveKit server.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from control_plane.deps import get_settings_state
from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.config import Settings
from vera_core.db import uuid7
from vera_core.observability.correlation import parse_room_name, room_name_for_call


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def trunk_configured(authz_app: FastAPI) -> Iterator[None]:
    """Override the app settings so the outbound SIP trunk reads as configured."""
    configured = Settings(_env_file=None, livekit_sip_trunk_id="ST_test_trunk")
    authz_app.dependency_overrides[get_settings_state] = lambda: configured
    try:
        yield
    finally:
        authz_app.dependency_overrides.pop(get_settings_state, None)


@pytest.mark.asyncio
async def test_browser_session_returns_caller_token_with_wait_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    before = len(fake_livekit.created)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["mode"] == "browser"
    assert parse_room_name(body["room_name"]) is not None
    # browser speaker identity + wait_for_speaker dispatch metadata
    assert body["token"].startswith(f"faketoken:{body['room_name']}:caller-")
    assert fake_livekit.created[before] == body["room_name"]
    assert fake_livekit.dispatch_metadata[before] == {
        "wait_for_speaker": True,
        "publish_transcript": True,
        "ivr_navigation": False,
    }


@pytest.mark.asyncio
async def test_ivr_navigation_flag_rides_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    before = len(fake_livekit.created)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser", "ivr_navigation": True},
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[before]
    assert meta is not None
    assert meta["ivr_navigation"] is True


@pytest.mark.asyncio
async def test_outbound_without_trunk_configured_returns_409(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_outbound_with_invalid_phone_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    trunk_configured: None,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "not-a-number"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_outbound_with_trunk_and_valid_phone_places_sip_call(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_configured: None,
) -> None:
    before = len(fake_livekit.sip_calls)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["mode"] == "outbound"
    # listen-only monitor identity for the browser
    assert body["token"].startswith(f"faketoken:{body['room_name']}:monitor-")
    assert fake_livekit.sip_calls[before] == (body["room_name"], "+15551234567")


@pytest.mark.asyncio
async def test_end_session_deletes_the_room(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    started = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser"},
    )
    room_name = started.json()["data"]["room_name"]

    before = len(fake_livekit.deleted)
    resp = await client.delete(
        f"/api/v1/voice-lab/sessions/{room_name}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert fake_livekit.deleted[before] == room_name


@pytest.mark.asyncio
async def test_end_session_foreign_tenant_room_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    # A room name carrying another tenant's uuid must not be deletable.
    foreign_room = room_name_for_call(rbac_world.other_tenant_id, uuid7())
    resp = await client.delete(
        f"/api/v1/voice-lab/sessions/{foreign_room}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_end_session_malformed_room_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.delete(
        "/api/v1/voice-lab/sessions/not-a-room",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_voice_lab_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/voice-lab/sessions", json={"mode": "browser"})
    assert resp.status_code == 401

    ended = await client.delete("/api/v1/voice-lab/sessions/call--x--y")
    assert ended.status_code == 401
