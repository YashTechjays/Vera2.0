"""Platform (SUPER_ADMIN) tenant administration — the tenant catalog and its config.

Split out of platform.py, which owns the elevation lifecycle; these routes are the
tenant-admin half of the same platform plane. NO tenant in the path and NO tenant
context: authorization is `platform_require(...)` over a platform session. Tenant rows
are org metadata/config, never PHI. The tenant table's platform RLS policy is
SELECT-only, so every write goes through a SECURITY DEFINER fn — never an RLS bypass.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import (
    AppSettings,
    AuthAudit,
    Email,
    Invites,
    Resolver,
    platform_tier_role_ids,
    roles_grant_platform_permission,
    send_invite_email,
)
from control_plane.api.v1.users import InviteUserRequest, InviteUserResponse
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import INVITE_NS, InviteData
from control_plane.auth.platform_tenant_config import create_tenant as create_tenant_row
from control_plane.auth.platform_tenant_config import (
    set_tenant_observer_enabled,
    set_tenant_retry_config,
    set_tenant_status,
)
from control_plane.auth.platform_tenant_config import update_tenant as update_tenant_row
from control_plane.auth.platform_tenant_users import invite_tenant_user as invite_tenant_user_row
from control_plane.auth.platform_tenant_users import list_tenant_users as list_tenant_users_rows
from control_plane.auth.rbac import platform_require
from control_plane.auth.tenant_slug import is_valid_slug, normalize_slug
from control_plane.deps import client_ip, get_idempotency_store, platform_scoped_session
from control_plane.exceptions import (
    ConflictError,
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
from vera_core.audit import emit_auth_event
from vera_core.db import uuid7
from vera_core.models import Role, Tenant
from vera_core.models.enums import AuthEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform")

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]

# Named because the write gate and `list_tenants`' disclosure check must stay the same
# code — a typo in either would silently widen or break the split.
TENANTS_MANAGE = "platform:tenants:manage"

TenantStatus = Literal["active", "deactivated"]
TenantListStatus = Literal["active", "deactivated", "all"]


class TenantSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    region: str | None
    created_at: datetime
    # None means "not disclosed to this caller" (no platform:tenants:manage), not "off" —
    # the elevation tenant picker reads this endpoint without holding that permission.
    observer_enabled: bool | None = None
    # Same tri-state disclosure rule as observer_enabled — gated on platform:tenants:manage.
    auto_retry_enabled: bool | None = None
    retry_fill_threshold: float | None = None


class TenantDetail(BaseModel):
    """Every tenant setting the platform edit form may show — nothing withheld, since
    this route (unlike the list) always requires platform:tenants:manage. `slug` is
    read-only here: immutable once set (ADR — it is in the login URL)."""

    id: UUID
    name: str
    slug: str
    status: str
    region: str | None
    created_at: datetime
    observer_enabled: bool
    auto_retry_enabled: bool
    retry_fill_threshold: float
    max_agents_per_va: int
    max_concurrent_calls: int
    max_retries: int
    queue_expiry_hours: int
    recording_retention_days: int | None


def _to_detail(t: Tenant) -> TenantDetail:
    return TenantDetail(
        id=t.id,
        name=t.name,
        slug=t.slug,
        status=t.status,
        region=t.region,
        created_at=t.created_at,
        observer_enabled=t.observer_enabled,
        auto_retry_enabled=t.auto_retry_enabled,
        retry_fill_threshold=float(t.retry_fill_threshold),
        max_agents_per_va=t.max_agents_per_va,
        max_concurrent_calls=t.max_concurrent_calls,
        max_retries=t.max_retries,
        queue_expiry_hours=t.queue_expiry_hours,
        recording_retention_days=t.recording_retention_days,
    )


async def _load_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise NotFoundError(message="no such tenant")
    return tenant


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
    resolver: Resolver,
    caller: Annotated[VerifiedIdentity, platform_require("platform:elevations:read")],
    status_filter: Annotated[TenantListStatus | None, Query(alias="status")] = None,
) -> ResponseModel[list[TenantSummary]]:
    """Tenants (id / name / slug / status — org metadata, not PHI) for the elevation
    tenant picker, the platform-settings screen, and the Tenants admin table. Readable
    here via the tenant_platform_read RLS policy (migration 0022); gated on
    platform:elevations:read because the elevation picker is the one caller that must
    keep working with no other permission.

    `status` widens beyond the active-only default, but ONLY for a caller holding
    platform:tenants:manage — a request for `deactivated`/`all` is silently narrowed
    back to active for anyone else, so the elevation picker (elevations:read only) can
    never be offered a switched-off tenant even if it asked for one. The AI form-filling
    switch has the identical tri-state disclosure rule for the same reason: the two
    permissions happen to sit on the same seeded role today, and the whole point of
    minting them separately is that they can be granted apart."""
    # A cache hit in practice: platform_require just resolved the same (session, None,
    # user_id) triple and populated the permission cache.
    _, permissions = await resolver.effective_permissions(session, None, caller.user_id)
    may_manage = TENANTS_MANAGE in permissions

    def _disclose[T](value: T, cast: Callable[[T], T]) -> T | None:
        """Gate a platform:tenants:manage-only field: withheld (None) unless the
        caller holds it, so a widened set of managed fields can't skip the check."""
        return cast(value) if may_manage else None

    # Only a manage caller that actually asked widens past the active-only default.
    effective_status: TenantListStatus = "active"
    if may_manage and status_filter is not None:
        effective_status = status_filter

    stmt = select(
        Tenant.id,
        Tenant.name,
        Tenant.slug,
        Tenant.status,
        Tenant.region,
        Tenant.created_at,
        Tenant.observer_enabled,
        Tenant.auto_retry_enabled,
        Tenant.retry_fill_threshold,
    ).order_by(Tenant.name)
    if effective_status != "all":
        stmt = stmt.where(Tenant.status == effective_status)
    rows = (await session.execute(stmt)).all()
    return ok(
        [
            TenantSummary(
                id=r.id,
                name=r.name,
                slug=r.slug,
                status=r.status,
                region=r.region,
                created_at=r.created_at,
                observer_enabled=_disclose(r.observer_enabled, bool),
                auto_retry_enabled=_disclose(r.auto_retry_enabled, bool),
                retry_fill_threshold=_disclose(r.retry_fill_threshold, float),
            )
            for r in rows
        ]
    )


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=63)
    region: str | None = Field(default=None, max_length=32)


