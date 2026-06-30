"""Integration tests for the tenant integration-credential endpoint, focused on the
save-time upstream validation for `livekit_outbound_trunk_id`.

The FakeLiveKit injected by the authz_app fixture stands in for the LiveKit SIP
service: `known_trunks` decides whether a trunk id is recognised, and
`lookup_unavailable` simulates LiveKit being unreachable. The admin persona holds
TENANT_ADMIN, which includes `integrations:manage`.
"""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.models import Integration

_TRUNK_TYPE = "livekit_outbound_trunk_id"
_PATH = f"/api/v1/integrations/{_TRUNK_TYPE}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_configure_with_recognised_trunk_succeeds(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_integration_type: None,
) -> None:
    fake_livekit.known_trunks = {"ST_valid_trunk"}
    resp = await client.put(
        _PATH,
        headers=_auth(rbac_world.admin_token),
        json={"credentials": {"trunk_id": "ST_valid_trunk"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["integration_type"] == _TRUNK_TYPE
    assert body["configured"] is True


@pytest.mark.asyncio
async def test_configure_with_unknown_trunk_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_integration_type: None,
) -> None:
    # known_trunks is empty (reset_livekit_knobs) → the id is not recognised.
    resp = await client.put(
        _PATH,
        headers=_auth(rbac_world.admin_token),
        json={"credentials": {"trunk_id": "ST_bogus"}},
    )
    assert resp.status_code == 422, resp.text
    # The secret value is never echoed back; only the field name.
    assert "ST_bogus" not in resp.text


@pytest.mark.asyncio
async def test_configure_when_livekit_unreachable_returns_502(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_integration_type: None,
) -> None:
    fake_livekit.lookup_unavailable = True
    resp = await client.put(
        _PATH,
        headers=_auth(rbac_world.admin_token),
        json={"credentials": {"trunk_id": "ST_cannot_verify"}},
    )
    assert resp.status_code == 502, resp.text


@pytest.mark.asyncio
async def test_unknown_trunk_is_not_stored(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_integration_type: None,
    admin_session: AsyncSession,
) -> None:
    # A rejected (422) save must leave no Integration row behind — strict fail-closed.
    await client.put(
        _PATH,
        headers=_auth(rbac_world.admin_token),
        json={"credentials": {"trunk_id": "ST_bogus"}},
    )
    rows = (
        (
            await admin_session.execute(
                select(Integration).where(Integration.tenant_id == rbac_world.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
