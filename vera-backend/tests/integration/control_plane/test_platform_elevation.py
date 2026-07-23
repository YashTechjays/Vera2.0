"""HTTP-level tests for the platform runtime (ADR-0006 §A/§B): the elevation
endpoints, platform RBAC, and a SUPER_ADMIN's elevated access into one tenant.

The app talks to Postgres as the non-superuser RLS role, so the whole request path
— platform RBAC over a platform session, the SECURITY DEFINER write paths, and the
elevated tenant session — runs under real RLS. Platform operators authenticate via
minted sessions (GCIP login is ADR-0006 §D, deferred), mirroring rbac_world."""

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
from vera_core.models import AppUser, Tenant, UserRole


@dataclass
class World:
    tenant_id: UUID
    other_tenant_id: UUID
    super_user_id: UUID
    tenant_admin_id: UUID
    super_token: str
    tenant_admin_token: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _mint(
    store: InMemorySessionStore, *, user_id: UUID, tenant_id: UUID | None, email: str
) -> str:
    # Mint like production (sess + sess_abs companion) so /auth/me can read the
    # absolute-cap TTL; a bare put() would leave no sess_abs and 401 on /me.
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="tenant" if tenant_id is not None else "platform",
            # slug == UUID string for a tenant user (matches the fixture); None for a
            # platform operator with no home tenant.
            tenant_slug=str(tenant_id) if tenant_id is not None else None,
        ),
        3600,
        3600,
    )


@pytest.fixture
async def world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, other_tenant_id = uuid7(), uuid7()
    super_id, admin_id = uuid7(), uuid7()
    suffix = tenant_id.hex[:8]

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        # slug == UUID string so the UUID-in-URL test helpers resolve unchanged.
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"PE {suffix}", status="active"))
        s.add(
            Tenant(
                id=other_tenant_id,
                slug=str(other_tenant_id),
                name=f"PE other {suffix}",
                status="active",
            )
        )
        await s.flush()
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        admin_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
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
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email="ta@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))

    store = InMemorySessionStore()
    super_token = await _mint(store, user_id=super_id, tenant_id=None, email="root@vera.example")
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email="ta@tenant.example"
    )

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
                World(
                    tenant_id,
                    other_tenant_id,
                    super_id,
                    admin_id,
                    super_token,
                    admin_token,
                ),
            )

    async with sm() as s, s.begin():
        await s.execute(
            text("DELETE FROM tenant_elevation WHERE super_admin_user_id = :u").bindparams(
                u=super_id
            )
        )
        await s.execute(
            text("DELETE FROM auth_audit_log WHERE app_user_id IN (:s, :a)").bindparams(
                s=super_id, a=admin_id
            )
        )
        for tbl in ("audit_log", "user_role", "role_permission", "role"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t").bindparams(t=tenant_id))
        await s.execute(text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=super_id))
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:s, :a)").bindparams(s=super_id, a=admin_id)
        )
        await s.execute(
            text("DELETE FROM tenant WHERE id IN (:a, :b)").bindparams(
                a=tenant_id, b=other_tenant_id
            )
        )
    await engine.dispose()


_BASE = "/api/v1/platform/elevations"


async def _create(
    client: httpx.AsyncClient, w: World, *, tenant: UUID, minutes: int = 60
) -> httpx.Response:
    return await client.post(
        _BASE,
        headers=_auth(w.super_token),
        json={"target_tenant_id": str(tenant), "reason": "ticket #7", "duration_minutes": minutes},
    )


async def test_super_admin_creates_and_lists_elevation(
    world: tuple[httpx.AsyncClient, World], admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, w = world
    resp = await _create(client, w, tenant=w.tenant_id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "SUCCESS"
    grant_id = resp.json()["data"]["id"]

    listed = await client.get(_BASE, headers=_auth(w.super_token))
    assert listed.status_code == 200
    assert grant_id in [g["id"] for g in listed.json()["data"]]

    # The grant is recorded in the auth audit as a null-tenant platform event.
    async with admin_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT tenant_id, event_type FROM auth_audit_log "
                    "WHERE app_user_id = :u AND event_type = 'tenant_elevation_granted'"
                ).bindparams(u=w.super_user_id)
            )
        ).one()
    assert row.tenant_id is None


