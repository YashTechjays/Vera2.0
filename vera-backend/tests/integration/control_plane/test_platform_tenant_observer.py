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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, Role, RolePermission, Tenant, UserRole
from vera_core.models.enums import AuthEvent

# No `pytestmark = pytest.mark.anyio`: this repo is asyncio-only (asyncio_mode="auto");
# anyio is a transitive dep, never a marker (see test_platform_users.py / repo CLAUDE.md).


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@dataclass
class ObserverWorld:
    super_admin_token: str
    # A platform operator holding ONLY platform:elevations:read — the caller the split
    # between that permission and platform:tenants:manage exists for.
    elevations_only_token: str
    tenant_id: UUID
    # The fixture's privileged (non-RLS) sessionmaker, so a test can read the audit log
    # without standing up a second engine.
    sm: async_sessionmaker[AsyncSession]


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
    elevations_id = uuid7()
    tenant_id = uuid7()

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        # A global role carrying platform:elevations:read and nothing else, so the two
        # permissions are actually held apart (on the seeded SUPER_ADMIN they coincide).
        narrow_role = Role(tenant_id=None, name="TEST_ELEVATIONS_ONLY")
        s.add(narrow_role)
        for user_id, email, name in (
            (super_id, "root@vera.example", "Root"),
            (elevations_id, "elev@vera.example", "Elevations Only"),
        ):
            s.add(
                AppUser(
                    id=user_id,
                    tenant_id=None,
                    account_type="platform",
                    email=email,
                    name=name,
                    status="active",
                )
            )
        s.add(Tenant(id=tenant_id, name="Acme Health", slug=f"acme-{tenant_id.hex[:8]}"))
        await s.flush()
        s.add(
            RolePermission(
                tenant_id=None,
                role_id=narrow_role.id,
                permission_id=permission_ids["platform:elevations:read"],
            )
        )
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=None, app_user_id=elevations_id, role_id=narrow_role.id))

    store = InMemorySessionStore()
    token = await _mint_platform(store, user_id=super_id, email="root@vera.example")
    narrow_token = await _mint_platform(store, user_id=elevations_id, email="elev@vera.example")

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
            yield (
                client,
                ObserverWorld(
                    super_admin_token=token,
                    elevations_only_token=narrow_token,
                    tenant_id=tenant_id,
                    sm=sm,
                ),
            )

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
        # role_permission.role_id is ON DELETE CASCADE, so the grant goes with the role.
        await s.execute(
            text("DELETE FROM role WHERE tenant_id IS NULL AND name = 'TEST_ELEVATIONS_ONLY'")
        )
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


async def test_replayed_idempotency_key_conflicts(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world
    headers = {**_auth(world.super_admin_token), **_idem()}
    first = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers=headers,
        json={"enabled": False},
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers=headers,
        json={"enabled": False},
    )
    assert replay.status_code == 409, replay.text


async def test_toggle_writes_the_auth_audit_event(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text

    async with world.sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT tenant_id, metadata FROM auth_audit_log "
                    "WHERE event_type = :event ORDER BY seq DESC LIMIT 1"
                ),
                {"event": AuthEvent.TENANT_OBSERVER_UPDATED.value},
            )
        ).one()
    # Null-tenant, like every other /platform action; the target rides in the meta.
    assert row.tenant_id is None
    assert row.metadata["target_tenant"] == str(world.tenant_id)
    assert row.metadata["observer_enabled"] is False


async def test_elevations_read_only_caller_cannot_toggle(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    client, world = observer_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.tenant_id}/observer",
        headers={**_auth(world.elevations_only_token), **_idem()},
        json={"enabled": False},
    )
    assert resp.status_code == 403, resp.text


async def test_observer_flag_is_withheld_without_tenants_manage(
    observer_world: tuple[httpx.AsyncClient, ObserverWorld],
) -> None:
    """platform:elevations:read still lists tenants (the elevation picker needs it) but
    must not disclose the AI form-filling switch, which platform:tenants:manage governs."""
    client, world = observer_world

    narrow = await client.get(
        "/api/v1/platform/tenants", headers=_auth(world.elevations_only_token)
    )
    assert narrow.status_code == 200, narrow.text
    narrow_row = next(r for r in narrow.json()["data"] if r["id"] == str(world.tenant_id))
    assert narrow_row["observer_enabled"] is None

    wide = await client.get("/api/v1/platform/tenants", headers=_auth(world.super_admin_token))
    wide_row = next(r for r in wide.json()["data"] if r["id"] == str(world.tenant_id))
    assert wide_row["observer_enabled"] is True
