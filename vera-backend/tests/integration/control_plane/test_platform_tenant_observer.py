"""Integration tests for the platform tenant AI form-filling (observer) toggle
(`GET /platform/tenants` + `POST /platform/tenants/{id}/observer`). Follows the
World/_mint local-fixture convention from test_platform_users.py — there is no shared
platform-tier conftest fixture in this repo yet."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, Tenant, UserRole

# No `pytestmark = pytest.mark.anyio`: this repo is asyncio-only (asyncio_mode="auto");
# anyio is a transitive dep, never a marker (see test_platform_users.py / repo CLAUDE.md).


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@dataclass
class ObserverWorld:
    super_admin_token: str
    tenant_id: UUID


async def _mint_platform(store: InMemorySessionStore, *, user_id: UUID, email: str) -> str:
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=None,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="platform",
            tenant_slug=None,
        ),
        3600,
        3600,
    )


@pytest.fixture
async def observer_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, ObserverWorld]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    super_id = uuid7()
    tenant_id = uuid7()

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        s.add(
            AppUser(
                id=super_id,
                tenant_id=None,
                account_type="platform",
                email="root@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(Tenant(id=tenant_id, name="Acme Health", slug=f"acme-{tenant_id.hex[:8]}"))
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))

    store = InMemorySessionStore()
    token = await _mint_platform(store, user_id=super_id, email="root@vera.example")

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=store,
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, ObserverWorld(super_admin_token=token, tenant_id=tenant_id)

    async with sm() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM auth_audit_log WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_role WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(text("DELETE FROM app_user WHERE account_type = 'platform'"))
        await s.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tenant_id})
    await engine.dispose()


async def test_list_tenants_defaults_observer_enabled_true(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world
    resp = await client.get("/api/v1/platform/tenants", headers=_auth(world.super_admin_token))
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["data"] if r["id"] == str(world.tenant_id))
    assert row["observer_enabled"] is True


async def test_toggle_observer_off_then_on_persists(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world

    off = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"enabled": False},
    )
    assert off.status_code == 200, off.text
    assert off.json()["data"]["observer_enabled"] is False

    # The write went through the SECURITY DEFINER fn; a fresh read reflects it.
    listed = await client.get("/api/v1/platform/tenants", headers=_auth(world.super_admin_token))
    row = next(r for r in listed.json()["data"] if r["id"] == str(world.tenant_id))
    assert row["observer_enabled"] is False

    on = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"enabled": True},
    )
    assert on.status_code == 200, on.text
    assert on.json()["data"]["observer_enabled"] is True


async def test_toggle_unknown_tenant_returns_404(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{uuid7()}/observer",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"enabled": False},
    )
    assert resp.status_code == 404, resp.text
