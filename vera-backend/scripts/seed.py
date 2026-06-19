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
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.password import hash_password
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.models import (
    AppUser,
    Permission,
    Role,
    RolePermission,
    SsoProvider,
    Tenant,
    UserIdentity,
    UserRole,
)
from vera_core.models.enums import ProviderKind
from vera_core.models.rbac_defaults import ALL_PERMISSIONS, SYSTEM_ROLES

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


async def seed() -> None:
    engine = create_engine(get_settings())
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as session, session.begin():
            permission_ids = await _seed_permissions(session)
            await _seed_system_roles(session, permission_ids)
            tenant_id = await _seed_tenant(session)
            await _seed_password_provider(session, tenant_id)
            await _seed_admin_user(session, tenant_id)
        print(
            f"seeded: {len(permission_ids)} permissions,"
            f" global system roles {sorted(SYSTEM_ROLES)},"
            f" tenant '{SAMPLE_TENANT_NAME}' (slug '{SAMPLE_TENANT_SLUG}', {tenant_id}),"
            f" password provider enabled, admin user '{SAMPLE_ADMIN_EMAIL}' (TENANT_ADMIN)"
        )
        print(
            "local dev login: "
            f"POST /api/v1/tenants/{SAMPLE_TENANT_SLUG}/auth/login "
            f'{{"email": "{SAMPLE_ADMIN_EMAIL}", "password": "{SAMPLE_ADMIN_PASSWORD}"}}'
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
