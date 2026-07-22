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

from control_plane.api.v1.common import AppSettings, AuthAudit, Email, Invites
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import PLATFORM_INVITE_NS, InviteData
from control_plane.auth.platform_provisioning import create_operator_invite
from control_plane.auth.rbac import platform_require
from control_plane.deps import client_ip, get_idempotency_store, platform_scoped_session
from control_plane.email import EmailMessage
from control_plane.exceptions import CustomAPIException, CustomAPIResponse, DefaultExceptionCode
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
        try:
            await email_sender.send(
                EmailMessage(
                    to=email,
                    subject="You're invited to Vera as a platform operator",
                    body=(
                        f"Hello{(' ' + body.name) if body.name else ''},\n\n"
                        "You've been invited as a Vera platform operator. Set your "
                        "password using the link below "
                        f"(valid for {settings.invite_ttl_seconds // 3600} hours). "
                        "Two-factor authentication is required to finish setup.\n\n"
                        f"{invite_url}\n\n"
                        "If you didn't expect this, you can ignore this email."
                    ),
                )
            )
            email_sent = True
        except Exception:
            logger.warning(
                "platform invitation email to %s could not be sent", email, exc_info=True
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
