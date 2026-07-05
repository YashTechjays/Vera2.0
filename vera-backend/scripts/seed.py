"""Idempotent seed: global permission catalog, the global system/template roles
(SUPER_ADMIN / TENANT_ADMIN / SUPERVISOR) wired to permissions, one sample tenant,
and a sample admin user granted the global TENANT_ADMIN role.

Run AFTER `alembic upgrade head`:  just seed   (or: uv run python scripts/seed.py)

Seeding/provisioning is a privileged operation: it runs as the DB user from
VERA_DATABASE_URL, which locally (docker-compose) is the superuser and so
bypasses RLS. Request-path application code never does this — it always goes
through tenant_session().
"""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.password import hash_password
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import compile_prompt_document
from vera_core.models import (
    AppUser,
    FormSchema,
    IntegrationType,
    Permission,
    Prompt,
    PromptVersion,
    Role,
    RolePermission,
    SchemaVersion,
    SsoProvider,
    Tenant,
    UserIdentity,
    UserRole,
)
from vera_core.models.enums import ProviderKind, VersionStatus
from vera_core.models.rbac_defaults import ALL_PERMISSIONS, SYSTEM_ROLES

# Baseline form schemas live as JSON under data/form_schemas/, mapped to their
# insurance type by manifest.json. form_schema.name comes from each file's
# top-level "name"; the document itself is stored opaquely in schema_version.schema_json.
FORM_SCHEMA_DIR = Path(__file__).parent.parent / "data" / "form_schemas"

# Prompts are GENERATED from each published form schema (vera_core.forms.prompting):
# composite_json = per-task nested JSON of the task-level prompt + the schema-derived
# question lists. The legacy hand-written documents under data/prompts/ are reference
# material only and are no longer seeded.

SAMPLE_TENANT_NAME = "Vera Health (Example)"
# The URL-facing tenant handle (`/tenants/{slug}/auth/login`). Override with
# SEED_TENANT_SLUG before `just seed` if you want a different login URL locally.
SAMPLE_TENANT_SLUG = os.environ.get("SEED_TENANT_SLUG", "vera-health-example")

# Each developer can seed their own admin login by exporting SEED_ADMIN_EMAIL /
# SEED_ADMIN_PASSWORD before `just seed`; both default to the shared sample
# credentials. Local-dev only — rotate everywhere else. The seed is idempotent
# and keyed on email, so a new email adds a user instead of replacing one.
SAMPLE_ADMIN_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@veratechsolutions.example")
SAMPLE_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "dev-password-change-me")


async def _seed_permissions(session: AsyncSession) -> dict[str, UUID]:
    existing = {p.code: p for p in (await session.execute(select(Permission))).scalars()}
    for code, description in ALL_PERMISSIONS.items():
        if code in existing:
            existing[code].description = description
        else:
            permission = Permission(code=code, description=description)
            session.add(permission)
            existing[code] = permission
    await session.flush()
    return {code: p.id for code, p in existing.items()}


async def _seed_tenant(session: AsyncSession) -> UUID:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.name == SAMPLE_TENANT_NAME))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=SAMPLE_TENANT_NAME, slug=SAMPLE_TENANT_SLUG, status="active")
        session.add(tenant)
        await session.flush()
    return tenant.id


async def _grant_permissions(
    session: AsyncSession,
    tenant_id: UUID | None,
    role: Role,
    permission_codes: frozenset[str],
    permission_ids: dict[str, UUID],
) -> None:
    """Idempotently attach the given permission codes to a role. role_permission
    rows carry the role's tenant_id (NULL for global system roles)."""
    granted = {
        rp.permission_id
        for rp in (
            await session.execute(select(RolePermission).where(RolePermission.role_id == role.id))
        ).scalars()
    }
    for permission_code in sorted(permission_codes):
        permission_id = permission_ids[permission_code]
        if permission_id not in granted:
            session.add(
                RolePermission(tenant_id=tenant_id, role_id=role.id, permission_id=permission_id)
            )


async def _seed_system_roles(session: AsyncSession, permission_ids: dict[str, UUID]) -> None:
    """Seed the GLOBAL system roles (tenant_id IS NULL) into the shared catalog.
    Idempotent: look roles up by name where tenant_id IS NULL. Global-catalog
    seeding runs with elevated privilege — the dev DB connects as a superuser,
    which bypasses RLS, so a tenant-pinned session is not required here."""
    existing = {
        r.name: r
        for r in (await session.execute(select(Role).where(Role.tenant_id.is_(None)))).scalars()
    }
    for name, permission_codes in SYSTEM_ROLES.items():
        role = existing.get(name)
        if role is None:
            role = Role(tenant_id=None, name=name, description="")
            session.add(role)
            await session.flush()
        await _grant_permissions(session, None, role, permission_codes, permission_ids)
    await session.flush()


