"""Integration tests for the Voice Lab session endpoint.

No DB rows are written (the endpoint is persistence-free); the FakeLiveKit
injected by the authz_app fixture records room/dispatch/SIP calls so we assert on
the seam without a real LiveKit server.
"""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.integrations.credentials import seal_credentials
from vera_core.models import Integration, IntegrationType
from vera_core.observability.correlation import parse_room_name, room_name_for_call

_TRUNK_TYPE = "livekit_outbound_trunk_id"
_TRUNK_VALUE = "ST_test_trunk"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def trunk_configured(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    trunk_integration_type: None,
) -> None:
    """Seal a trunk credential for the test tenant so the outbound dial resolves it from
    the DB. Uses the same LocalDevKMS master key as the app under test, so the app's
    get_integration_credentials can open what we seal here. The `trunk_integration_type`
    fixture owns the catalog-type row and tears down the Integration we add below."""
    kms = LocalDevKMS(master_key=b"a" * 32)
    async with admin_sessionmaker() as session, session.begin():
        type_id = (
            await session.execute(
                select(IntegrationType.id).where(IntegrationType.name == _TRUNK_TYPE)
            )
        ).scalar_one()
        integration = Integration(
            tenant_id=rbac_world.tenant_id,
            integration_type_id=type_id,
            status="active",
        )
        await seal_credentials(kms, integration=integration, credentials={"trunk_id": _TRUNK_VALUE})
        session.add(integration)


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
        "enable_ivr_navigation": False,
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
        json={"mode": "browser", "enable_ivr_navigation": True},
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[before]
    assert meta is not None
    assert meta["enable_ivr_navigation"] is True


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
    assert fake_livekit.sip_calls[before] == (body["room_name"], "+15551234567", _TRUNK_VALUE)


@pytest.mark.asyncio
async def test_outbound_dial_failure_returns_502_and_tears_down_room(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_configured: None,
) -> None:
    # The trunk is stored (passed save-time validation) but the dial fails at the
    # provider seam — e.g. the trunk was deleted afterwards. Expect a clean 502, not a
    # 500, and the room we created must be torn down so no agent is left orphaned.
    fake_livekit.dial_error = True
    before_deleted = len(fake_livekit.deleted)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 502, resp.text
    assert len(fake_livekit.deleted) == before_deleted + 1


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