async def test_tenant_admin_cannot_create_elevation(
    world: tuple[httpx.AsyncClient, World], admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, w = world
    resp = await client.post(
        _BASE,
        headers=_auth(w.tenant_admin_token),
        json={"target_tenant_id": str(w.tenant_id), "reason": "nope", "duration_minutes": 60},
    )
    assert resp.status_code == 403  # TENANT_ADMIN lacks platform:elevations:create
    async with admin_sessionmaker() as s:
        denies = (
            await s.execute(
                text(
                    "SELECT count(*) FROM auth_audit_log "
                    "WHERE app_user_id = :u AND event_type = 'authz_deny'"
                ).bindparams(u=w.tenant_admin_id)
            )
        ).scalar_one()
    assert denies >= 1


async def test_end_elevation_then_gone(world: tuple[httpx.AsyncClient, World]) -> None:
    client, w = world
    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    ended = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert ended.status_code == 200

    listed = await client.get(_BASE, headers=_auth(w.super_token))
    assert grant_id not in [g["id"] for g in listed.json()["data"]]

    again = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert again.status_code == 404


async def test_create_validation_and_conflicts(world: tuple[httpx.AsyncClient, World]) -> None:
    client, w = world
    over_cap = await _create(client, w, tenant=w.tenant_id, minutes=10_000)
    assert over_cap.status_code == 422  # exceeds MAX_ELEVATION_MINUTES (pydantic)

    unknown = await _create(client, w, tenant=uuid7())
    assert unknown.status_code == 404  # FK → unknown tenant

    first = await _create(client, w, tenant=w.tenant_id)
    assert first.status_code == 201
    second = await _create(client, w, tenant=w.other_tenant_id)
    assert second.status_code == 409  # one active grant per operator


async def test_elevated_request_reaches_tenant_and_stamps_audit(
    world: tuple[httpx.AsyncClient, World], admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, w = world
    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    # With an active grant, the platform token reaches the tenant route under RLS.
    ok = await client.get("/api/v1/calls", headers=_auth(w.super_token))
    assert ok.status_code == 200, ok.text

    # The authz.allow on that elevated read links back to the grant.
    async with admin_sessionmaker() as s:
        elev = (
            await s.execute(
                text(
                    "SELECT elevation_session_id FROM audit_log "
                    "WHERE tenant_id = :t AND event_type = 'authz.allow' "
                    "AND permission_key = 'calls:read'"
                ).bindparams(t=w.tenant_id)
            )
        ).scalar_one()
    assert str(elev) == grant_id


async def test_platform_user_without_grant_is_denied_on_tenant_route(
    world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = world
    resp = await client.get("/api/v1/calls", headers=_auth(w.super_token))
    assert resp.status_code == 403
    assert resp.json()["message"] == "no active elevation for tenant"


async def test_super_admin_lists_tenants(world: tuple[httpx.AsyncClient, World]) -> None:
    # The platform session reads the tenant catalog (tenant_platform_read policy)
    # without any elevation — it's the picker for choosing where to elevate.
    client, w = world
    resp = await client.get("/api/v1/platform/tenants", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["data"]}
    assert {str(w.tenant_id), str(w.other_tenant_id)} <= ids


async def test_tenant_user_cannot_list_tenants(world: tuple[httpx.AsyncClient, World]) -> None:
    client, w = world
    resp = await client.get("/api/v1/platform/tenants", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403


async def test_auth_me_reflects_active_elevation(world: tuple[httpx.AsyncClient, World]) -> None:
    # The frontend gates the tenant-scoped sidebar on this field, so /auth/me must
    # report the operator's active grant (and nothing before they elevate).
    client, w = world
    before = await client.get("/api/v1/auth/me", headers=_auth(w.super_token))
    assert before.json()["data"]["active_elevation"] is None

    await _create(client, w, tenant=w.tenant_id)
    after = await client.get("/api/v1/auth/me", headers=_auth(w.super_token))
    elevation = after.json()["data"]["active_elevation"]
    assert elevation is not None
    assert elevation["target_tenant_id"] == str(w.tenant_id)


async def test_elevated_operator_manages_roles_like_a_tenant_admin(
    world: tuple[httpx.AsyncClient, World],
) -> None:
    # DECISION (RBAC tickets): platform admins get NO parallel role API — they
    # elevate, then drive the same tenant endpoints under that tenant's RLS.
    client, w = world

    before = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert before.status_code == 403  # no active elevation → no tenant access

    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(w.super_token), **_idem()},
        json={"name": "ELEVATED_MADE", "description": "made under elevation", "permission_ids": []},
    )
    assert created.status_code == 200, created.text
    role_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(w.super_token), **_idem()},
        json={"description": "edited under elevation"},
    )
    assert patched.status_code == 200, patched.text

    perms = await client.get("/api/v1/permissions", headers=_auth(w.super_token))
    assert perms.status_code == 200
    assert not any(p["code"].startswith("platform:") for p in perms.json()["data"])

    deleted = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers={**_auth(w.super_token), **_idem()}
    )
    assert deleted.status_code == 200, deleted.text

    ended = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert ended.status_code == 200
    after = await client.get("/api/v1/roles", headers=_auth(w.super_token))
    assert after.status_code == 403  # elevation over → access gone again


async def test_elevated_super_admin_still_reaches_platform_tier_endpoints(
    world: tuple[httpx.AsyncClient, World],
) -> None:
    """An active elevation grant changes the DB session's tenant GUC for /api/v1/*
    tenant routes (`tenant_context` / `tenant_scoped_session`), but it does NOT
    change the caller's `account_type` — the identity resolved from the bearer
    token is still `'platform'`. `platform_require` (and the `platform_scoped_session`
    it depends on) never consults `tenant_context` or the elevation grant at all —
    it resolves permissions straight from `current_identity` over a platform
    session. So a SUPER_ADMIN who is mid-elevation into a tenant is still, and
    correctly, allowed to reach platform-tier endpoints like GET /platform/users;
    elevation only ADDS tenant access, it never subtracts platform access."""
    client, w = world
    grant_id = (await _create(client, w, tenant=w.tenant_id)).json()["data"]["id"]

    resp = await client.get("/api/v1/platform/users", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    emails = {row["email"] for row in resp.json()["data"]}
    assert "root@vera.example" in emails  # the elevated super admin sees itself listed

    ended = await client.post(f"{_BASE}/{grant_id}/end", headers=_auth(w.super_token))
    assert ended.status_code == 200
