"""Login provider resolution — per-tenant (sso_provider) and platform (platform_login_provider).

Login is provider-driven: a tenant's `sso_provider` row (or the single global
`platform_login_provider` row, for platform operators) says which IdP is enabled and
whether MFA is enforced. `password` is the first-class local provider; GCIP/SAML/OIDC are
added later behind the same table without touching the verify path. The tenant lookup runs
inside the request's tenant-scoped session (RLS confines it to the one tenant); the platform
lookup runs inside a `platform_session` (RLS exposes only the global NULL-tenant rows).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import PlatformLoginProvider, SsoProvider


@dataclass(frozen=True)
class LoginProvider:
    provider_type: str
    enforce_mfa: bool


async def resolve_login_provider(
    session: AsyncSession, tenant_id: UUID, provider_type: str
) -> LoginProvider | None:
    """The enabled provider of `provider_type` for this tenant, or None if the
    tenant has not enabled it (login must then be refused)."""
    row = (
        await session.execute(
            select(SsoProvider).where(
                SsoProvider.tenant_id == tenant_id,
                SsoProvider.provider_type == provider_type,
                SsoProvider.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return LoginProvider(provider_type=row.provider_type, enforce_mfa=row.enforce_mfa)


async def resolve_platform_login_provider(
    session: AsyncSession, provider_type: str
) -> LoginProvider | None:
    """The enabled global platform-operator provider of `provider_type`, or None if it
    is not enabled (platform login must then be refused). No tenant scope — the single
    `platform_login_provider` row is NULL-tenant and resolves only inside a
    `platform_session` (RLS exposes the global rows there)."""
    row = (
        await session.execute(
            select(PlatformLoginProvider).where(
                PlatformLoginProvider.provider_type == provider_type,
                PlatformLoginProvider.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return LoginProvider(provider_type=row.provider_type, enforce_mfa=row.enforce_mfa)