@router.post(
    "/tenants",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[TenantDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantDetail]:
    """Create the tenant ORGANISATION only — no user, no invite. Inviting the first
    tenant user is a separate step (`POST /platform/tenants/{id}/users/invitations`),
    so a half-onboarded tenant with zero users is a distinguishable, visible state
    rather than something this endpoint tries to prevent. Writes through the
    platform_create_tenant SECURITY DEFINER fn (the tenant table's platform RLS policy
    is SELECT-only), audited null-tenant like every other /platform authz."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    slug = normalize_slug(body.slug)
    if not is_valid_slug(slug):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="slug must be a lowercase DNS-label (letters, digits, hyphens)",
        )
    tenant_id = uuid7()
    try:
        await create_tenant_row(
            session, tenant_id=tenant_id, name=body.name, slug=slug, region=body.region
        )
    except IntegrityError as exc:
        raise ConflictError(message="a tenant with that slug already exists") from exc

    tenant = await _load_tenant(session, tenant_id)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_CREATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_tenant": str(tenant_id), "slug": slug},
    )
    return ok(_to_detail(tenant))


@router.get(
    "/tenants/{tenant_id}",
    response_model=ResponseModel[TenantDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_tenant(
    tenant_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantDetail]:
    return ok(_to_detail(await _load_tenant(session, tenant_id)))


class UpdateTenantRequest(BaseModel):
    """Every field the platform edit form may change. `slug` and `status` are not
    fields on this model at all — extra="forbid" turns a caller trying to smuggle
    either in through the PATCH body into a 422, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=32)
    observer_enabled: bool | None = None
    auto_retry_enabled: bool | None = None
    retry_fill_threshold: float | None = Field(default=None, ge=0, le=1)
    # Bounds mirror ConcurrencyConfigUpdate (schemas/dto.py) — the same knobs, same limits.
    max_agents_per_va: int | None = Field(default=None, ge=1, le=20)
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=100)
    max_retries: int | None = Field(default=None, ge=0)
    queue_expiry_hours: int | None = Field(default=None, ge=1)
    recording_retention_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateTenantRequest":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


