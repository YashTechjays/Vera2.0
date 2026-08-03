"""Integration tests for platform tenant CRUD (VR2-30): create, read one, the widened
list-status filter, edit, and deactivate/reactivate. Follows the World/_mint local-fixture
convention from test_platform_tenant_observer.py — there is no shared platform-tier
conftest fixture."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.password import hash_password
from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import (
    AppUser,
    Role,
    RolePermission,
    SsoProvider,
    Tenant,
    UserIdentity,
    UserRole,
)
from vera_core.models.enums import AuthEvent, ProviderKind

TENANT_ADMIN_EMAIL = "tadmin-crud@vera.example"
TENANT_ADMIN_PASSWORD = "correct horse battery staple"
# Load-bearing in BOTH fixture setup and teardown: a drift between the two leaks the role
# and breaks the next test's setup on a duplicate name.
NARROW_ROLE_NAME = "TEST_TENANT_CRUD_ELEVATIONS_ONLY"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _latest_audit(
    sm: async_sessionmaker[AsyncSession], event: AuthEvent
) -> tuple[UUID | None, dict[str, Any]]:
    """Newest auth_audit_log row for `event` as (tenant_id, metadata); raises if none."""
    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT tenant_id, metadata FROM auth_audit_log "
                    "WHERE event_type = :event ORDER BY seq DESC LIMIT 1"
                ),
                {"event": event.value},
            )
        ).one()
    audit_tenant_id: UUID | None = row.tenant_id
    metadata: dict[str, Any] = row.metadata
    return audit_tenant_id, metadata


@dataclass
class TenantCrudWorld:
    super_admin_token: str
    # Holds ONLY platform:elevations:read — the caller the split with
    # platform:tenants:manage exists for.
    elevations_only_token: str
    # An ordinary tenant-tier caller, for the "not a platform session" checks.
    tenant_admin_token: str
    active_tenant_id: UUID
    active_tenant_slug: str
    deactivated_tenant_id: UUID
    # A global (tenant_id IS NULL) system role — the only kind assignable through the
    # platform tenant-user-invite path (see platform_tenant_users.py).
    tenant_admin_role_id: UUID
    super_admin_role_id: UUID
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
async def crud_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, TenantCrudWorld]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    super_id = uuid7()
    elevations_id = uuid7()
    tenant_admin_id = uuid7()
    active_tenant_id = uuid7()
    deactivated_tenant_id = uuid7()
    active_tenant_slug = f"acme-crud-{active_tenant_id.hex[:8]}"

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        tenant_admin_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        narrow_role = Role(tenant_id=None, name=NARROW_ROLE_NAME)
        s.add(narrow_role)
        for user_id, email, name in (
            (super_id, "root-crud@vera.example", "Root"),
            (elevations_id, "elev-crud@vera.example", "Elevations Only"),
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
        s.add(
            Tenant(
                id=active_tenant_id,
                name="Acme Health",
                slug=active_tenant_slug,
                status="active",
            )
        )
        s.add(
            Tenant(
                id=deactivated_tenant_id,
                name="Retired Co",
                slug=f"retired-crud-{deactivated_tenant_id.hex[:8]}",
                status="deactivated",
            )
        )
        # Flush the tenants before adding a row that references active_tenant_id: with no
        # declared relationship() SQLAlchemy batches same-class inserts by add() order, not
        # by FK dependency, so an unflushed app_user/provider insert can race ahead of its
        # tenant row.
        await s.flush()
        # A real password provider + credential, so the deactivate/reactivate tests can
        # prove the login-block claim by hitting the actual /auth/login route, not just
        # the resolver function it depends on.
        s.add(
            SsoProvider(
                tenant_id=active_tenant_id,
                provider_type=ProviderKind.PASSWORD.value,
                display_name="Password",
                enabled=True,
                enforce_mfa=False,
            )
        )
        s.add(
            AppUser(
                id=tenant_admin_id,
                tenant_id=active_tenant_id,
                account_type="tenant",
                email=TENANT_ADMIN_EMAIL,
                name="T Admin",
                status="active",
            )
        )
        await s.flush()
        s.add(
            UserIdentity(
                tenant_id=active_tenant_id,
                app_user_id=tenant_admin_id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=TENANT_ADMIN_EMAIL,
                email=TENANT_ADMIN_EMAIL,
                hashed_password=hash_password(TENANT_ADMIN_PASSWORD),
                mfa_enabled=False,
            )
        )
        s.add(
            RolePermission(
                tenant_id=None,
                role_id=narrow_role.id,
                permission_id=permission_ids["platform:elevations:read"],
            )
        )
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=None, app_user_id=elevations_id, role_id=narrow_role.id))
        s.add(
            UserRole(
                tenant_id=active_tenant_id,
                app_user_id=tenant_admin_id,
                role_id=tenant_admin_role,
            )
        )

    store = InMemorySessionStore()
    token = await _mint_platform(store, user_id=super_id, email="root-crud@vera.example")
    narrow_token = await _mint_platform(
        store, user_id=elevations_id, email="elev-crud@vera.example"
    )
    tenant_token = await store.mint_session(
        SessionData(
            user_id=tenant_admin_id,
            tenant_id=active_tenant_id,
            email=TENANT_ADMIN_EMAIL,
            subject=TENANT_ADMIN_EMAIL,
            provider_type="password",
            mfa_passed=True,
            account_type="tenant",
            tenant_slug=active_tenant_slug,
        ),
        3600,
        3600,
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
                TenantCrudWorld(
                    super_admin_token=token,
                    elevations_only_token=narrow_token,
                    tenant_admin_token=tenant_token,
                    active_tenant_id=active_tenant_id,
                    active_tenant_slug=active_tenant_slug,
                    deactivated_tenant_id=deactivated_tenant_id,
                    tenant_admin_role_id=tenant_admin_role,
                    super_admin_role_id=super_role,
                    sm=sm,
                ),
            )

    # Scoped by TENANT, not by a fixed email pattern: an invite test creates app_user
    # rows (e.g. "newuser@acme.example") that the old email-LIKE cleanup never
    # matched, so they outlived their tenant's row and the tenant DELETE below hit
    # its FK — rolling back this whole transaction (role cleanup included) and
    # leaking NARROW_ROLE_NAME into the NEXT test's fixture setup.
    async with sm() as s, s.begin():
        test_tenants = "(SELECT id FROM tenant WHERE slug LIKE '%-crud-%')"
        test_user_match = f"tenant_id IN {test_tenants} OR email LIKE '%-crud@vera.example'"
        test_users = f"(SELECT id FROM app_user WHERE {test_user_match})"
        narrow_role_match = f"tenant_id IS NULL AND name = '{NARROW_ROLE_NAME}'"
        narrow_role_ids = f"(SELECT id FROM role WHERE {narrow_role_match})"
        await s.execute(text(f"DELETE FROM auth_audit_log WHERE app_user_id IN {test_users}"))
        await s.execute(text(f"DELETE FROM user_identity WHERE app_user_id IN {test_users}"))
        await s.execute(text(f"DELETE FROM user_role WHERE app_user_id IN {test_users}"))
        await s.execute(text(f"DELETE FROM user_role WHERE role_id IN {narrow_role_ids}"))
        await s.execute(text(f"DELETE FROM app_user WHERE {test_user_match}"))
        await s.execute(text(f"DELETE FROM role WHERE {narrow_role_match}"))
        await s.execute(text(f"DELETE FROM sso_provider WHERE tenant_id IN {test_tenants}"))
        # The isolation test drives a real login + a users:read authz check, which
        # writes an authz.allow row to the (separate, PHI-grade) audit_log table —
        # the only test in this file to exercise that path, so this delete has no
        # earlier rows to clean until then.
        await s.execute(text(f"DELETE FROM audit_log WHERE tenant_id IN {test_tenants}"))
        await s.execute(text("DELETE FROM tenant WHERE slug LIKE '%-crud-%'"))
    await engine.dispose()


# --- create -----------------------------------------------------------------


async def test_create_tenant_returns_the_detail(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"name": "New Co", "slug": "new-co-crud-1", "region": "us-east"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["name"] == "New Co"
    assert data["slug"] == "new-co-crud-1"
    assert data["region"] == "us-east"
    assert data["status"] == "active"
    # Model defaults, not caller-supplied — proves the definer fn's server defaults apply.
    assert data["max_agents_per_va"] == 3
    assert data["max_retries"] == 5
    assert data["queue_expiry_hours"] == 48
    assert data["observer_enabled"] is True


async def test_create_tenant_without_region_leaves_it_null(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"name": "No Region Co", "slug": "no-region-crud-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["region"] is None


async def test_create_tenant_duplicate_slug_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    body = {"name": "Dup", "slug": "dup-crud-1"}
    first = await client.post(
        "/api/v1/platform/tenants", headers={**_auth(world.super_admin_token), **_idem()}, json=body
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/v1/platform/tenants", headers={**_auth(world.super_admin_token), **_idem()}, json=body
    )
    assert second.status_code == 409, second.text


async def test_create_tenant_invalid_slug_rejected(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"name": "Bad", "slug": "Not Valid!"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_tenant_requires_idempotency_key(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers=_auth(world.super_admin_token),
        json={"name": "No Key", "slug": "no-key-crud-1"},
    )
    assert resp.status_code == 400, resp.text


async def test_replayed_create_idempotency_key_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    headers = {**_auth(world.super_admin_token), **_idem()}
    body = {"name": "Replay Co", "slug": "replay-crud-1"}
    first = await client.post("/api/v1/platform/tenants", headers=headers, json=body)
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/platform/tenants", headers=headers, json=body)
    assert replay.status_code == 409, replay.text


async def test_elevations_read_only_caller_cannot_create(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.elevations_only_token), **_idem()},
        json={"name": "Nope", "slug": "nope-crud-1"},
    )
    assert resp.status_code == 403, resp.text


async def test_tenant_tier_caller_cannot_create(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.tenant_admin_token), **_idem()},
        json={"name": "Nope", "slug": "nope-crud-2"},
    )
    assert resp.status_code == 403, resp.text


async def test_create_tenant_writes_the_auth_audit_event(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"name": "Audited Co", "slug": "audited-crud-1"},
    )
    assert resp.status_code == 201, resp.text
    tenant_id = resp.json()["data"]["id"]

    audit_tenant, meta = await _latest_audit(world.sm, AuthEvent.TENANT_CREATED)
    assert audit_tenant is None
    assert meta["target_tenant"] == tenant_id


# --- get one ------------------------------------------------------------------


async def test_get_tenant_returns_every_field(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}",
        headers=_auth(world.super_admin_token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == str(world.active_tenant_id)
    assert data["status"] == "active"
    for field in (
        "max_agents_per_va",
        "max_concurrent_calls",
        "max_retries",
        "queue_expiry_hours",
        "recording_retention_days",
        "observer_enabled",
        "auto_retry_enabled",
        "retry_fill_threshold",
        "created_at",
    ):
        assert field in data, field


async def test_get_unknown_tenant_returns_404(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{uuid7()}", headers=_auth(world.super_admin_token)
    )
    assert resp.status_code == 404, resp.text


async def test_elevations_read_only_caller_cannot_get_detail(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}",
        headers=_auth(world.elevations_only_token),
    )
    assert resp.status_code == 403, resp.text


# --- list status filter --------------------------------------------------------


async def test_list_defaults_to_active_only(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get("/api/v1/platform/tenants", headers=_auth(world.super_admin_token))
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["data"]}
    assert str(world.active_tenant_id) in ids
    assert str(world.deactivated_tenant_id) not in ids


async def test_list_status_all_includes_deactivated_for_a_manage_caller(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        "/api/v1/platform/tenants?status=all", headers=_auth(world.super_admin_token)
    )
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["data"]}
    assert str(world.active_tenant_id) in ids
    assert str(world.deactivated_tenant_id) in ids


async def test_list_status_deactivated_filters_to_only_those(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        "/api/v1/platform/tenants?status=deactivated", headers=_auth(world.super_admin_token)
    )
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["data"]}
    assert str(world.deactivated_tenant_id) in ids
    assert str(world.active_tenant_id) not in ids


async def test_list_status_all_ignored_without_tenants_manage(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """The elevation picker's caller (elevations:read only) must never see a
    deactivated tenant, even if it asks for status=all."""
    client, world = crud_world
    resp = await client.get(
        "/api/v1/platform/tenants?status=all", headers=_auth(world.elevations_only_token)
    )
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["data"]}
    assert str(world.deactivated_tenant_id) not in ids


# --- edit ----------------------------------------------------------------------


async def _create(client: httpx.AsyncClient, token: str, slug: str) -> str:
    resp = await client.post(
        "/api/v1/platform/tenants",
        headers={**_auth(token), **_idem()},
        json={"name": "Edit Target", "slug": slug},
    )
    assert resp.status_code == 201, resp.text
    tenant_id: str = resp.json()["data"]["id"]
    return tenant_id


async def test_patch_updates_only_the_fields_sent(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-name-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"name": "Renamed", "max_retries": 2},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "Renamed"
    assert data["max_retries"] == 2
    # Untouched fields keep their create-time defaults.
    assert data["queue_expiry_hours"] == 48
    assert data["max_agents_per_va"] == 3


async def test_patch_can_set_and_clear_the_region(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-region-crud-1")

    set_resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"region": "eu-west"},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["data"]["region"] == "eu-west"

    clear_resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"region": None},
    )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["data"]["region"] is None


async def test_patch_can_set_and_clear_recording_retention_days(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-retention-crud-1")

    set_resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"recording_retention_days": 90},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["data"]["recording_retention_days"] == 90

    clear_resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"recording_retention_days": None},
    )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["data"]["recording_retention_days"] is None


async def test_patch_every_editable_field(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-all-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={
            "name": "Fully Edited",
            "region": "ap-south",
            "observer_enabled": False,
            "auto_retry_enabled": False,
            "retry_fill_threshold": 0.75,
            "max_agents_per_va": 7,
            "max_concurrent_calls": 40,
            "max_retries": 1,
            "queue_expiry_hours": 12,
            "recording_retention_days": 30,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "Fully Edited"
    assert data["region"] == "ap-south"
    assert data["observer_enabled"] is False
    assert data["auto_retry_enabled"] is False
    assert data["retry_fill_threshold"] == 0.75
    assert data["max_agents_per_va"] == 7
    assert data["max_concurrent_calls"] == 40
    assert data["max_retries"] == 1
    assert data["queue_expiry_hours"] == 12
    assert data["recording_retention_days"] == 30


async def test_patch_rejects_an_empty_body(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-empty-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_cannot_touch_slug_or_status(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """slug/status are not fields on the request model at all — a caller who tries to
    send them gets a validation error (extra="forbid"), not a silent no-op change. The
    body also carries a genuinely valid field (name): without extra="forbid" this
    request would otherwise succeed (slug/status silently dropped), so this actually
    exercises the "forbid" behaviour rather than the separate empty-body check."""
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-immutable-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"name": "Still Named", "slug": "hijacked", "status": "deactivated"},
    )
    assert resp.status_code == 422, resp.text

    # The name change must not have gone through either — the whole request is
    # rejected, not partially applied.
    detail = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=_auth(world.super_admin_token)
    )
    assert detail.json()["data"]["name"] == "Edit Target"


async def test_patch_rejects_out_of_range_values(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-range-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"retry_fill_threshold": 1.5},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_unknown_tenant_returns_404(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.patch(
        f"/api/v1/platform/tenants/{uuid7()}",
        headers=_auth(world.super_admin_token),
        json={"name": "Nope"},
    )
    assert resp.status_code == 404, resp.text


async def test_elevations_read_only_caller_cannot_patch(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-forbidden-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.elevations_only_token),
        json={"name": "Nope"},
    )
    assert resp.status_code == 403, resp.text


async def test_patch_writes_the_auth_audit_event_with_field_names_only(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """No field VALUES in the audit meta — this is the one endpoint whose changed
    fields could plausibly include an operator-sensitive number; only names are safe
    to persist in the append-only log."""
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "patch-audit-crud-1")
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=_auth(world.super_admin_token),
        json={"name": "Audited Edit", "max_retries": 9},
    )
    assert resp.status_code == 200, resp.text

    audit_tenant, meta = await _latest_audit(world.sm, AuthEvent.TENANT_UPDATED)
    assert audit_tenant is None
    assert meta["target_tenant"] == tenant_id
    assert set(meta["fields"]) == {"name", "max_retries"}
    # No other keys — in particular, no "name"/"max_retries" key carrying the VALUE.
    assert set(meta.keys()) == {"target_tenant", "fields"}
    assert "Audited Edit" not in str(meta)


# --- deactivate / reactivate -----------------------------------------------------


async def _login(client: httpx.AsyncClient, slug: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/tenants/{slug}/auth/login",
        json={"email": TENANT_ADMIN_EMAIL, "password": TENANT_ADMIN_PASSWORD},
    )


async def test_login_works_before_deactivate(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """Sanity check the fixture's password credential actually works, so a later
    'login now fails' assertion proves the deactivate, not a broken test setup."""
    client, world = crud_world
    resp = await _login(client, world.active_tenant_slug)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mfa"] == "none"


async def test_deactivate_then_reactivate_blocks_and_restores_login(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world

    deactivate = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/deactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert deactivate.status_code == 200, deactivate.text
    assert deactivate.json()["data"]["status"] == "deactivated"

    blocked = await _login(client, world.active_tenant_slug)
    assert blocked.status_code == 401, blocked.text

    reactivate = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/reactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert reactivate.status_code == 200, reactivate.text
    assert reactivate.json()["data"]["status"] == "active"

    restored = await _login(client, world.active_tenant_slug)
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["mfa"] == "none"


async def test_deactivate_already_deactivated_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.deactivated_tenant_id}/deactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert resp.status_code == 409, resp.text


async def test_reactivate_already_active_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/reactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert resp.status_code == 409, resp.text


async def test_deactivate_unknown_tenant_returns_404(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{uuid7()}/deactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert resp.status_code == 404, resp.text


async def test_deactivate_requires_idempotency_key(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/deactivate",
        headers=_auth(world.super_admin_token),
    )
    assert resp.status_code == 400, resp.text


async def test_elevations_read_only_caller_cannot_deactivate(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/deactivate",
        headers={**_auth(world.elevations_only_token), **_idem()},
    )
    assert resp.status_code == 403, resp.text


async def test_tenant_tier_caller_cannot_deactivate(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/deactivate",
        headers={**_auth(world.tenant_admin_token), **_idem()},
    )
    assert resp.status_code == 403, resp.text


async def test_deactivate_writes_the_auth_audit_event(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "deactivate-audit-crud-1")
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/deactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert resp.status_code == 200, resp.text

    audit_tenant, meta = await _latest_audit(world.sm, AuthEvent.TENANT_DEACTIVATED)
    assert audit_tenant is None
    assert meta["target_tenant"] == tenant_id


async def test_reactivate_writes_the_auth_audit_event(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "reactivate-audit-crud-1")
    await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/deactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/reactivate",
        headers={**_auth(world.super_admin_token), **_idem()},
    )
    assert resp.status_code == 200, resp.text

    audit_tenant, meta = await _latest_audit(world.sm, AuthEvent.TENANT_REACTIVATED)
    assert audit_tenant is None
    assert meta["target_tenant"] == tenant_id


# --- invite a user into a tenant (VR2-30 Step 7 — no elevation) -----------------


async def test_list_tenant_users_includes_the_seeded_admin(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users",
        headers=_auth(world.super_admin_token),
    )
    assert resp.status_code == 200, resp.text
    row = next(u for u in resp.json()["data"] if u["email"] == TENANT_ADMIN_EMAIL)
    assert row["status"] == "active"
    assert "TENANT_ADMIN" in row["roles"]


async def test_list_tenant_users_empty_for_a_fresh_tenant(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    tenant_id = await _create(client, world.super_admin_token, "invite-users-empty-crud-1")
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}/users", headers=_auth(world.super_admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_list_tenant_users_unknown_tenant_returns_404(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{uuid7()}/users", headers=_auth(world.super_admin_token)
    )
    assert resp.status_code == 404, resp.text


async def test_list_tenant_roles_returns_only_global_system_roles(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/roles",
        headers=_auth(world.super_admin_token),
    )
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()["data"]}
    assert "TENANT_ADMIN" in names
    # SUPER_ADMIN is a global role too, but platform-tier — must never be offered as
    # an assignable option for a tenant user.
    assert "SUPER_ADMIN" not in names
    assert all(r["is_system"] for r in resp.json()["data"])


async def test_invite_tenant_user_creates_an_invited_user_with_the_role(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={
            "email": "brandnew@acme.example",
            "name": "Brand New",
            "role_ids": [str(world.tenant_admin_role_id)],
            "send_email": False,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["email"] == "brandnew@acme.example"
    assert data["email_sent"] is False
    assert world.active_tenant_slug in data["invite_url"]

    listed = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users",
        headers=_auth(world.super_admin_token),
    )
    row = next(u for u in listed.json()["data"] if u["email"] == "brandnew@acme.example")
    assert row["status"] == "invited"
    assert row["roles"] == ["TENANT_ADMIN"]


async def test_invite_tenant_user_without_roles(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": "norole@acme.example", "name": "No Role", "send_email": False},
    )
    assert resp.status_code == 200, resp.text

    listed = await client.get(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users",
        headers=_auth(world.super_admin_token),
    )
    row = next(u for u in listed.json()["data"] if u["email"] == "norole@acme.example")
    assert row["roles"] == []


async def test_invite_tenant_user_duplicate_email_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    body = {"email": "dupe-invite@acme.example", "name": "Dupe", "send_email": False}
    first = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json=body,
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json=body,
    )
    assert second.status_code == 409, second.text


async def test_invite_tenant_user_unknown_tenant_returns_404(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{uuid7()}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": "x@x.example", "name": "X", "send_email": False},
    )
    assert resp.status_code == 404, resp.text


async def test_invite_tenant_user_cannot_grant_a_platform_tier_role(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """SUPER_ADMIN is a global role too — the one thing this endpoint must never
    allow, since it would hand a tenant user platform-wide power."""
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={
            "email": "escalate@acme.example",
            "name": "Escalate",
            "role_ids": [str(world.super_admin_role_id)],
            "send_email": False,
        },
    )
    assert resp.status_code == 403, resp.text


async def test_invite_tenant_user_unknown_role_id_rejected(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={
            "email": "badrole@acme.example",
            "name": "Bad Role",
            "role_ids": [str(uuid7())],
            "send_email": False,
        },
    )
    assert resp.status_code == 404, resp.text


async def test_invite_tenant_user_requires_idempotency_key(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers=_auth(world.super_admin_token),
        json={"email": "nokey@acme.example", "name": "No Key", "send_email": False},
    )
    assert resp.status_code == 400, resp.text


async def test_elevations_read_only_caller_cannot_invite(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """The whole point of Option B: this succeeds WITHOUT an elevation grant for a
    super admin, but a caller lacking platform:tenants:manage still can't do it."""
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.elevations_only_token), **_idem()},
        json={"email": "forbidden@acme.example", "name": "Forbidden", "send_email": False},
    )
    assert resp.status_code == 403, resp.text


