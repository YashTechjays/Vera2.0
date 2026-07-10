"""Verification-call endpoints: join-token, active-list, live event stream,
publish, and revoke-access.

Auth note (acknowledged stopgap): join-token / active-list guard with
`require("calls:read")` for now — the SPA has no real auth yet, and the
spec flags this. `publish` and `revoke-access` are owner-only actions
gated on `require("calls:publish")`: the caller must hold the permission
*and* be the call's `initiated_by_id`, enforced by an explicit 403 check
in each handler.
"""

import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import Audit, LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver, require
from control_plane.deps import (
    current_identity,
    get_call_stream_service,
    get_sessionmaker,
)
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.models import Call, PatientForm
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import CallSummary, JoinTokenResponse, RevokeAccessRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calls"])

_ACTIVE_STATUSES = (
    CallStatus.INITIATED,
    CallStatus.RINGING,
    CallStatus.IVR,
    CallStatus.ACTIVE,
    CallStatus.WAITING,
    CallStatus.CRITICAL,
)


def _supervisor_identity(user_id: UUID) -> str:
    """LiveKit participant identity for a VA joining/intervening on a call."""
    return f"supervisor-{user_id}"


def _call_hidden_from(call: Call, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see this call (→ the same 404 as a missing row,
    so a private call is never revealed by enumeration).

    The owner always sees their own call. A non-owner sees it only when it is
    published or ownerless (dispatcher-created, joinable tenant-wide) AND they
    have not been revoked; a revoked user gets the same 404 as a private call.
    Shared by join-token and the event stream so the two visibility gates can
    never diverge.
    """
    if call.initiated_by_id == user_id:
        return False
    if str(user_id) in call.revoked_user_ids:
        return True
    return call.initiated_by_id is not None and not call.published


def _summary(call: Call, patient_name: str | None, caller_id: UUID) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name,
        started_at=call.started_at,
        created_at=call.created_at,
        published=call.published,
        is_owner=call.initiated_by_id == caller_id,
    )


@router.get(
    "/calls/{call_id}/join-token",
    response_model=ResponseModel[JoinTokenResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def join_token(
    call_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    intervene: bool = False,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[JoinTokenResponse]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if _call_hidden_from(call, caller.user_id):
        raise NotFoundError(message="call not found")  # don't reveal a private call
    if call.initiated_by_id != caller.user_id:  # non-owner joining another's call
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_INTERVENE_JOIN.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:read",
                decision="allow",
                request_id=current_request_id(request),
                detail={"owner_id": str(call.initiated_by_id) if call.initiated_by_id else None},
            )
        )
    room_name = room_name_for_call(tenant_id, call.id)
    identity = _supervisor_identity(caller.user_id)
    # Watch-only tokens are server-side mute; only ?intervene=true may publish.
    token = livekit.mint_join_token(room_name=room_name, identity=identity, can_publish=intervene)
    return ok(JoinTokenResponse(token=token, url=livekit.url, room_name=room_name))


@router.get("/calls/{call_id}/events")
async def stream_call_events(
    call_id: UUID,
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    service: Annotated[CallStreamService, Depends(get_call_stream_service)],
) -> StreamingResponse:
    """Live per-call event stream (transcript turns, call_status frames; form-fill
    later) for Live Monitoring. Same visibility rule as join-token: owner, or a
    published/ownerless call, minus revoked users. Authorization runs in a
    SHORT-LIVED tenant session released before streaming (an SSE is long-lived and
    must not pin a DB connection — mirrors voice_lab.stream_transcript)."""
    if identity.account_type != "tenant" or identity.tenant_id is None:
        raise NotFoundError(message="call not found")
    tenant_id = identity.tenant_id
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
        call = (
            await session.execute(select(Call).where(Call.id == call_id))
        ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if _call_hidden_from(call, user_id):
        raise NotFoundError(message="call not found")  # don't reveal a private call
    allowed = "calls:read" in permissions
    # Transcript text is tokenized/de-identified, but the disclosure is still audited
    # (mirrors the voice-lab transcript endpoint).
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call_events",
            resource_id=str(call_id),
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
    room_name = room_name_for_call(tenant_id, call.id)

    async def _events() -> AsyncIterator[str]:
        async for entry_id, event in service.consume(room_name):
            yield f"id: {entry_id}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/calls/{call_id}/publish",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def publish_call(
    call_id: UUID,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[CallSummary]:
    response.headers["Cache-Control"] = "no-store"
    # RLS scopes to the caller's tenant; the row lock serializes concurrent publishes.
    call = (
        await session.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can publish"
        )
    if not call.published:  # idempotent, one-way — no un-publish
        call.published = True
        call.published_at = func.now()
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_PUBLISH.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:publish",
                decision="allow",
                request_id=current_request_id(request),
            )
        )
    # Same row shape as list_calls — a None patient_name blanks the UI's
    # Patient cell until the next poll.
    patient_name = (
        await session.execute(
            select(PatientForm.patient_name).where(PatientForm.id == call.form_id)
        )
    ).scalar_one_or_none()
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call",
            resource_id=str(call.id),
            request_id=current_request_id(request),
            detail={"fields": ["patient_name"]},
        )
    )
    return ok(_summary(call, patient_name, caller.user_id))


@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_calls(
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_(list(_ACTIVE_STATUSES)))
            # Ownerless (pre-ownership dispatcher) calls are tenant-visible —
            # hidden, they'd have no monitoring and no owner to ever publish them.
            .where(
                or_(
                    Call.initiated_by_id == caller.user_id,
                    Call.published.is_(True),
                    Call.initiated_by_id.is_(None),
                )
            )
            .order_by(Call.created_at.desc())
        )
    ).all()
    # PHI disclosure (patient_name) — audit field names, mirroring list_patient_forms.
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call",
            resource_id="list",
            request_id=current_request_id(request),
            detail={"fields": ["patient_name"]},
        )
    )
    return ok([_summary(c, name, caller.user_id) for c, name in rows])


@router.post(
    "/calls/{call_id}/revoke-access",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def revoke_access(
    call_id: UUID,
    body: RevokeAccessRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[None]:
    # Row lock: concurrent revokes must not overwrite each other's list append.
    call = (
        await session.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can revoke access"
        )
    target = str(body.target_user_id)
    if target not in call.revoked_user_ids:
        call.revoked_user_ids = [*call.revoked_user_ids, target]
    room_name = room_name_for_call(tenant_id, call.id)
    await livekit.remove_participant(room_name, _supervisor_identity(body.target_user_id))
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.CALL_INTERVENE_REVOKE.value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:publish",
            decision="allow",
            request_id=current_request_id(request),
            detail={"target_user_id": str(body.target_user_id)},
        )
    )
    return ok(None, message="Access revoked.")
