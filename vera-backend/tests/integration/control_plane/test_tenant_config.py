"""Integration tests for the tenant runtime-config surface (persona_tweak) over a
live RLS-enforcing connection. The `admin` persona holds TENANT_ADMIN (which
includes `tenant:config:manage`); `norole` holds nothing."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.models import AuthAuditLog

PERSONA_PATH = "/api/v1/tenant/config/persona"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_get_persona_defaults_to_empty(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(PERSONA_PATH, headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    assert resp.json()["data"] == {"extra_instructions": None, "greeting": None}


async def test_put_then_get_round_trip(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    body = {"extra_instructions": "Confirm member ID twice.", "greeting": "Hello there."}
    put = await client.put(PERSONA_PATH, json=body, headers=_auth(rbac_world.admin_token))
    assert put.status_code == 200
    assert put.json()["data"] == body
    got = await client.get(PERSONA_PATH, headers=_auth(rbac_world.admin_token))
    assert got.json()["data"] == body


async def test_put_empty_body_is_noop(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    # The empty tweak ({} column default) round-trips to the all-None base persona.
    put = await client.put(PERSONA_PATH, json={}, headers=_auth(rbac_world.admin_token))
    assert put.status_code == 200
    assert put.json()["data"] == {"extra_instructions": None, "greeting": None}
    got = await client.get(PERSONA_PATH, headers=_auth(rbac_world.admin_token))
    assert got.json()["data"] == {"extra_instructions": None, "greeting": None}


async def test_put_rejects_unknown_key(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.put(
        PERSONA_PATH, json={"tone": "formal"}, headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 422


async def test_put_audits_field_names_not_values(
    client: httpx.AsyncClient, rbac_world: RBACWorld, admin_session: AsyncSession
) -> None:
    # Unique sentinels so the "values never recorded" check can't collide with the
    # config values other tests in this shared world write.
    instr = "SENTINEL-instr-9f3a confirm member ID twice"
    greet = "SENTINEL-greet-9f3a hello there"
    put = await client.put(
        PERSONA_PATH,
        json={"extra_instructions": instr, "greeting": greet},
        headers=_auth(rbac_world.admin_token),
    )
    assert put.status_code == 200

    rows = list(
        (
            await admin_session.execute(  # superuser read bypasses the WORM SELECT-only RLS
                select(AuthAuditLog).where(
                    AuthAuditLog.tenant_id == rbac_world.tenant_id,
                    AuthAuditLog.event_type == "persona_tweak_updated",
                )
            )
        ).scalars()
    )
    # The mutation is recorded with field NAMES only.
    assert any(
        r.meta == {"fields": ["extra_instructions", "greeting"]} and r.app_user_id is not None
        for r in rows
    )
    # No audit row ever carries the field VALUES.
    blob = "".join(str(r.meta) for r in rows)
    assert "SENTINEL-instr" not in blob
    assert "SENTINEL-greet" not in blob


async def test_requires_permission(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    assert (
        await client.get(PERSONA_PATH, headers=_auth(rbac_world.norole_token))
    ).status_code == 403
    assert (
        await client.put(PERSONA_PATH, json={}, headers=_auth(rbac_world.norole_token))
    ).status_code == 403
