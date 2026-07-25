"""Platform (SUPER_ADMIN) routes — scoped elevation lifecycle (ADR-0006 §B).

These routes have NO tenant in the path and NO tenant context: authorization is
`platform_require(...)`, which resolves the caller's GLOBAL grants over a platform
session, so only a SUPER_ADMIN passes. Every grant create/end is recorded in the
auth audit log (null-tenant). The grant itself is what later authorizes the operator
into one tenant, under that tenant's own RLS — there is no RLS bypass here.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings
from control_plane.auth import elevation
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.platform_tenant_config import set_tenant_observer_enabled
from control_plane.auth.rbac import platform_require
from control_plane.deps import (
    client_ip,
    get_auth_audit,
    get_idempotency_store,
    platform_scoped_session,
)
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.idempotency import (
    PLATFORM_IDEM_SCOPE,
    claim_or_conflict,
    require_idempotency_key,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuthAuditSink, emit_auth_event
from vera_core.models import Tenant
from vera_core.models.enums import AuthEvent

router = APIRouter(prefix="/platform")

# Upper bound on a single break-glass window. A grant can always be ended early;
# this caps the blast radius if it is forgotten.
MAX_ELEVATION_MINUTES = 8 * 60

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
AuthAudit = Annotated[AuthAuditSink, Depends(get_auth_audit)]


class CreateElevationRequest(BaseModel):
    target_tenant_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    duration_minutes: int = Field(gt=0, le=MAX_ELEVATION_MINUTES)


class ElevationResponse(BaseModel):
    id: UUID
    target_tenant_id: UUID
    reason: str
    granted_at: datetime
    expires_at: datetime
    ended_at: datetime | None


def _to_response(grant: elevation.ElevationGrant) -> ElevationResponse:
    return ElevationResponse(
        id=grant.id,
        target_tenant_id=grant.target_tenant_id,
        reason=grant.reason,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        ended_at=grant.ended_at,
    )


@router.post(
    "/elevations",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[ElevationResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def create_elevation(
    body: CreateElevationRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, platform_require("platform:elevations:create")],
) -> ResponseModel[ElevationResponse]:
    try:
        grant_id = await elevation.create_grant(
            session,
            super_admin_user_id=caller.user_id,
            target_tenant_id=body.target_tenant_id,
            reason=body.reason,
            duration_minutes=body.duration_minutes,
        )
    except IntegrityError as exc:
        raise _map_create_error(exc) from exc

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_ELEVATION_GRANTED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"elevation_id": str(grant_id), "target_tenant": str(body.target_tenant_id)},
    )
    grant = await elevation.active_grant_for(
        session, super_admin_user_id=caller.user_id, target_tenant_id=body.target_tenant_id
    )
    if grant is None:  # pragma: no cover — just created, must be active
        raise CustomAPIException(DefaultExceptionCode.INTERNAL_SERVER_ERROR)
    return ok(_to_response(grant))


@router.post(
    "/elevations/{elevation_id}/end",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def end_elevation(
    elevation_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, platform_require("platform:elevations:end")],
) -> ResponseModel[None]:
    ended = await elevation.end_grant(session, elevation_id)
    if not ended:
        raise NotFoundError(message="no active elevation with that id")
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_ELEVATION_ENDED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"elevation_id": str(elevation_id)},
    )
    return ok(None, message="Elevation ended.")


@router.get(
    "/elevations",
    response_model=ResponseModel[list[ElevationResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_active_elevations(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require("platform:elevations:read")],
) -> ResponseModel[list[ElevationResponse]]:
    grants = await elevation.active_grants(session)
    return ok([_to_response(g) for g in grants])


class TenantSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    observer_enabled: bool


@router.get(
    "/tenants",
    response_model=ResponseModel[list[TenantSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_tenants(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require("platform:elevations:read")],
) -> ResponseModel[list[TenantSummary]]:
    """Active tenants (id / name / slug + the AI form-filling switch — org metadata, not
    PHI) for the elevation tenant picker and the platform-settings screen. Readable here
    via the tenant_platform_read RLS policy (migration 0022); gated on
    platform:elevations:read since it serves the elevation workflow (a SUPER_ADMIN also
    holds platform:tenants:manage)."""
    rows = (
        await session.execute(
            select(Tenant.id, Tenant.name, Tenant.slug, Tenant.observer_enabled)
            .where(Tenant.status == "active")
            .order_by(Tenant.name)
        )
    ).all()
    return ok(
        [
            TenantSummary(id=r.id, name=r.name, slug=r.slug, observer_enabled=r.observer_enabled)
            for r in rows
        ]
    )


class SetTenantObserverRequest(BaseModel):
    enabled: bool


class TenantObserverResponse(BaseModel):
    tenant_id: UUID
    observer_enabled: bool


@router.post(
    "/tenants/{tenant_id}/observer",
    response_model=ResponseModel[TenantObserverResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def set_tenant_observer(
    tenant_id: UUID,
    body: SetTenantObserverRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require("platform:tenants:manage")],
) -> ResponseModel[TenantObserverResponse]:
    """Toggle a tenant's AI form-filling (observer) feature. Writes through the
    platform_set_tenant_observer_enabled SECURITY DEFINER fn (the tenant table's platform
    RLS policy is SELECT-only), audited null-tenant like every other /platform authz."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    flipped = await set_tenant_observer_enabled(session, tenant_id=tenant_id, enabled=body.enabled)
    if not flipped:
        raise NotFoundError(message="no such tenant")
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_OBSERVER_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_tenant": str(tenant_id), "observer_enabled": body.enabled},
    )
    return ok(TenantObserverResponse(tenant_id=tenant_id, observer_enabled=body.enabled))


def _map_create_error(exc: IntegrityError) -> CustomAPIException:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate == "23505":  # unique_violation — operator already has an active grant
        return CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="operator already holds an active elevation",
        )
    if sqlstate == "23503":  # foreign_key_violation — unknown tenant or operator
        return NotFoundError(message="unknown tenant")
    if sqlstate == "23514":  # check_violation — empty reason / non-future expiry
        return BadRequestError(message="invalid elevation")
    return BadRequestError(message="elevation rejected")
