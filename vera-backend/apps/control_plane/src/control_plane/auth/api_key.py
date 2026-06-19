"""Inbound API-key authentication (spec §4.1.2 / §7.1) — the machine-to-machine
caller class, distinct from the human console session.

The issued token is ``vk_<tenant_id>.<key_id>.<secret>``. Only a per-key random
``salt`` and ``key_hash = sha256(salt || secret)`` are stored; the raw secret is
shown once at issuance and never persisted, so a DB leak cannot replay a key.

Verification embeds the tenant in the token so the lookup can run inside a
tenant-scoped session — ``api_key`` is under FORCE RLS, and a tenant-less query
returns zero rows. The tenant the token claims is therefore proven by RLS (a
key_id from another tenant is simply invisible) AND by the secret hash. Expiry and
revocation are filtered in SQL against the DB clock (``now()``), so no app clock
takes part.

This module provides the verifier + a `require_scope(...)` dependency for the
future key-authenticated ingress endpoints (intake / queue / export, spec §4.3);
issuance/revocation live in `api/v1/api_keys.py`.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from control_plane.deps import get_sessionmaker
from control_plane.exceptions import CustomAPIException, DefaultExceptionCode, UnauthorizedError
from vera_core.db import tenant_session
from vera_core.models import ApiKey

_TOKEN_PREFIX = "vk_"
_SALT_BYTES = 16
_SECRET_BYTES = 32

# The fixed vocabulary of capabilities an inbound key may carry — the single source
# of truth for both create-time validation and the UI's scope picker (so an admin
# chooses from this set, never free-types a string that would silently never match).
# A key holds exactly one scope (`ApiKey.scope` is a single column, checked by exact
# match in `require_scope`). Add an entry here as each key-authenticated ingress
# endpoint lands (intake / queue / export, spec §4.3); only `intake:write` is built.
API_KEY_SCOPES: Final[dict[str, str]] = {
    "intake:write": "Submit patient intake payloads",
}

_api_key_bearer = HTTPBearer(auto_error=False)


def new_salt() -> bytes:
    return secrets.token_bytes(_SALT_BYTES)


def new_secret() -> str:
    return secrets.token_urlsafe(_SECRET_BYTES)


def hash_secret(salt: bytes, secret: str) -> bytes:
    """The stored hash for a key: SHA-256 over the per-key salt and the secret.
    The secret is high-entropy (256-bit random), so a single salted hash is
    sufficient — no slow KDF is needed as it would be for a low-entropy password."""
    return hashlib.sha256(salt + secret.encode()).digest()


def verify_secret(salt: bytes, secret: str, key_hash: bytes) -> bool:
    """Constant-time check that `secret` matches the stored `key_hash`."""
    return hmac.compare_digest(hash_secret(salt, secret), key_hash)


def format_token(tenant_id: UUID, key_id: UUID, secret: str) -> str:
    return f"{_TOKEN_PREFIX}{tenant_id}.{key_id}.{secret}"


@dataclass(frozen=True)
class _ParsedToken:
    tenant_id: UUID
    key_id: UUID
    secret: str


def parse_token(token: str) -> _ParsedToken | None:
    """Split a presented token into (tenant_id, key_id, secret), or None if it is
    not a well-formed Vera API-key token. Never raises."""
    if not token.startswith(_TOKEN_PREFIX):
        return None
    parts = token[len(_TOKEN_PREFIX) :].split(".")
    if len(parts) != 3:
        return None
    raw_tenant, raw_key, secret = parts
    if not secret:
        return None
    try:
        return _ParsedToken(tenant_id=UUID(raw_tenant), key_id=UUID(raw_key), secret=secret)
    except ValueError:
        return None


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """The authenticated machine caller. `tenant_id` is the sole authority for
    row-level isolation downstream (spec §7.1); `scope` gates the operation."""

    tenant_id: UUID
    key_id: UUID
    scope: str


async def resolve_api_key(
    token: str, sessionmaker: async_sessionmaker[AsyncSession]
) -> ApiKeyPrincipal | None:
    """Authenticate a presented token to an `ApiKeyPrincipal`, or None if it is
    unknown, malformed, revoked, expired, or the secret does not match. The lookup
    runs inside a tenant-scoped session (RLS confines it to the token's tenant);
    expiry/revocation are filtered against the DB clock."""
    parsed = parse_token(token)
    if parsed is None:
        return None
    async with tenant_session(sessionmaker, parsed.tenant_id) as session:
        row = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.id == parsed.key_id,
                    ApiKey.revoked.is_(False),
                    or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > func.now()),
                )
            )
        ).scalar_one_or_none()
        if row is None or not verify_secret(row.salt, parsed.secret, row.key_hash):
            return None
        return ApiKeyPrincipal(tenant_id=row.tenant_id, key_id=row.id, scope=row.scope)


async def api_key_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_api_key_bearer)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> ApiKeyPrincipal:
    """Dependency: resolve the Bearer API key to a principal or fail 401 (uniform,
    no key enumeration)."""
    if credentials is None:
        raise UnauthorizedError(message="missing API key")
    principal = await resolve_api_key(credentials.credentials, sessionmaker)
    if principal is None:
        raise UnauthorizedError(message="invalid API key")
    return principal


def require_scope(scope: str) -> Any:
    """Dependency factory: 403 unless the authenticated key carries `scope`
    (e.g. `intake:write`). Returns the principal for the route to use."""
    if scope not in API_KEY_SCOPES:
        # Fail loud at import (routes build this at definition time) — a typo'd scope
        # would otherwise be a silent gate no key could ever satisfy, since issuance
        # rejects off-catalog scopes too. The catalog is the single source of truth.
        raise ValueError(f"unknown API key scope: {scope!r}")

    async def dependency(
        principal: Annotated[ApiKeyPrincipal, Depends(api_key_principal)],
    ) -> ApiKeyPrincipal:
        if principal.scope != scope:
            raise CustomAPIException(
                DefaultExceptionCode.FORBIDDEN, message=f"API key lacks scope {scope}"
            )
        return principal

    return Depends(dependency)
