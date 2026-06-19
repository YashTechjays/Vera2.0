"""End-to-end proof of the authz chain on the example endpoint:
401 (no/bad token) -> 403 missing permission (audited) -> 200 with allow audited.

The URL slug is no longer authoritative — tenant is derived from the session.
Cross-tenant URL access is no longer intercepted: the chain uses the session
tenant regardless of which slug appears in the path."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import AuditLog

from .conftest import RBACWorld


def _url(world: RBACWorld) -> str:
    return "/api/v1/calls"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _audit_rows(session: AsyncSession, world: RBACWorld, event_type: str) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(
            AuditLog.tenant_id == world.tenant_id, AuditLog.event_type == event_type
        )
    )
    return list(rows.scalars())


async def test_missing_token_is_401(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get(_url(rbac_world))
    assert resp.status_code == 401


async def test_unknown_token_is_401(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get(_url(rbac_world), headers=_auth("tok-bogus"))
    assert resp.status_code == 401


async def test_missing_permission_is_403_and_audited(
    client: httpx.AsyncClient, rbac_world: RBACWorld, admin_session: AsyncSession
) -> None:
    resp = await client.get(_url(rbac_world), headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403
    assert "calls:read" in resp.json()["message"]
    denies = await _audit_rows(admin_session, rbac_world, "authz.deny")
    assert any(r.permission_key == "calls:read" and r.reason == "not granted" for r in denies)


async def test_unknown_user_is_403(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get(_url(rbac_world), headers=_auth(rbac_world.ghost_token))
    assert resp.status_code == 403


async def test_allowed_request_succeeds_and_audits_allow(
    client: httpx.AsyncClient, rbac_world: RBACWorld, admin_session: AsyncSession
) -> None:
    resp = await client.get(_url(rbac_world), headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    assert resp.json()["data"] == []
    allows = await _audit_rows(admin_session, rbac_world, "authz.allow")
    assert any(
        r.permission_key == "calls:read" and r.decision == "allow" and r.actor_user_id is not None
        for r in allows
    )


async def test_healthz_needs_no_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
