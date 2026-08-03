"""User administration (spec §4.1.1 / §4.1.3) — a TENANT_ADMIN invites, lists, and
deactivates tenant users. Invite acceptance (which sets the password and enrolls
MFA) is unauthenticated and lives in `api/v1/auth.py`.

Onboarding is invite-based: the create call always returns a copyable `invite_url`
and optionally emails it (sendria sandbox locally). Invitees are workforce members,
so this surface carries **no PHI**; the invite token is a single-use bearer
credential held only as a hash in Redis (auth/invitations.py) and is never logged.
Gated by `users:manage` (write) / `users:read` (list).
"""

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from control_plane.api.v1.common import (
    AppSettings,
    AuthAudit,
    Email,
    Invites,
    Resolver,
    TenantId,
    TenantSession,
    build_role_grant,
    roles_grant_platform_permission,
)
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import INVITE_NS, InviteData
from control_plane.auth.invite_reset import reset_and_reissue_invite
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
from vera_core.audit import emit_auth_event
from vera_core.models import AppUser, Role, UserRole
from vera_core.models.enums import AccountType, AuthEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
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
    roles: list[str]


def _to_response(row: AppUser, roles: list[str]) -> UserResponse:
    return UserResponse(
        id=row.id,
        email=row.email,
        name=row.name,
        status=row.status,
        last_login_at=row.last_login_at,
        roles=roles,
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
    email = body.email
    # Durable de-dup (no UNIQUE on email exists): a user with this email already in
    # the tenant is a conflict — also collapses a late retry into one account. Matched
    # case-insensitively like the login lookup, so `User@x` can't shadow `user@x`.
    existing = (
        await session.execute(select(AppUser.id).where(func.lower(AppUser.email) == email.lower()))
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
        invited_by=caller.user_id,
    )
    session.add(user)
    await session.flush()
    for role_id in body.role_ids:
        session.add(
            build_role_grant(
                tenant_id=tenant_id,
                app_user_id=user.id,
                role_id=role_id,
                granted_by=caller.user_id,
            )
        )

    token = await invites.put(
        INVITE_NS,
        InviteData(tenant_id=tenant_id, app_user_id=user.id, email=email),
        settings.invite_ttl_seconds,
    )
    invite_url = (
        f"{settings.frontend_base_url}/tenants/{caller.tenant_slug}/accept-invite?token={token}"
    )

    email_sent = False
    if body.send_email:
        try:
            await email_sender.send(
                EmailMessage(
                    to=email,
                    subject="You're invited to Vera Techsolutions",
                    body=(
                        f"Hello{(' ' + body.name) if body.name else ''},\n\n"
                        "You've been invited to join Vera Techsolutions, the platform your "
                        "team uses to run AI-assisted insurance benefit verification.\n\n"
                        "Click below to set your password and get started. This link is "
                        f"valid for {settings.invite_ttl_seconds // 3600} hours.\n\n"
                        f"{invite_url}\n\n"
                        "If you weren't expecting this, you can safely ignore this email."
                    ),
                    action_url=invite_url,
                    action_label="Set your password",
                )
            )
            email_sent = True
        except Exception:
            logger.warning("invitation email to %s could not be sent", email, exc_info=True)

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.USER_INVITED,
        ip=client_ip(request),
        user_id=caller.user_id,
        # Never the token — only who/how, for the trail.
        meta={"target_user": str(user.id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteUserResponse(
            user_id=user.id, email=body.email, invite_url=invite_url, email_sent=email_sent
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
    role_rows = (
        await session.execute(
            select(UserRole.app_user_id, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .order_by(Role.name)
        )
    ).all()
    roles_by_user: dict[UUID, list[str]] = {}
    for user_id, role_name in role_rows:
        roles_by_user.setdefault(user_id, []).append(role_name)
    return ok([_to_response(r, roles_by_user.get(r.id, [])) for r in rows])


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

    # PermissionResolver.effective_permissions checks the cache BEFORE the
    # active-status query — a cache hit skips the DB (and the active check)
    # entirely. This invalidate() is therefore the only thing that closes the
    # window promptly; without it, a deactivated user stays authorized for up
    # to the cache TTL (see the docstring on effective_permissions,
    # control_plane/auth/rbac.py). Any future path that flips AppUser.status
    # away from "active" must call this too.
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


@router.post(
    "/users/{user_id}/resend-invitation",
    response_model=ResponseModel[InviteUserResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def resend_invitation(
    user_id: UUID,
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
    """Reissue a fresh invite link for a user stuck in status="invited" (their
    original link or MFA bridge token expired before they finished onboarding).
    Deletes any stale password UserIdentity and mints a new INVITE_NS token."""
    if caller.tenant_slug is None:
        raise UnauthorizedError(message="malformed session: tenant slug missing")
    await claim_or_conflict(
        get_idempotency_store(request),
        tenant_id,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such user in this tenant")
    if user.status != "invited":
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="user is not in invited status"
        )

    token = await reset_and_reissue_invite(
        session,
        invites,
        namespace=INVITE_NS,
        app_user=user,
        ttl_seconds=settings.invite_ttl_seconds,
    )
    invite_url = (
        f"{settings.frontend_base_url}/tenants/{caller.tenant_slug}/accept-invite?token={token}"
    )

    email_sent = False
    try:
        await email_sender.send(
            EmailMessage(
                to=user.email,
                subject="You're invited to Vera Techsolutions",
                body=(
                    f"Hello{(' ' + user.name) if user.name else ''},\n\n"
                    "Your invitation has been refreshed so you can finish setting up your "
                    "Vera Techsolutions account.\n\n"
                    "Click below to set your password. This link is valid for "
                    f"{settings.invite_ttl_seconds // 3600} hours.\n\n"
                    f"{invite_url}\n\n"
                    "If you weren't expecting this, you can safely ignore this email."
                ),
                action_url=invite_url,
                action_label="Set your password",
            )
        )
        email_sent = True
    except Exception:
        logger.warning("resend invitation email to %s could not be sent", user.email, exc_info=True)

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.INVITE_RESENT,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user.id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteUserResponse(
            user_id=user.id, email=user.email, invite_url=invite_url, email_sent=email_sent
        )
    )
