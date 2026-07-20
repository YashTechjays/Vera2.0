import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.call_stream import CallStreamService
from vera_core.db import uuid7
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import ROLE_AGENT, ROLE_USER


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_stream_replays_then_ends(
    client: httpx.AsyncClient, rbac_world: RBACWorld, call_stream_service: CallStreamService
) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    await call_stream_service.publish_turn(room, ROLE_USER, "hi", ts=1)
    await call_stream_service.publish_turn(room, ROLE_AGENT, "hello", ts=2)
    await call_stream_service.end(room)

    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert '"text":"hi"' in body and '"text":"hello"' in body
    assert body.index('"text":"hi"') < body.index('"text":"hello"')


@pytest.mark.asyncio
async def test_stream_requires_auth(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    resp = await client.get(f"/api/v1/voice-lab/sessions/{room}/transcript")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_requires_permission(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_stream_foreign_tenant_room_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    foreign = room_name_for_call(rbac_world.other_tenant_id, uuid7())
    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{foreign}/transcript", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_access_is_audited(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    call_stream_service: CallStreamService,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    await call_stream_service.end(room)
    await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.admin_token)
    )
    async with admin_sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT decision FROM audit_log WHERE event_type='phi.access' "
                    "AND resource_type='transcript' AND resource_id=:r"
                ).bindparams(r=room)
            )
        ).first()
    assert row is not None and row[0] == "allow"
