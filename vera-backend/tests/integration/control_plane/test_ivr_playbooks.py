"""HTTP-level tests for the platform insurance-provider + IVR-playbook CRUD, plus the
runtime generic-vs-playbook selection that injects the active playbook into dispatch
metadata. Mirrors test_prompts.py's self-contained world for the platform CRUD, and reuses
the shared rbac_world/client/fake_livekit fixtures for the Voice-Lab selection path.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

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
from vera_core.models import AppUser, InsuranceProvider, IvrPlaybook, Tenant, UserRole

from .conftest import FakeLiveKit, RBACWorld


@dataclass
class World:
    tenant_id: UUID
    super_token: str
    tenant_admin_token: str
    provider_id: UUID
    name_suffix: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_headers(token: str) -> dict[str, str]:
    """Auth + a fresh Idempotency-Key (mutating platform ingress requires one)."""
    return {**_auth(token), "Idempotency-Key": str(uuid7())}


async def _mint(
    store: InMemorySessionStore, *, user_id: UUID, tenant_id: UUID | None, email: str
) -> str:
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="tenant" if tenant_id is not None else "platform",
            tenant_slug=str(tenant_id) if tenant_id is not None else None,
        ),
        3600,
        3600,
    )


@pytest.fixture
async def playbooks_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id, super_id, admin_id, provider_id = uuid7(), uuid7(), uuid7(), uuid7()
    suffix = tenant_id.hex[:8]

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"PB {suffix}", status="active"))
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
                email=f"pb-root-{suffix}@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=f"pb-ta-{suffix}@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))
        s.add(InsuranceProvider(id=provider_id, name=f"UnitedHealth {suffix}", status="active"))

    store = InMemorySessionStore()
    super_token = await _mint(
        store, user_id=super_id, tenant_id=None, email=f"pb-root-{suffix}@vera.example"
    )
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email=f"pb-ta-{suffix}@tenant.example"
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
            yield client, World(tenant_id, super_token, admin_token, provider_id, suffix)

    async with sm() as s, s.begin():
        # Playbooks first (FK), then providers created by this test run (matched by suffix).
        await s.execute(
            text(
                "DELETE FROM ivr_playbook WHERE provider_id IN "
                "(SELECT id FROM insurance_provider WHERE name LIKE :pat)"
            ).bindparams(pat=f"%{suffix}%")
        )
        await s.execute(
            text("DELETE FROM insurance_provider WHERE name LIKE :pat").bindparams(
                pat=f"%{suffix}%"
            )
        )
        for tbl in ("audit_log", "auth_audit_log", "user_role", "role_permission", "role"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t").bindparams(t=tenant_id))
        await s.execute(text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=super_id))
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:s, :a)").bindparams(s=super_id, a=admin_id)
        )
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await engine.dispose()


# ---------------------------------------------------------------- provider + playbook CRUD


async def test_list_and_create_provider(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    listed = await client.get("/api/v1/insurance-providers", headers=_auth(w.super_token))
    assert listed.status_code == 200, listed.text
    assert any(p["id"] == str(w.provider_id) for p in listed.json()["data"])

    created = await client.post(
        "/api/v1/insurance-providers",
        headers=_write_headers(w.super_token),
        json={"name": f"Cigna {w.name_suffix}"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["name"] == f"Cigna {w.name_suffix}"


async def test_provider_get_update_soft_delete(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    detail = await client.get(
        f"/api/v1/insurance-providers/{w.provider_id}", headers=_auth(w.super_token)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "active"

    updated = await client.patch(
        f"/api/v1/insurance-providers/{w.provider_id}",
        headers=_write_headers(w.super_token),
        json={"name": f"Aetna {w.name_suffix}", "working_hour_start": "09:00:00"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()["data"]
    assert body["name"] == f"Aetna {w.name_suffix}"
    assert body["working_hour_start"] == "09:00:00"

    # Soft delete deactivates rather than removing — the row is still fetchable, now inactive.
    deleted = await client.delete(
        f"/api/v1/insurance-providers/{w.provider_id}", headers=_write_headers(w.super_token)
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "inactive"
    still_there = await client.get(
        f"/api/v1/insurance-providers/{w.provider_id}", headers=_auth(w.super_token)
    )
    assert still_there.status_code == 200
    assert still_there.json()["data"]["status"] == "inactive"

    # Deleting an already-inactive provider is an idempotent no-op success.
    again = await client.delete(
        f"/api/v1/insurance-providers/{w.provider_id}", headers=_write_headers(w.super_token)
    )
    assert again.status_code == 200, again.text


async def test_provider_patch_omitted_vs_null_and_reactivation(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    # UpdateProviderRequest's whole reason to exist is distinguishing "field omitted" (leave
    # unchanged) from "explicit null" (clear) on the nullable working-hour fields — plus the
    # reactivation path the DELETE endpoint advertises. Cover both.
    client, w = playbooks_world

    async def _patch(payload: dict[str, object]) -> httpx.Response:
        return await client.patch(
            f"/api/v1/insurance-providers/{w.provider_id}",
            headers=_write_headers(w.super_token),
            json=payload,
        )

    seeded = await _patch({"working_hour_start": "09:00:00", "working_hour_end": "17:00:00"})
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["data"]["working_hour_start"] == "09:00:00"
    assert seeded.json()["data"]["working_hour_end"] == "17:00:00"

    # Explicit null clears ONLY that field; the omitted end is left untouched.
    cleared = await _patch({"working_hour_start": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["working_hour_start"] is None
    assert cleared.json()["data"]["working_hour_end"] == "17:00:00"

    # A name-only PATCH leaves both hours untouched (neither field is in model_fields_set).
    renamed = await _patch({"name": f"Renamed {w.name_suffix}"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["working_hour_start"] is None
    assert renamed.json()["data"]["working_hour_end"] == "17:00:00"

    # Reactivation: soft-delete (→ inactive), then PATCH status=active brings it back.
    await client.delete(
        f"/api/v1/insurance-providers/{w.provider_id}", headers=_write_headers(w.super_token)
    )
    reactivated = await _patch({"status": "active"})
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["data"]["status"] == "active"


async def test_provider_patch_rejects_blank_name(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    # A whitespace-only name is stripped and rejected at the edge (min_length=1), not stored.
    client, w = playbooks_world
    resp = await client.patch(
        f"/api/v1/insurance-providers/{w.provider_id}",
        headers=_write_headers(w.super_token),
        json={"name": "   "},
    )
    assert resp.status_code == 422, resp.text


async def test_provider_get_unknown_404(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    resp = await client.get(f"/api/v1/insurance-providers/{uuid7()}", headers=_auth(w.super_token))
    assert resp.status_code == 404, resp.text


async def test_update_provider_unknown_404(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    resp = await client.patch(
        f"/api/v1/insurance-providers/{uuid7()}",
        headers=_write_headers(w.super_token),
        json={"name": "Nope"},
    )
    assert resp.status_code == 404, resp.text


async def test_update_provider_duplicate_name_409(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    other = await client.post(
        "/api/v1/insurance-providers",
        headers=_write_headers(w.super_token),
        json={"name": f"Humana {w.name_suffix}"},
    )
    assert other.status_code == 201, other.text
    # Renaming the seed provider onto the just-created name (case-variant) hits the
    # case-insensitive unique index → 409, not a 500.
    clash = await client.patch(
        f"/api/v1/insurance-providers/{w.provider_id}",
        headers=_write_headers(w.super_token),
        json={"name": f"humana {w.name_suffix}"},
    )
    assert clash.status_code == 409, clash.text


async def test_provider_write_requires_idempotency_key(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    resp = await client.patch(
        f"/api/v1/insurance-providers/{w.provider_id}",
        headers=_auth(w.super_token),
        json={"name": "NoKey"},
    )
    assert resp.status_code == 400, resp.text


async def test_provider_routes_forbidden_for_tenant(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    listed = await client.get("/api/v1/insurance-providers", headers=_auth(w.tenant_admin_token))
    assert listed.status_code == 403
    patched = await client.patch(
        f"/api/v1/insurance-providers/{w.provider_id}",
        headers=_write_headers(w.tenant_admin_token),
        json={"name": "X"},
    )
    assert patched.status_code == 403


async def test_playbook_crud_happy_path(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    created = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_write_headers(w.super_token),
        json={
            "provider_id": str(w.provider_id),
            "instructions": {"provider_subflows": "Press 3", "extra_rules": "Say Advocate"},
            "status": "active",
        },
    )
    assert created.status_code == 201, created.text
    pb = created.json()["data"]
    assert pb["status"] == "active"
    assert pb["instructions"]["provider_subflows"] == "Press 3"

    listed = await client.get(
        f"/api/v1/ivr-playbooks?provider_id={w.provider_id}", headers=_auth(w.super_token)
    )
    assert [p["id"] for p in listed.json()["data"]] == [pb["id"]]

    detail = await client.get(f"/api/v1/ivr-playbooks/{pb['id']}", headers=_auth(w.super_token))
    assert detail.json()["data"]["instructions"]["extra_rules"] == "Say Advocate"

    updated = await client.patch(
        f"/api/v1/ivr-playbooks/{pb['id']}",
        headers=_auth(w.super_token),
        json={"instructions": {"provider_subflows": "Press 5"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["instructions"]["provider_subflows"] == "Press 5"

    deleted = await client.delete(f"/api/v1/ivr-playbooks/{pb['id']}", headers=_auth(w.super_token))
    assert deleted.status_code == 200
    assert (
        await client.get(f"/api/v1/ivr-playbooks/{pb['id']}", headers=_auth(w.super_token))
    ).status_code == 404


async def test_activating_second_playbook_demotes_the_first(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    first = (
        await client.post(
            "/api/v1/ivr-playbooks",
            headers=_write_headers(w.super_token),
            json={"provider_id": str(w.provider_id), "instructions": {"provider_subflows": "A"}},
        )
    ).json()["data"]
    # A second active for the same provider is allowed — it demotes the first (one active max).
    second = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_write_headers(w.super_token),
        json={
            "provider_id": str(w.provider_id),
            "instructions": {"provider_subflows": "B"},
            "status": "active",
        },
    )
    assert second.status_code == 201, second.text
    listed = (
        await client.get(
            f"/api/v1/ivr-playbooks?provider_id={w.provider_id}", headers=_auth(w.super_token)
        )
    ).json()["data"]
    active = [p for p in listed if p["status"] == "active"]
    assert len(active) == 1 and active[0]["id"] == second.json()["data"]["id"]
    assert next(p for p in listed if p["id"] == first["id"])["status"] == "inactive"


async def test_create_playbook_unknown_provider_404(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    resp = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_write_headers(w.super_token),
        json={"provider_id": str(uuid7()), "instructions": {}},
    )
    assert resp.status_code == 404


async def test_create_playbook_rejects_unknown_status(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    # A mis-cased/typo status would silently escape every status == "active" comparison
    # (demote, unique index, runtime selection) — the API must reject it outright.
    client, w = playbooks_world
    resp = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_write_headers(w.super_token),
        json={"provider_id": str(w.provider_id), "instructions": {}, "status": "Active"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_playbook_requires_idempotency_key(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    resp = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_auth(w.super_token),
        json={"provider_id": str(w.provider_id), "instructions": {}},
    )
    assert resp.status_code == 400, resp.text


async def test_malformed_playbook_row_is_viewable_and_deletable(
    playbooks_world: tuple[httpx.AsyncClient, World],
    database_url: str,
) -> None:
    """A row whose stored instructions no longer validate (seed script, raw SQL, schema
    rename) must still be readable (unknown keys dropped) and deletable via the API."""
    client, w = playbooks_world
    playbook_id = uuid7()
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s, s.begin():
            s.add(
                IvrPlaybook(
                    id=playbook_id,
                    provider_id=w.provider_id,
                    instructions={"legacy_key": "x", "provider_subflows": "Press 3"},
                    status="inactive",
                )
            )
        detail = await client.get(
            f"/api/v1/ivr-playbooks/{playbook_id}", headers=_auth(w.super_token)
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["instructions"]["provider_subflows"] == "Press 3"

        deleted = await client.delete(
            f"/api/v1/ivr-playbooks/{playbook_id}", headers=_auth(w.super_token)
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] is None
    finally:
        async with sm() as s, s.begin():
            await s.execute(
                text("DELETE FROM ivr_playbook WHERE id = :i").bindparams(i=playbook_id)
            )
        await engine.dispose()


async def test_platform_routes_forbidden_for_tenant(
    playbooks_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = playbooks_world
    listed = await client.get("/api/v1/ivr-playbooks", headers=_auth(w.tenant_admin_token))
    assert listed.status_code == 403
    created = await client.post(
        "/api/v1/ivr-playbooks",
        headers=_write_headers(w.tenant_admin_token),
        json={"provider_id": str(w.provider_id), "instructions": {}},
    )
    assert created.status_code == 403


# ------------------------------------------------------------------- runtime selection


async def _seed_provider_with_active_playbook(
    sm: async_sessionmaker[AsyncSession],
    instructions: dict[str, str],
    *,
    provider_status: str = "active",
) -> tuple[UUID, UUID]:
    provider_id, playbook_id = uuid7(), uuid7()
    async with sm() as s, s.begin():
        s.add(
            InsuranceProvider(id=provider_id, name=f"Sel {provider_id.hex}", status=provider_status)
        )
        await s.flush()
        s.add(
            IvrPlaybook(
                id=playbook_id,
                provider_id=provider_id,
                instructions=instructions,
                status="active",
            )
        )
    return provider_id, playbook_id


async def test_voice_lab_injects_active_playbook_into_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    provider_id, playbook_id = await _seed_provider_with_active_playbook(
        admin_sessionmaker, {"provider_subflows": "Press 3"}
    )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers=_auth(rbac_world.admin_token),
            json={
                "mode": "browser",
                "enable_ivr_navigation": True,
                "insurance_provider_id": str(provider_id),
            },
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert meta["enable_ivr_navigation"] is True
        assert meta["ivr_playbook"] == {"provider_subflows": "Press 3"}
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM ivr_playbook WHERE id = :i").bindparams(i=playbook_id)
            )
            await s.execute(
                text("DELETE FROM insurance_provider WHERE id = :i").bindparams(i=provider_id)
            )


async def test_voice_lab_generic_navigator_when_provider_has_no_playbook(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={
            "mode": "browser",
            "enable_ivr_navigation": True,
            "insurance_provider_id": str(uuid7()),
        },
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[-1]
    assert meta is not None
    assert meta["enable_ivr_navigation"] is True
    assert "ivr_playbook" not in meta  # no active playbook → generic navigator


async def test_malformed_active_playbook_degrades_to_generic_navigator(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A poisoned active row must not 500 call start — it degrades to the generic navigator.
    provider_id, playbook_id = await _seed_provider_with_active_playbook(
        admin_sessionmaker, {"legacy_key": "x"}
    )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers=_auth(rbac_world.admin_token),
            json={
                "mode": "browser",
                "enable_ivr_navigation": True,
                "insurance_provider_id": str(provider_id),
            },
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert "ivr_playbook" not in meta
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM ivr_playbook WHERE id = :i").bindparams(i=playbook_id)
            )
            await s.execute(
                text("DELETE FROM insurance_provider WHERE id = :i").bindparams(i=provider_id)
            )


async def test_inactive_provider_never_steers_a_call(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # An inactive provider with an active playbook must not steer the call — the provider-status
    # gate lives in add_active_playbook_metadata, so Voice Lab (no provider check of its own) is
    # covered centrally.
    provider_id, playbook_id = await _seed_provider_with_active_playbook(
        admin_sessionmaker, {"provider_subflows": "Press 3"}, provider_status="inactive"
    )
    try:
        resp = await client.post(
            "/api/v1/voice-lab/sessions",
            headers=_auth(rbac_world.admin_token),
            json={
                "mode": "browser",
                "enable_ivr_navigation": True,
                "insurance_provider_id": str(provider_id),
            },
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert "ivr_playbook" not in meta
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM ivr_playbook WHERE id = :i").bindparams(i=playbook_id)
            )
            await s.execute(
                text("DELETE FROM insurance_provider WHERE id = :i").bindparams(i=provider_id)
            )


async def test_voice_lab_lists_active_providers_for_tenant_user(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A tenant user (voice_lab:sandbox) can read the provider list for the call-start picker;
    # only active providers are offered.
    active_id, inactive_id = uuid7(), uuid7()
    async with admin_sessionmaker() as s, s.begin():
        s.add(InsuranceProvider(id=active_id, name=f"Sel {active_id.hex}", status="active"))
        s.add(InsuranceProvider(id=inactive_id, name=f"Sel {inactive_id.hex}", status="inactive"))
    try:
        resp = await client.get(
            "/api/v1/voice-lab/insurance-providers", headers=_auth(rbac_world.admin_token)
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in resp.json()["data"]]
        assert str(active_id) in ids
        assert str(inactive_id) not in ids
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM insurance_provider WHERE id IN (:a, :b)").bindparams(
                    a=active_id, b=inactive_id
                )
            )


async def test_voice_lab_providers_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/voice-lab/insurance-providers")
    assert resp.status_code == 401