@router.patch(
    "/tenants/{tenant_id}",
    response_model=ResponseModel[TenantDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_tenant(
    tenant_id: UUID,
    body: UpdateTenantRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantDetail]:
    """Edit any tenant setting except slug/status (immutable / owned by
    deactivate-reactivate). Naturally idempotent — every field is set to an absolute
    value, never incremented — so this route skips the Idempotency-Key gate, matching
    tenant_config.py's patch_retention_policy/patch_concurrency_config. Writes through
    the platform_update_tenant SECURITY DEFINER fn (the tenant table's platform RLS
    policy is SELECT-only), audited null-tenant like every other /platform authz."""
    provided = body.model_fields_set
    if "name" in provided and body.name is None:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR, message="name cannot be cleared"
        )

    # Every field defaults to None, which the definer fn reads as "leave unchanged", so an
    # unset one passes straight through; `provided` is needed only for the two nullable
    # columns, to tell an explicit null ("clear it") from an omission.
    matched = await update_tenant_row(
        session,
        tenant_id=tenant_id,
        name=body.name,
        region=body.region,
        clear_region="region" in provided and body.region is None,
        observer_enabled=body.observer_enabled,
        auto_retry_enabled=body.auto_retry_enabled,
        retry_fill_threshold=body.retry_fill_threshold,
        max_agents_per_va=body.max_agents_per_va,
        max_concurrent_calls=body.max_concurrent_calls,
        max_retries=body.max_retries,
        queue_expiry_hours=body.queue_expiry_hours,
        recording_retention_days=body.recording_retention_days,
        clear_recording_retention_days=(
            "recording_retention_days" in provided and body.recording_retention_days is None
        ),
    )
    if not matched:
        raise NotFoundError(message="no such tenant")

    tenant = await _load_tenant(session, tenant_id)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        # Field NAMES only — a changed value (e.g. a new retry threshold) is operator
        # config, not PHI, but the audit log records what changed, never the values.
        meta={"target_tenant": str(tenant_id), "fields": sorted(provided)},
    )
    return ok(_to_detail(tenant))


_STATUS_EVENTS: dict[TenantStatus, AuthEvent] = {
    "deactivated": AuthEvent.TENANT_DEACTIVATED,
    "active": AuthEvent.TENANT_REACTIVATED,
}


async def _flip_tenant_status(
    tenant_id: UUID,
    request: Request,
    session: AsyncSession,
    audit: AuthAudit,
    caller: VerifiedIdentity,
    *,
    target_status: TenantStatus,
) -> ResponseModel[TenantDetail]:
    previous = await set_tenant_status(session, tenant_id=tenant_id, target_status=target_status)
    if previous is None:
        raise NotFoundError(message="no such tenant")
    if previous == target_status:
        raise ConflictError(message=f"tenant is already {target_status}")

    tenant = await _load_tenant(session, tenant_id)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=_STATUS_EVENTS[target_status],
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_tenant": str(tenant_id)},
    )
    return ok(_to_detail(tenant))


