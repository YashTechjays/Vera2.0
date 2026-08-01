"""Step 1 of the per-request authz chain: WHO is calling.

Every request carries an opaque session token minted at login (see
auth/session.py). Verification sits behind the TokenVerifier protocol so the
verify path never has to know HOW the session was established — password+MFA
today, GCIP/SSO later. The verified identity carries the app_user id (the stable
internal key RBAC resolves against) and the tenant UUID; the tenant context
(step 2) and RLS both key off the tenant, and nothing downstream ever trusts a
tenant id sent by the client.

`tenant_id` is `None` for a platform operator (a SUPER_ADMIN, `app_user.account_type
= 'platform'`): they have no home tenant and no standing tenant access. Cross-tenant
access comes only from a scoped elevation grant (ADR-0006); `tenant_context` denies a
null-tenant identity on a tenant route unless an active elevation grant covers it.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from vera_core.models.enums import AccountType


class InvalidTokenError(Exception):
    """Verification failed — the API layer turns this into a 401."""


@dataclass(frozen=True)
class VerifiedIdentity:
    user_id: UUID  # app_user.id — the key RBAC resolves against
    subject: str  # provider subject (email for password, gcip_uid for SSO)
    email: str
    tenant_id: UUID | None  # None ⇒ a platform operator (no home tenant)
    account_type: AccountType  # definitive platform-vs-tenant signal (ADR §3.5.9)
    # This login's non-secret handle (SessionData.session_id) — what tells one user's
    # concurrent browsers apart, and so what names their LiveKit participant.
    session_id: UUID
    # The home tenant's URL slug. Used for display and invite-URL construction only;
    # `tenant_context` derives the operating tenant from the verified session UUID,
    # not this field. None for a platform operator or a slug-less session.
    tenant_slug: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity:
        """Return the verified identity or raise InvalidTokenError."""
        ...