async def test_tenant_tier_caller_cannot_invite(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.tenant_admin_token), **_idem()},
        json={"email": "tenanttier@acme.example", "name": "Tenant Tier", "send_email": False},
    )
    assert resp.status_code == 403, resp.text


async def test_invite_tenant_user_writes_the_auth_audit_event(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    client, world = crud_world
    resp = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": "audited-invite@acme.example", "name": "Audited", "send_email": False},
    )
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["data"]["user_id"]

    audit_tenant, meta = await _latest_audit(world.sm, AuthEvent.TENANT_USER_INVITED)
    assert audit_tenant is None
    assert meta["target_tenant"] == str(world.active_tenant_id)
    assert meta["target_user"] == user_id


async def test_invited_tenant_user_lands_in_the_right_tenant_and_cannot_see_another_tenant(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """End-to-end proof (not just the invite row): accept the platform-issued invite,
    log in, and confirm the session is scoped to the invited tenant and RLS hides a
    second tenant's users from the roster."""
    client, world = crud_world

    other_tenant_id = await _create(client, world.super_admin_token, "isolation-other-crud-1")
    other_invite = await client.post(
        f"/api/v1/platform/tenants/{other_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": "other-tenant-user@other.example", "name": "Other", "send_email": False},
    )
    assert other_invite.status_code == 200, other_invite.text

    invite = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={
            "email": "isolation@acme.example",
            "name": "Isolation Check",
            "role_ids": [str(world.tenant_admin_role_id)],
            "send_email": False,
        },
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    accept = await client.post(
        f"/api/v1/tenants/{world.active_tenant_slug}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text

    login = await client.post(
        f"/api/v1/tenants/{world.active_tenant_slug}/auth/login",
        json={"email": "isolation@acme.example", "password": "a-strong-password"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["data"]["mfa"] == "none"
    session_token = login.json()["data"]["session_token"]

    me = await client.get("/api/v1/auth/me", headers=_auth(session_token))
    assert me.status_code == 200, me.text
    me_data = me.json()["data"]
    assert me_data["tenant_id"] == str(world.active_tenant_id)
    assert me_data["tenant_slug"] == world.active_tenant_slug
    assert "TENANT_ADMIN" in me_data["roles"]

    roster = await client.get("/api/v1/users", headers=_auth(session_token))
    assert roster.status_code == 200, roster.text
    emails = {u["email"] for u in roster.json()["data"]}
    assert "isolation@acme.example" in emails
    assert TENANT_ADMIN_EMAIL in emails
    assert "other-tenant-user@other.example" not in emails


async def test_invite_email_with_password_identity_in_another_tenant_conflicts(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """The user_identity (provider_type, provider_subject) unique constraint is global,
    so an email whose invite was already ACCEPTED in one tenant can never accept in a
    second — reject the second invite up front with an exact message, not at accept."""
    client, world = crud_world
    # Unique per run: tenants and accepted identities created here outlive the test in
    # the shared local DB, and a reused email would trip the very check under test.
    unique = uuid4().hex[:8]
    email = f"taken-elsewhere-{unique}@acme.example"
    other_slug = f"email-taken-other-{unique}"

    other_tenant_id = await _create(client, world.super_admin_token, other_slug)
    invite = await client.post(
        f"/api/v1/platform/tenants/{other_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": email, "name": "Taken", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]
    accept = await client.post(
        f"/api/v1/tenants/{other_slug}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text

    second = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": email, "name": "Taken Again", "send_email": False},
    )
    assert second.status_code == 409, second.text
    assert "another tenant" in second.json()["message"]


async def test_accept_races_identity_in_another_tenant_conflicts_cleanly(
    crud_world: tuple[httpx.AsyncClient, TenantCrudWorld],
) -> None:
    """Both invites issued BEFORE either accept (so the invite-time check passes), then
    the second accept hits the global identity constraint — it must surface the exact
    409, not a 500."""
    client, world = crud_world
    unique = uuid4().hex[:8]
    email = f"race-accept-{unique}@acme.example"
    other_slug = f"race-accept-other-{unique}"

    other_tenant_id = await _create(client, world.super_admin_token, other_slug)
    first = await client.post(
        f"/api/v1/platform/tenants/{other_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": email, "name": "Race", "send_email": False},
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/v1/platform/tenants/{world.active_tenant_id}/users/invitations",
        headers={**_auth(world.super_admin_token), **_idem()},
        json={"email": email, "name": "Race", "send_email": False},
    )
    assert second.status_code == 200, second.text

    first_token = first.json()["data"]["invite_url"].split("token=", 1)[1]
    second_token = second.json()["data"]["invite_url"].split("token=", 1)[1]

    accept_first = await client.post(
        f"/api/v1/tenants/{other_slug}/auth/invitations/accept",
        json={"token": first_token, "password": "a-strong-password"},
    )
    assert accept_first.status_code == 200, accept_first.text

    accept_second = await client.post(
        f"/api/v1/tenants/{world.active_tenant_slug}/auth/invitations/accept",
        json={"token": second_token, "password": "a-strong-password"},
    )
    assert accept_second.status_code == 409, accept_second.text
    assert "another tenant" in accept_second.json()["message"]
