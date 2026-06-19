"""Inbound API-key management (spec §4.1.2) — a TENANT_ADMIN issues, lists, and
revokes the per-tenant keys external systems use for machine-to-machine ingress.

Gated by `apikeys:manage`. The plaintext token is returned **once** at creation and
never stored or logged; only a per-key salt + SHA-256 hash persist (auth/api_key.py).
The verifier that consumes these keys lives in `auth/api_key.py`; the ingress
endpoints that require a scope (intake / queue / export, spec §4.3) come later.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from control_plane.api.v1.common import (
    AppSettings,
    AuthAudit,
    TenantId,
    TenantSession,
    emit_auth_event,
)
from control_plane.auth import api_key
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip, get_idempotency_store
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.idempotency import claim_or_conflict, require_idempotency_key
from control_plane.responses import ResponseModel, ok
from vera_core.db import uuid7
from vera_core.models import ApiKey
from vera_core.models.enums import AuthEvent

router = APIRouter(tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scope: str = Field(min_length=1, max_length=128)
    expires_at: datetime | None = None


class CreatedApiKeyResponse(BaseModel):
    """The one and only time the plaintext `token` is returned — store it now."""

    id: UUID
    name: str
    scope: str
    expires_at: datetime | None
    token: str


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    scope: str
    expires_at: datetime | None
    revoked: bool


class ApiKeyScopeResponse(BaseModel):
    """One selectable capability for the scope picker — `code` is stored on the key,
    `description` is the human label the admin UI renders."""

    code: str
    description: str


def _to_response(row: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=row.id,
        name=row.name,
        scope=row.scope,
        expires_at=row.expires_at,
        revoked=row.revoked,
    )


@router.post(
    "/api-keys",
    response_model=ResponseModel[CreatedApiKeyResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def create_api_key(
    body: CreateApiKeyRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: VerifiedIdentity = require("apikeys:manage"),
) -> ResponseModel[CreatedApiKeyResponse]:
    if body.scope not in api_key.API_KEY_SCOPES:
        # Reject unknown scopes at issuance so a key can never carry a capability
        # string that no endpoint will ever match (exact-match `require_scope`).
        raise BadRequestError(message=f"unknown API key scope: {body.scope}")
    await claim_or_conflict(
        get_idempotency_store(request),
        tenant_id,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    key_id = uuid7()
    salt = api_key.new_salt()
    secret = api_key.new_secret()
    row = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        name=body.name,
        salt=salt,
        key_hash=api_key.hash_secret(salt, secret),
        scope=body.scope,
        expires_at=body.expires_at,
        revoked=False,
    )
    session.add(row)
    token = api_key.format_token(tenant_id, key_id, secret)
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.API_KEY_CREATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        # Never the token/secret — only non-sensitive metadata.
        meta={"api_key_id": str(key_id), "scope": body.scope},
    )
    return ok(
        CreatedApiKeyResponse(
            id=key_id, name=body.name, scope=body.scope, expires_at=body.expires_at, token=token
        ),
        message="Store this token now — it will not be shown again.",
    )


@router.get(
    "/api-keys",
    response_model=ResponseModel[list[ApiKeyResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_api_keys(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("apikeys:manage"),
) -> ResponseModel[list[ApiKeyResponse]]:
    rows = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars().all()
    return ok([_to_response(r) for r in rows])


@router.get(
    "/api-keys/scopes",
    response_model=ResponseModel[list[ApiKeyScopeResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_api_key_scopes(
    _tenant_id: TenantId,
    _caller: VerifiedIdentity = require("apikeys:manage"),
) -> ResponseModel[list[ApiKeyScopeResponse]]:
    """The fixed scope vocabulary for the create form — backend-owned so the picker
    never drifts from what `require_scope` will accept. Static, non-PHI; no DB hit."""
    return ok(
        [
            ApiKeyScopeResponse(code=code, description=desc)
            for code, desc in api_key.API_KEY_SCOPES.items()
        ]
    )


@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    _caller: VerifiedIdentity = require("apikeys:manage"),
) -> ResponseModel[None]:
    row = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(message="no API key with that id")
    row.revoked = True
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.API_KEY_REVOKED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"api_key_id": str(key_id)},
    )
    return ok(None, message="API key revoked.")