@router.post(
    "/tenants/{tenant_id}/deactivate",
    response_model=ResponseModel[TenantDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def deactivate_tenant(
    tenant_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantDetail]:
    """Blocks NEW logins for this tenant's users — the resolver
    (`resolve_tenant_by_slug`, migration e529f5cac06d) stops matching a deactivated
    tenant's slug, so login returns the uniform 401 with no hint the tenant exists.
    Sessions already open at deactivation time run until they expire; this does not
    revoke them. 409 if the tenant is already deactivated. Writes through the
    platform_set_tenant_status SECURITY DEFINER fn, audited null-tenant like every
    other /platform authz."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    return await _flip_tenant_status(
        tenant_id, request, session, audit, caller, target_status="deactivated"
    )


@router.post(
    "/tenants/{tenant_id}/reactivate",
    response_model=ResponseModel[TenantDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def reactivate_tenant(
    tenant_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantDetail]:
    """Restores login for this tenant's users. 409 if the tenant is already active."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    return await _flip_tenant_status(
        tenant_id, request, session, audit, caller, target_status="active"
    )


class TenantUser(BaseModel):
    id: UUID
    email: str
    name: str
    status: str
    roles: list[str]


@router.get(
    "/tenants/{tenant_id}/users",
    response_model=ResponseModel[list[TenantUser]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_tenant_users(
    tenant_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[list[TenantUser]]:
    """A tenant's users, for the Tenants admin screen. Requires no elevation grant:
    reads through the platform_list_tenant_users SECURITY DEFINER fn (VR2-30) — the
    same "administration, not a PHI read" posture as the invite endpoint below."""
    await _load_tenant(session, tenant_id)
    rows = await list_tenant_users_rows(session, tenant_id=tenant_id)
    return ok(
        [
            TenantUser(id=r.id, email=r.email, name=r.name, status=r.status, roles=r.roles)
            for r in rows
        ]
    )


class TenantRole(BaseModel):
    id: UUID
    name: str
    is_system: bool


@router.get(
    "/tenants/{tenant_id}/roles",
    response_model=ResponseModel[list[TenantRole]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_tenant_roles(
    tenant_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[list[TenantRole]]:
    """Roles assignable when inviting a user into this tenant: GLOBAL (tenant_id IS
    NULL) system roles only, excluding platform-tier ones (SUPER_ADMIN). A tenant's
    own custom roles are unreachable from the platform plane (role RLS requires
    app.tenant_id, which a platform session never has) and are out of scope here —
    the tenant's own Users screen is still where those get assigned."""
    await _load_tenant(session, tenant_id)
    role_rows = (
        await session.execute(
            select(Role.id, Role.name).where(Role.tenant_id.is_(None)).order_by(Role.name)
        )
    ).all()
    platform_tier = await platform_tier_role_ids(session, [r.id for r in role_rows])
    return ok(
        [
            TenantRole(id=r.id, name=r.name, is_system=True)
            for r in role_rows
            if r.id not in platform_tier
        ]
    )


async def _reject_unassignable_roles(session: AsyncSession, role_ids: list[UUID]) -> None:
    """Allow only the roles `list_tenant_roles` offers: GLOBAL (tenant_id IS NULL) and
    non-platform-tier. The unknown-id check is what makes an unknown role a clean 404 —
    without it the definer fn's FK would surface as an unhandled 500."""
    if not role_ids:
        return
    visible = set(
        (
            await session.execute(
                select(Role.id).where(Role.id.in_(role_ids), Role.tenant_id.is_(None))
            )
        ).scalars()
    )
    if visible != set(role_ids):
        raise NotFoundError(message="unknown role id")
    if await roles_grant_platform_permission(session, role_ids):
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN,
            message="cannot grant a platform-privileged role",
        )


@router.post(
    "/tenants/{tenant_id}/users/invitations",
    response_model=ResponseModel[InviteUserResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def invite_tenant_user(
    tenant_id: UUID,
    body: InviteUserRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[InviteUserResponse]:
    """Invite a user INTO a chosen tenant from the platform plane — no elevation
    grant needed (VR2-30 Option B: administration, not a PHI read). Creating a
    tenant never auto-invites anyone; this is the separate step that actually lets
    someone sign in. Reuses the same invite token / accept-invite flow as the
    tenant-admin `invite_user` — acceptance doesn't care who issued the invite."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    tenant = await _load_tenant(session, tenant_id)
    role_ids = body.role_ids
    await _reject_unassignable_roles(session, role_ids)

    user_id = uuid7()
    grant_ids = [uuid7() for _ in role_ids]
    outcome = await invite_tenant_user_row(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        email=body.email,
        name=body.name,
        invited_by=caller.user_id,
        role_ids=role_ids,
        grant_ids=grant_ids,
    )
    if outcome == "no_tenant":
        raise NotFoundError(message="no such tenant")
    if outcome == "duplicate":
        raise ConflictError(message="a user with that email already exists in this tenant")

    token = await invites.put(
        INVITE_NS,
        InviteData(tenant_id=tenant_id, app_user_id=user_id, email=body.email),
        settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.frontend_base_url}/tenants/{tenant.slug}/accept-invite?token={token}"

    email_sent = False
    if body.send_email:
        email_sent = await send_invite_email(
            email_sender,
            logger,
            to=body.email,
            name=body.name,
            invite_url=invite_url,
            ttl_seconds=settings.invite_ttl_seconds,
        )

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_USER_INVITED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={
            "target_tenant": str(tenant_id),
            "target_user": str(user_id),
            "delivery": "email" if email_sent else "link",
        },
    )
    return ok(
        InviteUserResponse(
            user_id=user_id, email=body.email, invite_url=invite_url, email_sent=email_sent
        )
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
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
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
    # Read the value BACK rather than echoing the request: the definer fn only reports whether
    # a row matched, so echoing would quietly lie if the stored value ever diverged (a trigger
    # normalising it, a concurrent flip). Readable via the tenant_platform_read RLS policy.
    stored = bool(
        await session.scalar(select(Tenant.observer_enabled).where(Tenant.id == tenant_id))
    )
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_OBSERVER_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_tenant": str(tenant_id), "observer_enabled": stored},
    )
    return ok(TenantObserverResponse(tenant_id=tenant_id, observer_enabled=stored))


class SetTenantRetryConfigRequest(BaseModel):
    auto_retry_enabled: bool | None = None
    retry_fill_threshold: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "SetTenantRetryConfigRequest":
        if self.auto_retry_enabled is None and self.retry_fill_threshold is None:
            raise ValueError("provide auto_retry_enabled and/or retry_fill_threshold")
        return self


class TenantRetryConfigResponse(BaseModel):
    tenant_id: UUID
    auto_retry_enabled: bool
    retry_fill_threshold: float


@router.post(
    "/tenants/{tenant_id}/retry-config",
    response_model=ResponseModel[TenantRetryConfigResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def set_tenant_retry_config_endpoint(
    tenant_id: UUID,
    body: SetTenantRetryConfigRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require(TENANTS_MANAGE)],
) -> ResponseModel[TenantRetryConfigResponse]:
    """Set a tenant's auto-retry flag and/or fill threshold. Writes through the
    platform_set_tenant_retry_config SECURITY DEFINER fn (the tenant table's
    platform RLS policy is SELECT-only), audited null-tenant like every other
    /platform authz."""
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    matched = await set_tenant_retry_config(
        session,
        tenant_id=tenant_id,
        enabled=body.auto_retry_enabled,
        threshold=body.retry_fill_threshold,
    )
    if not matched:
        raise NotFoundError(message="no such tenant")
    # Read the values BACK rather than echoing the request (same rationale as
    # set_tenant_observer: the fn only reports whether a row matched).
    row = (
        await session.execute(
            select(Tenant.auto_retry_enabled, Tenant.retry_fill_threshold).where(
                Tenant.id == tenant_id
            )
        )
    ).one()
    enabled = bool(row.auto_retry_enabled)
    threshold = float(row.retry_fill_threshold)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.TENANT_RETRY_CONFIG_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={
            "target_tenant": str(tenant_id),
            "auto_retry_enabled": enabled,
            "retry_fill_threshold": threshold,
        },
    )
    return ok(
        TenantRetryConfigResponse(
            tenant_id=tenant_id, auto_retry_enabled=enabled, retry_fill_threshold=threshold
        )
    )
