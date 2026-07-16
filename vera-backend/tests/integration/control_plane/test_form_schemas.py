"""HTTP-level tests for the platform form-schema catalog routes.

Mirrors test_ivr_playbooks.py's self-contained world: a platform SUPER_ADMIN,
a tenant admin (to prove the 403), and a form-schema chain. `form_schema.
insurance_type` is a globally UNIQUE, CHECK-constrained catalog key, so the
schema row is find-or-create (CI's seed may already have published it) and
teardown removes only what this fixture created.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, Tenant, UserRole
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus


@dataclass
class World:
    super_token: str
    tenant_admin_token: str
    schema_id: UUID
    draft_version_id: UUID
    published_version: int | None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
async def schemas_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, World]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id, super_id, admin_id = uuid7(), uuid7(), uuid7()
    suffix = tenant_id.hex[:8]
    draft_version_id = uuid7()

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name=f"FS {suffix}", status="active"))
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
                email=f"fs-root-{suffix}@vera.example",
                name="Root",
                status="active",
            )
        )
        s.add(
            AppUser(
                id=admin_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=f"fs-ta-{suffix}@tenant.example",
                name="TA",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))
        s.add(UserRole(tenant_id=tenant_id, app_user_id=admin_id, role_id=admin_role))

        # Find-or-create the schema family (insurance_type is globally unique).
        schema = (
            await s.execute(
                select(FormSchema).where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                )
            )
        ).scalar_one_or_none()
        created_schema = schema is None
        if schema is None:
            schema = FormSchema(
                id=uuid7(),
                insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                name="Test Schema",
            )
            s.add(schema)
            await s.flush()
            s.add(
                SchemaVersion(
                    id=uuid7(),
                    schema_id=schema.id,
                    version=1,
                    schema_json={},
                    status=VersionStatus.PUBLISHED,
                )
            )
            await s.flush()
        schema_id = schema.id
        next_version = (
            await s.execute(
                select(SchemaVersion.version)
                .where(SchemaVersion.schema_id == schema_id)
                .order_by(SchemaVersion.version.desc())
                .limit(1)
            )
        ).scalar_one() + 1
        published_version = (
            await s.execute(
                select(SchemaVersion.version).where(
                    SchemaVersion.schema_id == schema_id,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).scalar_one_or_none()
        # Always add a draft so the versions view has a non-active row to assert on.
        s.add(
            SchemaVersion(
                id=draft_version_id,
                schema_id=schema_id,
                version=next_version,
                schema_json={},
                status=VersionStatus.DRAFT,
            )
        )

    store = InMemorySessionStore()
    super_token = await _mint(
        store, user_id=super_id, tenant_id=None, email=f"fs-root-{suffix}@vera.example"
    )
    admin_token = await _mint(
        store, user_id=admin_id, tenant_id=tenant_id, email=f"fs-ta-{suffix}@tenant.example"
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
                World(super_token, admin_token, schema_id, draft_version_id, published_version),
            )

    async with sm() as s, s.begin():
        await s.execute(
            text("DELETE FROM schema_version WHERE id = :v").bindparams(v=draft_version_id)
        )
        if created_schema:
            await s.execute(
                text("DELETE FROM schema_version WHERE schema_id = :s").bindparams(s=schema_id)
            )
            await s.execute(text("DELETE FROM form_schema WHERE id = :s").bindparams(s=schema_id))
        for tbl in ("audit_log", "auth_audit_log", "user_role", "role_permission", "role"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t").bindparams(t=tenant_id))
        await s.execute(text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=super_id))
        await s.execute(
            text("DELETE FROM app_user WHERE id IN (:s, :a)").bindparams(s=super_id, a=admin_id)
        )
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await engine.dispose()


async def test_super_admin_lists_schemas_with_active_version(
    schemas_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = schemas_world
    resp = await client.get("/api/v1/form-schemas", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    mine = next(r for r in rows if r["id"] == str(w.schema_id))
    assert mine["insurance_type"] == "infertility_treatment"
    assert mine["version_count"] >= 2  # at least the published one + our draft
    assert mine["active_version"] == w.published_version


async def test_versions_view_marks_the_active_one(
    schemas_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = schemas_world
    resp = await client.get(
        f"/api/v1/form-schemas/{w.schema_id}/versions", headers=_auth(w.super_token)
    )
    assert resp.status_code == 200, resp.text
    versions = resp.json()["data"]
    # Newest first; our draft is the newest and is not published.
    assert versions[0]["id"] == str(w.draft_version_id)
    assert versions[0]["status"] == "draft"
    statuses = {v["version"]: v["status"] for v in versions}
    if w.published_version is not None:
        assert statuses[w.published_version] == "published"
    assert sum(1 for v in versions if v["status"] == "published") <= 1


async def test_unknown_schema_returns_404(
    schemas_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = schemas_world
    resp = await client.get(
        f"/api/v1/form-schemas/{uuid7()}/versions", headers=_auth(w.super_token)
    )
    assert resp.status_code == 404, resp.text


async def test_form_schema_routes_forbidden_for_tenant(
    schemas_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = schemas_world
    listed = await client.get("/api/v1/form-schemas", headers=_auth(w.tenant_admin_token))
    assert listed.status_code == 403, listed.text
    versions = await client.get(
        f"/api/v1/form-schemas/{w.schema_id}/versions", headers=_auth(w.tenant_admin_token)
    )
    assert versions.status_code == 403, versions.text


async def test_form_schema_routes_require_auth(
    schemas_world: tuple[httpx.AsyncClient, World],
) -> None:
    client, w = schemas_world
    resp = await client.get("/api/v1/form-schemas")
    assert resp.status_code == 401, resp.text
    resp = await client.get(f"/api/v1/form-schemas/{w.schema_id}/versions")
    assert resp.status_code == 401, resp.text
