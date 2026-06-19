"""Per-tenant login provider resolution (sso_provider).

Login is provider-driven: the tenant's `sso_provider` row says which IdP is
enabled and whether MFA is enforced. `password` is the first-class local
provider; GCIP/SAML/OIDC are added later behind the same table without touching
the verify path. The lookup runs inside the request's tenant-scoped session, so
RLS already confines it to the one tenant.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import SsoProvider


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
