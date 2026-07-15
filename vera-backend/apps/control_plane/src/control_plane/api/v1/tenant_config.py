"""Tenant runtime-config surface (spec Fig 7 knobs). A TENANT_ADMIN reads/edits the
tenant's persona overlay. Gated by `tenant:config:manage` and audited. persona_tweak
is admin-authored, non-PHI config — no `phi:read` gate, no PHI-access audit — but the
mutation is recorded in the auth audit log (field names only)."""

from fastapi import APIRouter, Request
from sqlalchemy import select

from control_plane.api.v1.common import AppSettings, AuthAudit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.responses import ResponseModel, ok
from vera_core.audit import emit_auth_event
from vera_core.config import Settings
from vera_core.models import Tenant
from vera_core.models.enums import AuthEvent
from vera_core.schemas import PersonaTweak, RetentionPolicy, RetentionPolicyUpdate

router = APIRouter(tags=["tenant-config"])


async def _load_tenant(session: TenantSession, tenant_id: TenantId) -> Tenant:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()  # RLS on `tenant` keys on id → only the caller's own row
    if tenant is None:  # pragma: no cover — an authenticated tenant always has its row
        raise NotFoundError(message="tenant not found")
    return tenant


@router.get(
    "/tenant/config/persona",
    response_model=ResponseModel[PersonaTweak],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_persona(
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[PersonaTweak]:
    tenant = await _load_tenant(session, tenant_id)
    return ok(PersonaTweak.model_validate(tenant.persona_tweak))


@router.put(
    "/tenant/config/persona",
    response_model=ResponseModel[PersonaTweak],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def put_persona(
    body: PersonaTweak,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[PersonaTweak]:
    tenant = await _load_tenant(session, tenant_id)
    stored = body.model_dump(exclude_none=True)
    tenant.persona_tweak = stored
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.PERSONA_TWEAK_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"fields": sorted(stored.keys())},  # field names only, never values
    )
    return ok(body)


def _retention_policy(days: int | None, settings: Settings) -> RetentionPolicy:
    """Build the retention-policy response — the effective knob plus the platform
    default the UI renders when the tenant hasn't set one."""
    return RetentionPolicy(
        retention_days=days,
        default_days=settings.recording_retention_days_default,
    )


@router.get(
    "/tenant/config/retention",
    response_model=ResponseModel[RetentionPolicy],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_retention_policy(
    tenant_id: TenantId,
    session: TenantSession,
    settings: AppSettings,
    _caller: VerifiedIdentity = require("recordings:manage"),
) -> ResponseModel[RetentionPolicy]:
    tenant = await _load_tenant(session, tenant_id)
    return ok(_retention_policy(tenant.recording_retention_days, settings))


@router.patch(
    "/tenant/config/retention",
    response_model=ResponseModel[RetentionPolicy],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def patch_retention_policy(
    body: RetentionPolicyUpdate,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    settings: AppSettings,
    audit: AuthAudit,
    caller: VerifiedIdentity = require("recordings:manage"),
) -> ResponseModel[RetentionPolicy]:
    tenant = await _load_tenant(session, tenant_id)
    old_days = tenant.recording_retention_days
    tenant.recording_retention_days = body.retention_days
    # Policy-change before/after (spec decision 5). Config values, not PHI.
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.RETENTION_POLICY_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"old_days": old_days, "new_days": body.retention_days},
    )
    return ok(_retention_policy(body.retention_days, settings))
