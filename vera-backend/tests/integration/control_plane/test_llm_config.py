"""HTTP-level tests for the platform LLM-model-override endpoints. Mirrors
test_ivr_playbooks.py's self-contained `playbooks_world` pattern.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

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


@dataclass
class World:
    tenant_id: UUID
    super_token: str
    tenant_admin_token: str
    name_suffix: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_headers(token: str) -> dict[str, str]:
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
async def llm_config_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id, super_id, admin_id = uuid7(), uuid7(), uuid7()
    suffix = tenant_id.hex[:8]

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"LC {suffix}", status="active"))
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
                email=f"lc-root-{suffix}@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=f"lc-ta-{suffix}@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))

    store = InMemorySessionStore()
    super_token = await _mint(
        store, user_id=super_id, tenant_id=None, email=f"lc-root-{suffix}@vera.example"
    )
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email=f"lc-ta-{suffix}@tenant.example"
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
            yield client, World(tenant_id, super_token, admin_token, suffix)

    async with sm() as s, s.begin():
        await s.execute(text("DELETE FROM voice_model_config WHERE stage = 'llm'"))
        for tbl in ("audit_log", "auth_audit_log", "user_role", "role_permission", "role"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t").bindparams(t=tenant_id))
        await s.execute(text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=super_id))
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:s, :a)").bindparams(s=super_id, a=admin_id)
        )
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await engine.dispose()


async def test_get_llm_config_defaults_when_never_set(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.get("/api/v1/platform/llm-config", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["is_default"] is True
    assert body["model"] is None
    assert body["default_model"] == "gemini-2.5-flash"


async def test_get_llm_config_reports_default_model_even_with_an_active_override(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    # default_model always reflects the cascade's hardcoded fallback — regardless of
    # whether an override is active — so the admin page can show what an override
    # actually replaces, not just what's currently active.
    client, w = llm_config_world
    saved = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["default_model"] == "gemini-2.5-flash"


async def test_save_then_get_reflects_override(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    saved = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["model"] == "gemini-3.5-flash"
    assert saved.json()["data"]["is_default"] is False

    current = await client.get("/api/v1/platform/llm-config", headers=_auth(w.super_token))
    assert current.json()["data"]["model"] == "gemini-3.5-flash"


async def test_save_rejects_blank_model(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "   "},
    )
    assert resp.status_code == 422, resp.text


async def test_reset_clears_override_and_is_idempotent(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    reset = await client.post(
        "/api/v1/platform/llm-config/reset", headers=_write_headers(w.super_token)
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["is_default"] is True

    # Already at default — a second reset is a no-op success, not an error.
    again = await client.post(
        "/api/v1/platform/llm-config/reset", headers=_write_headers(w.super_token)
    )
    assert again.status_code == 200, again.text


async def test_history_lists_saves_and_resets_newest_first(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-2.5-flash"},
    )
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    history = await client.get("/api/v1/platform/llm-config/history", headers=_auth(w.super_token))
    assert history.status_code == 200, history.text
    models = [row["model"] for row in history.json()["data"]]
    assert models[:2] == ["gemini-3.5-flash", "gemini-2.5-flash"]


async def test_routes_forbidden_for_tenant(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    # All four routes gate on a platform permission — a tenant caller (even one
    # carrying a valid Idempotency-Key on the mutating routes) must be denied on
    # every one of them, not just GET.
    client, w = llm_config_world
    resp = await client.get("/api/v1/platform/llm-config", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403

    resp = await client.get(
        "/api/v1/platform/llm-config/history", headers=_auth(w.tenant_admin_token)
    )
    assert resp.status_code == 403

    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.tenant_admin_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/api/v1/platform/llm-config/reset", headers=_write_headers(w.tenant_admin_token)
    )
    assert resp.status_code == 403


async def test_write_requires_idempotency_key(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_auth(w.super_token),
        json={"model": "gemini-3.5-flash"},
    )
    assert resp.status_code == 400, resp.text


async def test_reset_requires_idempotency_key(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.post("/api/v1/platform/llm-config/reset", headers=_auth(w.super_token))
    assert resp.status_code == 400, resp.text


async def test_save_rejects_too_long_model(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "g" * 201},
    )
    assert resp.status_code == 422, resp.text


async def test_save_rejects_disallowed_characters(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini 3.5 flash"},
    )
    assert resp.status_code == 422, resp.text


async def test_save_with_matching_thinking_override_succeeds(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash", "extra_config": {"thinking_level": "high"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["extra_config"] == {"thinking_level": "high"}


async def test_save_rejects_mismatched_thinking_override(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-3.5-flash", "extra_config": {"thinking_budget": 0}},
    )
    assert resp.status_code == 422, resp.text


async def test_save_rejects_both_fields_set(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    resp = await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={
            "model": "gemini-2.5-flash",
            "extra_config": {"thinking_budget": 0, "thinking_level": "low"},
        },
    )
    assert resp.status_code == 422, resp.text


async def test_history_carries_extra_config(
    llm_config_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = llm_config_world
    await client.put(
        "/api/v1/platform/llm-config",
        headers=_write_headers(w.super_token),
        json={"model": "gemini-2.5-flash", "extra_config": {"thinking_budget": 500}},
    )
    history = await client.get("/api/v1/platform/llm-config/history", headers=_auth(w.super_token))
    assert history.status_code == 200, history.text
    assert history.json()["data"][0]["extra_config"] == {"thinking_budget": 500}
