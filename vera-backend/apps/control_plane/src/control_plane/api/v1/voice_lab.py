"""Voice Lab — a standalone developer/QA harness to start a voice session with
the Vera agent and listen to it live in the browser.

No persistence: this creates an ephemeral LiveKit room, dispatches the agent, and
(for outbound) places a SIP call — but writes no Call/PatientForm rows and never
appears in Live Monitoring. It is deliberately decoupled from /calls so it can be
built and tested in parallel with the real call-initiation flow.

Auth note (acknowledged stopgap): guards with `require("calls:read")`, matching the
interim convention in `calls.py`.
"""

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import Kms, LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver, require
from control_plane.deps import current_identity, get_audit, get_sessionmaker, get_transcript_service
from control_plane.exceptions import (
    ConflictError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.ivr_selection import add_active_playbook_metadata
from control_plane.livekit_gateway import OutboundDialError
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord, AuditSink
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.integrations.credentials import get_integration_credentials
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.observability.correlation import (
    CALLER_IDENTITY_PREFIX,
    MONITOR_IDENTITY_PREFIX,
    parse_room_name,
    room_name_for_call,
)
from vera_core.schemas import StartVoiceSessionRequest, VoiceSessionResponse
from vera_core.transcript import TranscriptEvent, TranscriptService

router = APIRouter(tags=["voice-lab"])

# E.164: a leading + and 1-15 digits, first digit non-zero.
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


@router.post(
    "/voice-lab/sessions",
    response_model=ResponseModel[VoiceSessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.BAD_GATEWAY,
    ),
)
async def start_voice_session(
    body: StartVoiceSessionRequest,
    tenant_id: TenantId,
    livekit: LiveKit,
    session: TenantSession,
    kms: Kms,
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[VoiceSessionResponse]:
    # Synthetic call id — no DB row; the room name is still the canonical
    # call--<tenant>--<call> so worker correlation/observability work unchanged.
    room_name = room_name_for_call(tenant_id, uuid7())

    # The browser always joins to hear the conversation. In browser mode it also
    # publishes the mic (caller-); in outbound mode it is listen-only (monitor-),
    # which the worker's wait_for_speaker skips when deciding the room is ready.
    is_outbound = body.mode == "outbound"
    # Resolved (phone_number, trunk_id) for an outbound call; None in browser mode.
    # Check the cheap E.164 precondition before the DB + KMS credential lookup, and carry
    # both values as a typed pair so the dial site needs no None-narrowing asserts.
    outbound: tuple[str, str] | None = None
    if is_outbound:
        if body.phone_number is None or not _E164.match(body.phone_number):
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="phone_number must be E.164 for an outbound call",
            )
        creds = await get_integration_credentials(
            session, kms, integration_type_name="livekit_outbound_trunk_id"
        )
        trunk_id = creds.get("trunk_id") if creds else None
        if not trunk_id:
            raise ConflictError(message="outbound SIP is not configured")
        outbound = (body.phone_number, trunk_id)

    prefix = MONITOR_IDENTITY_PREFIX if is_outbound else CALLER_IDENTITY_PREFIX
    browser_identity = f"{prefix}{caller.user_id}"

    metadata: dict[str, Any] = {
        "wait_for_speaker": True,
        "publish_transcript": True,
        "ivr_navigation": body.ivr_navigation,
    }
    # When navigating, specialize the navigator with the provider's active playbook if one exists;
    # otherwise the worker falls back to the generic navigator (no ivr_playbook key).
    if body.ivr_navigation:
        await add_active_playbook_metadata(session, body.insurance_provider_id, metadata)
    await livekit.create_call_room(room_name, metadata=metadata)
    if outbound is not None:
        phone_number, trunk_id = outbound
        try:
            await livekit.create_sip_participant(room_name, phone_number, trunk_id)
        except OutboundDialError as e:
            # The dial failed at the LiveKit/telephony seam (e.g. the trunk was deleted
            # after it was stored, or the carrier refused the call). Tear down the room
            # + dispatched agent we just created so nothing is left orphaned, and return
            # a clean upstream error instead of letting it surface as a raw 500.
            await livekit.delete_room(room_name)
            raise CustomAPIException(
                DefaultExceptionCode.BAD_GATEWAY,
                message="could not place the outbound call — the telephony provider rejected it",
            ) from e

    token = livekit.mint_join_token(room_name=room_name, identity=browser_identity)
    return ok(
        VoiceSessionResponse(
            room_name=room_name,
            url=livekit.url,
            token=token,
            mode=body.mode,
        )
    )


@router.delete(
    "/voice-lab/sessions/{room_name}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def end_voice_session(
    room_name: str,
    tenant_id: TenantId,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[None]:
    # Deleting the room is what actually ends the session: it disconnects the agent
    # worker (its session shuts down) and any SIP callee (the outbound call hangs up).
    # The browser leaving on its own does none of that. Tenant-scope by the room name
    # (which embeds the tenant uuid) so one tenant can't tear down another's room.
    ref = parse_room_name(room_name)
    if ref is None or ref.tenant_id != tenant_id:
        raise NotFoundError(message="voice session not found")
    await livekit.delete_room(room_name)
    return ok(None, message="Voice session ended.")


def _sse_frame(entry_id: str, event: TranscriptEvent) -> str:
    return f"id: {entry_id}\ndata: {event.model_dump_json()}\n\n"


@router.get("/voice-lab/sessions/{room_name}/transcript")
async def stream_transcript(
    room_name: str,
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Annotated[AuditSink, Depends(get_audit)],
    service: Annotated[TranscriptService, Depends(get_transcript_service)],
) -> StreamingResponse:
    # Tenant scope without a DB hit: only tenant users; the room name embeds the tenant
    # uuid and must match the caller's (cross-tenant guard, like end_voice_session).
    # These structural 404s (non-tenant account, unparseable or cross-tenant room) are
    # intentionally NOT audited: there is no caller-owned tenant scope to attribute the
    # probe to, and RLS already hides existence — matching require()/end_voice_session,
    # which audit the authz allow/deny on a valid tenant-scoped request (done below),
    # not the not-found/cross-tenant rejections.
    ref = parse_room_name(room_name)
    if identity.account_type != "tenant" or identity.tenant_id is None or ref is None:
        raise NotFoundError(message="voice session not found")
    tenant_id = identity.tenant_id
    if ref.tenant_id != tenant_id:
        raise NotFoundError(message="voice session not found")

    # Authorize in a SHORT-LIVED tenant session, then release it before streaming — an
    # SSE response is long-lived; we must not hold a DB connection for its duration.
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
    allowed = "calls:read" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="transcript",
            resource_id=room_name,
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )

    async def _events() -> AsyncIterator[str]:
        async for entry_id, event in service.consume(room_name):
            yield _sse_frame(entry_id, event)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
