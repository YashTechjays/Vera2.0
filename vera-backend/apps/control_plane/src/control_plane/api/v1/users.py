"""User administration (spec §4.1.1 / §4.1.3) — a TENANT_ADMIN invites, lists, and
deactivates tenant users. Invite acceptance (which sets the password and enrolls
MFA) is unauthenticated and lives in `api/v1/auth.py`.

Onboarding is invite-based: the create call always returns a copyable `invite_url`
and optionally emails it (sendria sandbox locally). Invitees are workforce members,
so this surface carries **no PHI**; the invite token is a single-use bearer
credential held only as a hash in Redis (auth/invitations.py) and is never logged.
Gated by `users:manage` (write) / `users:read` (list).
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
    Email,
    Invites,
    Resolver,
    TenantId,
    TenantSession,
    emit_auth_event,
    roles_grant_platform_permission,
)
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import INVITE_NS, InviteData
from control_plane.auth.rbac import require
from control_plane.deps import client_ip, get_idempotency_store
from control_plane.email import EmailMessage
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
    UnauthorizedError,
)
from control_plane.idempotency import claim_or_conflict, require_idempotency_key
from control_plane.responses import ResponseModel, ok
from vera_core.models import AppUser, Role, UserRole
from vera_core.models.enums import AccountType, AuthEvent

router = APIRouter(tags=["users"])


class InviteUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=255)
    role_ids: list[UUID] = Field(default_factory=list)
    send_email: bool = True


class InviteUserResponse(BaseModel):
    user_id: UUID
    email: str
    invite_url: str
    email_sent: bool


class UserResponse(BaseModel):
    id: UUID
    email: str
    name: str
    status: str
    last_login_at: datetime | None


def _to_response(row: AppUser) -> UserResponse:
    return UserResponse(
        id=row.id,
        email=row.email,
        name=row.name,
        status=row.status,
        last_login_at=row.last_login_at,
    )


@router.post(
    "/users/invitations",
    response_model=ResponseModel[InviteUserResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def invite_user(
    body: InviteUserRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: VerifiedIdentity = require("users:manage"),
) -> ResponseModel[InviteUserResponse]:
    # Fail fast: slug is required to build the invite URL. A missing slug means the
    # session was minted incorrectly — treat it as a malformed-session 401 before
    # touching the DB or Redis.
    if caller.tenant_slug is None:
        raise UnauthorizedError(message="malformed session: tenant slug missing")
    await claim_or_conflict(
        get_idempotency_store(request),
        tenant_id,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    email = str(body.email)
    # Durable de-dup (no UNIQUE on email exists): a user with this email already in
    # the tenant is a conflict — also collapses a late retry into one account.
    existing = (
        await session.execute(select(AppUser.id).where(AppUser.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="a user with that email already exists"
        )

    # Validate any requested roles are visible in this tenant before creating.
    if body.role_ids:
        visible = (
            (await session.execute(select(Role.id).where(Role.id.in_(body.role_ids))))
            .scalars()
            .all()
        )
        if len(set(visible)) != len(set(body.role_ids)):
            raise NotFoundError(message="unknown role id")
        # Same guard as assign_role: an invite must not grant a platform-tier role
        # (e.g. the globally-visible SUPER_ADMIN) — this seam bypasses assign_role.
        if await roles_grant_platform_permission(session, body.role_ids):
            raise CustomAPIException(
                DefaultExceptionCode.FORBIDDEN,
                message="cannot grant a platform-privileged role",
            )

    user = AppUser(
        tenant_id=tenant_id,
        email=email,
        name=body.name,
        status="invited",
        account_type=AccountType.TENANT.value,
    )
    session.add(user)
    await session.flush()
    for role_id in body.role_ids:
        session.add(UserRole(tenant_id=tenant_id, app_user_id=user.id, role_id=role_id))

    token = await invites.put(
        INVITE_NS,
        InviteData(tenant_id=tenant_id, app_user_id=user.id, email=email),
        settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.app_base_url}/tenants/{caller.tenant_slug}/accept-invite?token={token}"

    if body.send_email:
        await email_sender.send(
            EmailMessage(
                to=email,
                subject="You're invited to Vera",
                body=(
                    f"Hello{(' ' + body.name) if body.name else ''},\n\n"
                    "You've been invited to Vera. Set your password using the link below "
                    f"(valid for {settings.invite_ttl_seconds // 3600} hours):\n\n"
                    f"{invite_url}\n\n"
                    "If you didn't expect this, you can ignore this email."
                ),
            )
        )

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.USER_INVITED,
        ip=client_ip(request),
        user_id=caller.user_id,
        # Never the token — only who/how, for the trail.
        meta={"target_user": str(user.id), "delivery": "email" if body.send_email else "link"},
    )
    return ok(
        InviteUserResponse(
            user_id=user.id, email=body.email, invite_url=invite_url, email_sent=body.send_email
        )
    )


@router.get(
    "/users",
    response_model=ResponseModel[list[UserResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_users(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("users:read"),
) -> ResponseModel[list[UserResponse]]:
    rows = (await session.execute(select(AppUser).order_by(AppUser.email))).scalars().all()
    return ok([_to_response(r) for r in rows])


@router.post(
    "/users/{user_id}/deactivate",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def deactivate_user(
    user_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    resolver: Resolver,
    _caller: VerifiedIdentity = require("users:manage"),
) -> ResponseModel[None]:
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such user in this tenant")
    user.status = "deactivated"

    # Effective permissions are resolved only for active users, but invalidate the
    # cache so any in-flight grant is dropped immediately.
    await resolver.invalidate(tenant_id, user_id)
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.USER_DEACTIVATED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"target_user": str(user_id)},
    )
    return ok(None, message="User deactivated.")