async def _seed_password_provider(session: AsyncSession, tenant_id: UUID) -> None:
    """Enable the local password provider for the sample tenant. enforce_mfa is
    False here so the dev admin can log in without first enrolling TOTP (the user
    can still enroll via /auth/mfa/enroll to exercise that path)."""
    provider = (
        await session.execute(
            select(SsoProvider).where(
                SsoProvider.tenant_id == tenant_id,
                SsoProvider.provider_type == ProviderKind.PASSWORD.value,
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        session.add(
            SsoProvider(
                tenant_id=tenant_id,
                provider_type=ProviderKind.PASSWORD.value,
                display_name="Password",
                enabled=True,
                enforce_mfa=False,
            )
        )
        await session.flush()


async def _seed_admin_user(session: AsyncSession, tenant_id: UUID) -> None:
    # A pure local-password operator: gcip_uid is NULL (no federated identity).
    user = (
        await session.execute(
            select(AppUser).where(
                AppUser.tenant_id == tenant_id, AppUser.email == SAMPLE_ADMIN_EMAIL
            )
        )
    ).scalar_one_or_none()
    if user is None:
        user = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email=SAMPLE_ADMIN_EMAIL,
            name="Dev Admin",
            status="active",
        )
        session.add(user)
        await session.flush()

    identity = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.app_user_id == user.id,
                UserIdentity.provider_type == ProviderKind.PASSWORD.value,
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        session.add(
            UserIdentity(
                tenant_id=tenant_id,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=SAMPLE_ADMIN_EMAIL,
                email=SAMPLE_ADMIN_EMAIL,
                hashed_password=hash_password(SAMPLE_ADMIN_PASSWORD),
                mfa_enabled=False,
            )
        )
        await session.flush()

    # TENANT_ADMIN is a global system role (tenant_id IS NULL); the grant itself is
    # tenant-scoped via the user_role row below.
    admin_role = (
        await session.execute(
            select(Role).where(Role.tenant_id.is_(None), Role.name == "TENANT_ADMIN")
        )
    ).scalar_one()
    assignment = (
        await session.execute(
            select(UserRole).where(
                UserRole.app_user_id == user.id,
                UserRole.role_id == admin_role.id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        session.add(UserRole(tenant_id=tenant_id, app_user_id=user.id, role_id=admin_role.id))
    await session.flush()


def _same_document(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Order-sensitive document equality. Plain `==` ignores key order, but a DSL
    document's key order IS its field order (the columns are JSON, not JSONB, for
    the same reason) — a reordered document must publish a new version."""
    return json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)


def _load_manifest(catalog_dir: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Read `<catalog_dir>/manifest.json` and return one `(insurance_type, name, doc)`
    per entry. `doc` is the parsed JSON document the manifest points to; `name` is its
    top-level "name". Shared by the form-schema and prompt seeders (same file layout)."""
    manifest: list[dict[str, str]] = json.loads(
        (catalog_dir / "manifest.json").read_text(encoding="utf-8")
    )
    out: list[tuple[str, str, dict[str, Any]]] = []
    for entry in manifest:
        doc: dict[str, Any] = json.loads((catalog_dir / entry["file"]).read_text(encoding="utf-8"))
        out.append((entry["insurance_type"], doc["name"], doc))
    return out


async def _seed_form_schemas(session: AsyncSession) -> list[str]:
    """Seed the baseline form schemas from data/form_schemas/. Idempotent and keyed
    on the unique insurance_type. Re-running with unchanged JSON is a no-op; changed
    JSON demotes the current published version to DRAFT and publishes a new version
    (the partial unique index allows only one published row per schema)."""
    summary: list[str] = []
    for insurance_type, name, doc in _load_manifest(FORM_SCHEMA_DIR):
        schema = (
            await session.execute(
                select(FormSchema).where(FormSchema.insurance_type == insurance_type)
            )
        ).scalar_one_or_none()
        if schema is None:
            schema = FormSchema(insurance_type=insurance_type, name=name)
            session.add(schema)
        else:
            schema.name = name
        await session.flush()

        published = (
            await session.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == schema.id,
                    SchemaVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        if published is not None and _same_document(published.schema_json, doc):
            summary.append(f"{insurance_type} '{name}' v{published.version} (unchanged)")
            continue

        max_version = (
            await session.execute(
                select(func.max(SchemaVersion.version)).where(SchemaVersion.schema_id == schema.id)
            )
        ).scalar()
        next_version = (max_version or 0) + 1
        if published is not None:
            published.status = VersionStatus.DRAFT
            await session.flush()  # free the partial unique index before publishing anew
        session.add(
            SchemaVersion(
                schema_id=schema.id,
                version=next_version,
                schema_json=doc,
                status=VersionStatus.PUBLISHED,
                published_at=func.now(),
            )
        )
        await session.flush()
        summary.append(f"{insurance_type} '{name}' v{next_version} (published)")
    return summary


async def _seed_prompts(session: AsyncSession) -> list[str]:
    """Generate + publish one prompt per published form schema, compiled from the
    schema document itself (`compile_prompt_document`): per-task nested JSON of
    the task-level prompt + question lists. Mirrors `_seed_form_schemas`:
    idempotent and keyed on `(schema_id, name)`. Re-running with an unchanged
    schema is a no-op; a changed schema demotes the current published prompt
    version to DRAFT and publishes a new one.

    `prompt_version.schema_version_id` is NOT NULL + RESTRICT, so a prompt is only
    generated once its schema has a published version (form schemas are seeded
    just before this); schemas without one are skipped with a warning."""
    summary: list[str] = []
    schemas = (
        (await session.execute(select(FormSchema).order_by(FormSchema.insurance_type)))
        .scalars()
        .all()
    )
    for schema in schemas:
        insurance_type = schema.insurance_type
        published_schema = (
            await session.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == schema.id,
                    SchemaVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        if published_schema is None:
            summary.append(f"{insurance_type} (skipped — no published schema)")
            continue

        schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
        doc = compile_prompt_document(schema_doc)
        name = f"{schema_doc.name} Prompt"

        prompt = (
            await session.execute(
                select(Prompt).where(Prompt.schema_id == schema.id, Prompt.name == name)
            )
        ).scalar_one_or_none()
        if prompt is None:
            prompt = Prompt(schema_id=schema.id, name=name)
            session.add(prompt)
            await session.flush()

        published = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_id == prompt.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        if published is not None and _same_document(published.composite_json, doc):
            summary.append(f"{insurance_type} '{name}' v{published.version} (unchanged)")
            continue

        max_version = (
            await session.execute(
                select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
            )
        ).scalar()
        next_version = (max_version or 0) + 1
        if published is not None:
            # uq_prompt_version_published_per_prompt allows only one published row
            # per prompt; free it by demoting the old version before publishing anew.
            published.status = VersionStatus.DRAFT
            await session.flush()
        session.add(
            PromptVersion(
                prompt_id=prompt.id,
                schema_version_id=published_schema.id,
                version=next_version,
                composite_json=doc,
                status=VersionStatus.PUBLISHED,
            )
        )
        await session.flush()
        summary.append(f"{insurance_type} '{name}' v{next_version} (published)")
    return summary


# Global integration catalog. `credentials_schema` declares the credential shape a
# tenant supplies (validated + sealed by the integrations endpoint). Keyed on the
# unique `name`; idempotent — re-running refreshes the schema.
INTEGRATION_TYPES: list[dict[str, Any]] = [
    {"name": "livekit_outbound_trunk_id", "credentials_schema": {"trunk_id": "string"}},
]


async def _seed_integration_types(session: AsyncSession) -> list[str]:
    existing = {it.name: it for it in (await session.execute(select(IntegrationType))).scalars()}
    for spec in INTEGRATION_TYPES:
        itype = existing.get(spec["name"])
        if itype is None:
            session.add(
                IntegrationType(name=spec["name"], credentials_schema=spec["credentials_schema"])
            )
        else:
            itype.credentials_schema = spec["credentials_schema"]
    await session.flush()
    return [spec["name"] for spec in INTEGRATION_TYPES]


@asynccontextmanager
async def _seeding_session() -> AsyncIterator[AsyncSession]:
    """A transactional session for a seed run, with the engine disposed on exit.
    Seeds run as the privileged DB user (bypasses RLS); see module docstring."""
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def seed() -> None:
    async with _seeding_session() as session:
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)
        tenant_id = await _seed_tenant(session)
        await _seed_password_provider(session, tenant_id)
        await _seed_admin_user(session, tenant_id)
        schema_summary = await _seed_form_schemas(session)
        prompt_summary = await _seed_prompts(session)
        integration_types = await _seed_integration_types(session)
    print(
        f"seeded: {len(permission_ids)} permissions,"
        f" global system roles {sorted(SYSTEM_ROLES)},"
        f" tenant '{SAMPLE_TENANT_NAME}' (slug '{SAMPLE_TENANT_SLUG}', {tenant_id}),"
        f" password provider enabled, admin user '{SAMPLE_ADMIN_EMAIL}' (TENANT_ADMIN),"
        f" form schemas {schema_summary},"
        f" prompts {prompt_summary},"
        f" integration types {integration_types}"
    )
    print(
        "local dev login: "
        f"POST /api/v1/tenants/{SAMPLE_TENANT_SLUG}/auth/login "
        f'{{"email": "{SAMPLE_ADMIN_EMAIL}", "password": "{SAMPLE_ADMIN_PASSWORD}"}}'
    )


async def seed_schemas() -> None:
    """Seed ONLY the baseline form schemas (`just seed-schemas`)."""
    async with _seeding_session() as session:
        summary = await _seed_form_schemas(session)
    print(f"seeded form schemas: {summary}")


async def seed_prompts() -> None:
    """Seed ONLY the baseline prompts (`just seed-prompts`). Each prompt binds to
    its target schema's published version, so the form schemas must already be
    seeded (run `just seed` or `just seed-schemas` first)."""
    async with _seeding_session() as session:
        summary = await _seed_prompts(session)
    print(f"seeded prompts: {summary}")


if __name__ == "__main__":
    if "--schemas" in sys.argv:
        asyncio.run(seed_schemas())
    elif "--prompts" in sys.argv:
        asyncio.run(seed_prompts())
    else:
        asyncio.run(seed())
