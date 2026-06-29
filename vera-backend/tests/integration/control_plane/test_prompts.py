"""HTTP-level tests for the platform prompt-catalog read endpoints.

Reuses the `world` fixture pattern from test_platform_elevation.py: seeds roles,
tenants, a platform super_token + a tenant_admin_token, plus a FormSchema +
published SchemaVersion + Prompt + published PromptVersion for read assertions."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import SESSION_NS, InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import (
    AppUser,
    FormSchema,
    Prompt,
    PromptVersion,
    SchemaVersion,
    Tenant,
    UserRole,
)
from vera_core.models.enums import InsuranceType, VersionStatus


@dataclass
class World:
    tenant_id: UUID
    other_tenant_id: UUID
    super_user_id: UUID
    tenant_admin_id: UUID
    super_token: str
    tenant_admin_token: str


@dataclass
class PromptIds:
    form_schema_id: UUID
    schema_version_id: UUID
    prompt_id: UUID
    version_id: UUID


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint(
    store: InMemorySessionStore, *, user_id: UUID, tenant_id: UUID | None, email: str
) -> str:
    return await store.put(
        SESSION_NS,
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
    )


@pytest.fixture
async def prompts_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World, PromptIds]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id, other_tenant_id = uuid7(), uuid7()
    super_id, admin_id = uuid7(), uuid7()
    suffix = tenant_id.hex[:8]

    # Wipe any existing infertility_treatment data to avoid UniqueConstraint conflicts
    # (the form_schema table has UniqueConstraint("insurance_type")).
    async with sm() as s, s.begin():
        schema_ids = (
            (
                await s.execute(
                    select(FormSchema.id).where(
                        FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        if schema_ids:
            await s.execute(delete(Prompt).where(Prompt.schema_id.in_(schema_ids)))
            await s.execute(delete(SchemaVersion).where(SchemaVersion.schema_id.in_(schema_ids)))
            await s.execute(delete(FormSchema).where(FormSchema.id.in_(schema_ids)))

    # Seed world: global permissions, system roles, tenants, users.
    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"PW {suffix}", status="active"))
        s.add(
            Tenant(
                id=other_tenant_id,
                slug=str(other_tenant_id),
                name=f"PW other {suffix}",
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
                email="prompts-root@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email="prompts-ta@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))

    # Seed form schema + schema version + prompt + prompt version.
    async with sm() as s, s.begin():
        fs = FormSchema(insurance_type=InsuranceType.INFERTILITY_TREATMENT.value, name="IBV")
        s.add(fs)
        await s.flush()
        sv = SchemaVersion(
            schema_id=fs.id, version=1, schema_json={}, status=VersionStatus.PUBLISHED
        )
        s.add(sv)
        await s.flush()
        prompt = Prompt(schema_id=fs.id, name="IBV Standard Prompt")
        s.add(prompt)
        await s.flush()
        pv = PromptVersion(
            prompt_id=prompt.id,
            schema_version_id=sv.id,
            version=1,
            composite_json={
                "name": "IBV Standard Prompt",
                "format": "text",
                "source": "x",
                "prompt": "hello",
            },
            status=VersionStatus.PUBLISHED,
        )
        s.add(pv)
        await s.flush()
        ids = PromptIds(
            form_schema_id=fs.id,
            schema_version_id=sv.id,
            prompt_id=prompt.id,
            version_id=pv.id,
        )

    store = InMemorySessionStore()
    super_token = await _mint(
        store, user_id=super_id, tenant_id=None, email="prompts-root@vera.example"
    )
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email="prompts-ta@tenant.example"
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
                ids,
            )

    # Cleanup: prompt data first (FK order: prompt_version → prompt → schema_version → form_schema).
    async with sm() as s, s.begin():
        await s.execute(
            text("DELETE FROM prompt_version WHERE id = :id").bindparams(id=ids.version_id)
        )
        await s.execute(text("DELETE FROM prompt WHERE id = :id").bindparams(id=ids.prompt_id))
        await s.execute(
            text("DELETE FROM schema_version WHERE id = :id").bindparams(id=ids.schema_version_id)
        )
        await s.execute(
            text("DELETE FROM form_schema WHERE id = :id").bindparams(id=ids.form_schema_id)
        )

    # Cleanup: world data (mirrors test_platform_elevation.py teardown).
    async with sm() as s, s.begin():
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


async def test_list_prompts_and_versions(prompts_world: tuple) -> None:
    client, w, ids = prompts_world
    listed = await client.get("/api/v1/prompts", headers=_auth(w.super_token))
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert any(p["id"] == str(ids.prompt_id) and p["published_version"] == 1 for p in data)

    versions = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token)
    )
    assert versions.status_code == 200
    assert versions.json()["data"][0]["status"] == "published"

    detail = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}",
        headers=_auth(w.super_token),
    )
    assert detail.json()["data"]["composite_json"]["prompt"] == "hello"


async def test_tenant_user_forbidden(prompts_world: tuple) -> None:
    client, w, _ids = prompts_world
    resp = await client.get("/api/v1/prompts", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403


async def test_create_draft_increments_version(prompts_world) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json={
            "composite_json": {
                "name": "IBV Standard Prompt",
                "format": "text",
                "source": "x",
                "prompt": "edited",
            }
        },
    )
    assert resp.status_code == 201, resp.text
    d = resp.json()["data"]
    assert d["version"] == 2 and d["status"] == "draft"
    assert d["composite_json"]["prompt"] == "edited"


async def test_create_draft_without_published_schema_conflicts(
    prompts_world, admin_sessionmaker
) -> None:
    client, w, ids = prompts_world
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE schema_version SET status='draft' WHERE id=:i").bindparams(
                i=ids.schema_version_id
            )
        )
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json={"composite_json": {"prompt": "x"}},
    )
    assert resp.status_code == 409
