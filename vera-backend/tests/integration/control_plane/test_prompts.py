"""HTTP-level tests for the platform prompt-catalog read endpoints.

Reuses the `world` fixture pattern from test_platform_elevation.py: seeds roles,
tenants, a platform super_token + a tenant_admin_token, plus a FormSchema +
published SchemaVersion + Prompt + published PromptVersion for read assertions."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.forms.dsl import PromotedFields
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


VALID_SCHEMA_JSON: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "IBV",
    "insurance_type": "infertility_treatment",
    "system_fields": {"member_id": "sections.basics.plan_type"},
    "promoted_fields": dict.fromkeys(PromotedFields.model_fields, "sections.basics.plan_type"),
    "rep_call_reference_number_field": "sections.basics.plan_type",
    "sections": {
        "basics": {
            "title": "Basics",
            "fields": {
                "plan_type": {
                    "type": "text",
                    "title": "Plan Type",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What type of plan is this?"},
                },
                "bg": {"type": "text", "title": "Background", "role": "context"},
            },
        }
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["basics"]}],
}

VALID_PROMPT_DOC: dict[str, Any] = {
    "kind": "prompt_document",
    "session": {
        "persona": "You are VERA.",
        "goal": "Verify benefits.",
        "base_instructions": "Ask one question at a time.",
    },
    "task_overrides": {},
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
            tenant_slug=str(tenant_id) if tenant_id is not None else None,
        ),
        3600,
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
            schema_id=fs.id,
            version=1,
            schema_json=VALID_SCHEMA_JSON,
            status=VersionStatus.PUBLISHED,
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
            composite_json=VALID_PROMPT_DOC,
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


async def test_list_prompts_and_versions(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
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
    assert detail.json()["data"]["composite_json"]["session"]["persona"] == "You are VERA."


async def test_tenant_user_forbidden(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, _ids = prompts_world
    resp = await client.get("/api/v1/prompts", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403


async def test_create_draft_increments_version(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    edited = {**VALID_PROMPT_DOC, "session": {**VALID_PROMPT_DOC["session"], "goal": "edited"}}
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json=edited,
    )
    assert resp.status_code == 201, resp.text
    d = resp.json()["data"]
    assert d["version"] == 2 and d["status"] == "draft"
    assert d["composite_json"]["session"]["goal"] == "edited"


async def test_publish_promotes_and_demotes(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    draft = (
        await client.post(
            f"/api/v1/prompts/{ids.prompt_id}/versions",
            headers=_auth(w.super_token),
            json=VALID_PROMPT_DOC,
        )
    ).json()["data"]
    pub = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{draft['id']}/publish",
        headers=_auth(w.super_token),
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["status"] == "published"

    versions = (
        await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token))
    ).json()["data"]
    published = [v for v in versions if v["status"] == "published"]
    assert len(published) == 1 and published[0]["version"] == 2


async def test_create_draft_without_published_schema_conflicts(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
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
        json=VALID_PROMPT_DOC,
    )
    assert resp.status_code == 409


async def test_write_endpoints_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    create_resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.tenant_admin_token),
        json=VALID_PROMPT_DOC,
    )
    assert create_resp.status_code == 403

    publish_resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}/publish",
        headers=_auth(w.tenant_admin_token),
    )
    assert publish_resp.status_code == 403


async def test_unknown_prompt_and_version_404(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    unknown_prompt_id = uuid7()
    versions_resp = await client.get(
        f"/api/v1/prompts/{unknown_prompt_id}/versions",
        headers=_auth(w.super_token),
    )
    assert versions_resp.status_code == 404

    unknown_version_id = uuid7()
    detail_resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{unknown_version_id}",
        headers=_auth(w.super_token),
    )
    assert detail_resp.status_code == 404


async def test_publish_already_published_is_noop(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}/publish",
        headers=_auth(w.super_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "published"

    versions = (
        await client.get(
            f"/api/v1/prompts/{ids.prompt_id}/versions",
            headers=_auth(w.super_token),
        )
    ).json()["data"]
    published = [v for v in versions if v["status"] == "published"]
    assert len(published) == 1


async def test_create_draft_validates_document(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    url = f"/api/v1/prompts/{ids.prompt_id}/versions"
    headers = _auth(world.super_token)

    # not a prompt document at all → 422 (pydantic body validation)
    resp = await client.post(url, headers=headers, json={"composite_json": {}})
    assert resp.status_code == 422

    # unknown task key → 400
    bad_key = {**VALID_PROMPT_DOC, "task_overrides": {"ghost": {"prompt": "x"}}}
    resp = await client.post(url, headers=headers, json=bad_key)
    assert resp.status_code == 400
    assert "unknown task_key" in resp.text

    # unknown placeholder → 400
    bad_ph = {
        **VALID_PROMPT_DOC,
        "session": {**VALID_PROMPT_DOC["session"], "persona": "Hi {{patietn}}."},
    }
    resp = await client.post(url, headers=headers, json=bad_ph)
    assert resp.status_code == 400
    assert "unknown placeholder" in resp.text

    # valid document → 201 draft
    resp = await client.post(url, headers=headers, json=VALID_PROMPT_DOC)
    assert resp.status_code == 201


async def test_preview_renders_published_and_named_draft(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    headers = _auth(world.super_token)

    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/preview", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["persona"] == "You are VERA."
    assert data["tasks"] and all(t["prompt"] for t in data["tasks"])

    # a draft with an override previews differently when named explicitly
    draft_doc = {
        **VALID_PROMPT_DOC,
        "task_overrides": {"main": {"prompt": "OVERRIDDEN INSTRUCTIONS."}},
    }
    created = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions", headers=headers, json=draft_doc
    )
    draft_id = created.json()["data"]["id"]
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=headers,
        params={"version_id": draft_id},
    )
    assert resp.status_code == 200
    main = next(t for t in resp.json()["data"]["tasks"] if t["task_key"] == "main")
    assert main["prompt"].startswith("OVERRIDDEN INSTRUCTIONS.")


async def test_preview_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(world.tenant_admin_token)
    )
    assert resp.status_code == 403


async def test_versions_expose_pinned_schema_version(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    versions = (
        await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token))
    ).json()["data"]
    assert versions[0]["schema_version_id"] == str(ids.schema_version_id)
    assert versions[0]["schema_version"] == 1

    detail = (
        await client.get(
            f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}",
            headers=_auth(w.super_token),
        )
    ).json()["data"]
    assert detail["schema_version_id"] == str(ids.schema_version_id)
    assert detail["schema_version"] == 1


async def test_get_prompt_schema_returns_published_document(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == str(ids.schema_version_id)
    assert data["version"] == 1
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    assert data["document"]["system_fields"] == {"member_id": "sections.basics.plan_type"}
    assert data["document"]["tasks"][0]["task_key"] == "main"


async def test_get_prompt_schema_conflict_when_none_published(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, w, ids = prompts_world
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE schema_version SET status='draft' WHERE id=:i").bindparams(
                i=ids.schema_version_id
            )
        )
    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.super_token))
    assert resp.status_code == 409


async def test_get_prompt_schema_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.tenant_admin_token)
    )
    assert resp.status_code == 403


async def test_stateless_preview_renders_without_saving(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    headers = _auth(w.super_token)
    body = {**VALID_PROMPT_DOC, "task_overrides": {"main": {"prompt": "DRY RUN."}}}
    resp = await client.post(f"/api/v1/prompts/{ids.prompt_id}/preview", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["errors"] == []
    main = next(t for t in data["rendered"]["tasks"] if t["task_key"] == "main")
    assert main["prompt"].startswith("DRY RUN.")

    versions = (
        await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=headers)
    ).json()["data"]
    assert len(versions) == 1  # no draft row was created


async def test_stateless_preview_reports_content_errors_but_still_renders(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    body = {
        **VALID_PROMPT_DOC,
        "session": {**VALID_PROMPT_DOC["session"], "persona": "Hi {{ghost}}."},
        "task_overrides": {"phantom": {"prompt": "x"}},
    }
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(w.super_token), json=body
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "session.persona: unknown placeholder {{ghost}}" in data["errors"]
    assert "task_overrides.phantom: unknown task_key" in data["errors"]
    assert data["rendered"]["persona"] == "Hi {{ghost}}."  # rendered anyway


async def test_stateless_preview_shape_error_is_422(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(w.super_token), json={"nope": 1}
    )
    assert resp.status_code == 422


async def test_stateless_preview_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=_auth(w.tenant_admin_token),
        json=VALID_PROMPT_DOC,
    )
    assert resp.status_code == 403


async def test_preview_named_version_pinned_to_pre_promoted_fields_schema_conflicts(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A historical prompt_version can pin (RESTRICT FK) a schema_version whose
    schema_json predates the required `promoted_fields` block — such rows are
    left in place by the cleanup migration since prompt_version references them.
    Requesting that version_id explicitly must 409, not 500."""
    client, w, ids = prompts_world
    pre_block_schema_json = {k: v for k, v in VALID_SCHEMA_JSON.items() if k != "promoted_fields"}

    async with admin_sessionmaker() as s, s.begin():
        # Insert the raw dict directly via the DB session (SQLAlchemy model, no
        # Pydantic) — bypassing FormSchemaDoc validation is the whole point:
        # this is exactly the shape a pre-existing row can have.
        stale_schema_version = SchemaVersion(
            schema_id=ids.form_schema_id,
            version=99,
            schema_json=pre_block_schema_json,
            status=VersionStatus.DRAFT,
        )
        s.add(stale_schema_version)
        await s.flush()
        stale_prompt_version = PromptVersion(
            prompt_id=ids.prompt_id,
            schema_version_id=stale_schema_version.id,
            version=99,
            composite_json=VALID_PROMPT_DOC,
            status=VersionStatus.DRAFT,
        )
        s.add(stale_prompt_version)
        await s.flush()
        stale_version_id = stale_prompt_version.id

    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=_auth(w.super_token),
        params={"version_id": str(stale_version_id)},
    )
    assert resp.status_code == 409, resp.text


async def test_stateless_preview_conflict_when_none_published(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, w, ids = prompts_world
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE schema_version SET status='draft' WHERE id=:i").bindparams(
                i=ids.schema_version_id
            )
        )
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=_auth(w.super_token),
        json=VALID_PROMPT_DOC,
    )
    assert resp.status_code == 409
