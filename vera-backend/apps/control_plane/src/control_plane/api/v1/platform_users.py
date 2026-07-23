"""Platform-operator administration — an existing SUPER_ADMIN invites, lists,
deactivates, and resends invitations to platform operators. Mirrors
api/v1/users.py, but for account_type='platform' (tenant_id=NULL) accounts.
Invite acceptance lives in api/v1/platform_auth.py (pre-auth, no tenant slug).
Gated by `platform:users:invite` (write) / `platform:users:read` (list). Carries
no PHI — platform operators are workforce, not patients."""

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings, AuthAudit, Email, Invites, Resolver
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import PLATFORM_INVITE_NS, InviteData
from control_plane.auth.invite_reset import reset_and_reissue_invite
from control_plane.auth.platform_provisioning import create_operator_invite, set_operator_status
from control_plane.auth.rbac import platform_require
from control_plane.deps import client_ip, get_idempotency_store, platform_scoped_session
from control_plane.email import EmailMessage, EmailSender
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
from vera_core.models import AppUser
from vera_core.models.enums import AuthEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform", tags=["platform-users"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]


class InviteOperatorRequest(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=255)
    send_email: bool = True


class InviteOperatorResponse(BaseModel):
    user_id: UUID
    email: str
    invite_url: str
    email_sent: bool


class OperatorResponse(BaseModel):
    id: UUID
    email: str
    name: str
    status: str
    last_login_at: datetime | None


async def _send_operator_invite_email(
    email_sender: EmailSender,
    *,
    to: str,
    name: str,
    intro: str,
    invite_url: str,
    ttl_seconds: int,
    log_context: str,
) -> bool:
    """Send a platform-operator invite email, returning whether it was sent. Never
    raises: on delivery failure the caller falls back to the copyable invite link.
    `intro` is the tier-specific opening sentence (fresh invite vs. resend)."""
    try:
        await email_sender.send(
            EmailMessage(
                to=to,
                subject="You're invited to Vera as a platform operator",
                body=(
                    f"Hello{(' ' + name) if name else ''},\n\n"
                    f"{intro} "
                    f"(valid for {ttl_seconds // 3600} hours). "
                    "Two-factor authentication is required to finish setup.\n\n"
                    f"{invite_url}\n\n"
                    "If you didn't expect this, you can ignore this email."
                ),
            )
        )
        return True
    except Exception:
        logger.warning("%s to %s could not be sent", log_context, to, exc_info=True)
        return False


def _to_response(row: AppUser) -> OperatorResponse:
    return OperatorResponse(
        id=row.id,
        email=row.email,
        name=row.name,
        status=row.status,
        last_login_at=row.last_login_at,
    )


@router.post(
    "/users/invitations",
    response_model=ResponseModel[InviteOperatorResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def invite_operator(
    body: InviteOperatorRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[InviteOperatorResponse]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    email = body.email
    existing = (
        await session.execute(select(AppUser.id).where(AppUser.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="a platform operator with that email already exists",
        )

    user_id = await create_operator_invite(
        session, email=email, name=body.name, invited_by=caller.user_id
    )

    token = await invites.put(
        PLATFORM_INVITE_NS,
        InviteData(tenant_id=None, app_user_id=user_id, email=email),
        settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.frontend_base_url}/platform/accept-invite?token={token}"

    email_sent = False
    if body.send_email:
        email_sent = await _send_operator_invite_email(
            email_sender,
            to=email,
            name=body.name,
            intro=(
                "You've been invited as a Vera platform operator. Set your "
                "password using the link below"
            ),
            invite_url=invite_url,
            ttl_seconds=settings.invite_ttl_seconds,
            log_context="platform invitation email",
        )

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_INVITED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user_id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteOperatorResponse(
            user_id=user_id, email=email, invite_url=invite_url, email_sent=email_sent
        )
    )


@router.get(
    "/users",
    response_model=ResponseModel[list[OperatorResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_operators(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, platform_require("platform:users:read")],
) -> ResponseModel[list[OperatorResponse]]:
    rows = (await session.execute(select(AppUser).order_by(AppUser.email))).scalars().all()
    return ok([_to_response(r) for r in rows])


@router.post(
    "/users/{user_id}/deactivate",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def deactivate_operator(
    user_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    resolver: Resolver,
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[None]:
    # The last-active-operator lockout guard is atomic inside `platform_set_operator_status`
    # itself (locks the active set before counting it — see migration 9cec58e69e92); a
    # Python-side count-then-check here would be a TOCTOU race (two concurrent deactivates
    # against two different active operators could each read the pre-write count and both
    # commit, leaving zero active operators — unrecoverable, no bootstrap path once any
    # platform operator has ever existed). `flipped` is `None` when the guard blocked the
    # write, `False` when `user_id` doesn't match a platform operator, `True` on success.
    flipped = await set_operator_status(session, app_user_id=user_id, status="deactivated")
    if flipped is None:
        raise ConflictError(message="cannot deactivate the last active platform operator")
    if not flipped:
        raise NotFoundError(message="no such platform operator")

    # PermissionResolver.effective_permissions checks the cache BEFORE the
    # active-status query — a cache hit skips the DB (and the active check)
    # entirely. This invalidate() is therefore the only thing that closes the
    # window promptly; without it, a deactivated operator stays authorized for
    # up to the cache TTL (see the docstring on effective_permissions,
    # control_plane/auth/rbac.py). The cache keys on tenant_id directly, and
    # None is the platform scope — mirrors deactivate_user in api/v1/users.py.
    await resolver.invalidate(None, user_id)

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_DEACTIVATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user_id)},
    )
    return ok(None, message="Platform operator deactivated.")


@router.post(
    "/users/{user_id}/resend-invitation",
    response_model=ResponseModel[InviteOperatorResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def resend_operator_invitation(
    user_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    settings: AppSettings,
    invites: Invites,
    email_sender: Email,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, platform_require("platform:users:invite")],
) -> ResponseModel[InviteOperatorResponse]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="no such platform operator")
    if user.status != "invited":
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="operator is not in invited status"
        )

    token = await reset_and_reissue_invite(
        session,
        invites,
        namespace=PLATFORM_INVITE_NS,
        app_user=user,
        ttl_seconds=settings.invite_ttl_seconds,
    )
    invite_url = f"{settings.frontend_base_url}/platform/accept-invite?token={token}"

    email_sent = await _send_operator_invite_email(
        email_sender,
        to=user.email,
        name=user.name,
        intro="Here is a fresh link to set your password",
        invite_url=invite_url,
        ttl_seconds=settings.invite_ttl_seconds,
        log_context="resend platform invitation email",
    )

    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_INVITE_RESENT,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"target_user": str(user.id), "delivery": "email" if email_sent else "link"},
    )
    return ok(
        InviteOperatorResponse(
            user_id=user.id, email=user.email, invite_url=invite_url, email_sent=email_sent
        )
    )
