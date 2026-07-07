"""Voice Lab — a standalone developer/QA harness to start a voice session with
the Vera agent and listen to it live in the browser.

No persistence: this creates an ephemeral LiveKit room, dispatches the agent, and
(for outbound) places a SIP call — but writes no Call/PatientForm rows and never
appears in Live Monitoring. It is deliberately decoupled from /calls so it can be
built and tested in parallel with the real call-initiation flow.

Auth note: guarded by the dedicated `voice_lab:sandbox` permission, kept separate
from `calls:read` (which gates the real call system) so a narrow role like
VIRTUAL_ASSISTANT can use this sandbox without seeing real call data.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
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
from control_plane.livekit_gateway import LiveKitGateway, OutboundDialError
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from control_plane.worker_events import classify_dial_failure, teardown_call_failed
from vera_core.audit import AuditRecord, AuditSink
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.integrations.credentials import get_integration_credentials
from vera_core.models import InsuranceProvider
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

logger = logging.getLogger("control_plane.voice_lab")


def _log_task_exception(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("voice-lab background task failed", exc_info=task.exception())


def _spawn_tracked(app: FastAPI, coro: Coroutine[Any, Any, None]) -> None:
    """Run a fire-and-forget coroutine tracked on app.state so it isn't GC'd mid-flight
    and is cancelled on shutdown; log (never swallow) any exception it raises."""
    tasks: set[asyncio.Task[None]] = app.state.background_tasks
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_task_exception)


async def _watch_outbound_dial(
    livekit: LiveKitGateway,
    room_name: str,
    phone_number: str,
    trunk_id: str,
    *,
    dial_timeout_s: float,
    teardown_grace_ms: int,
) -> None:
    """Place the outbound call and wait for its outcome. On a busy / declined / no-answer /
    bad-trunk failure, reflect the reason to the browser (room metadata) and tear the room
    down. Runs in the background so the HTTP response returns immediately, yet the dialer —
    not a late-joining worker — is the one that reliably observes the failure."""
    try:
        await livekit.create_sip_participant(
            room_name, phone_number, trunk_id, wait_until_answered=True, dial_timeout=dial_timeout_s
        )
        logger.info("voice-lab: outbound call answered for room %s", room_name)
    except OutboundDialError as e:
        reason = classify_dial_failure(e)
        logger.warning(
            "voice-lab: outbound call failed for room %s (%s): %s", room_name, reason.value, e
        )
        await teardown_call_failed(livekit, room_name, reason, teardown_grace_ms=teardown_grace_ms)


# E.164: a leading + and 1-15 digits, first digit non-zero.
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


class ProviderOption(BaseModel):
    """Minimal insurance-provider option for the call-start provider picker (non-PHI)."""

    id: UUID
    name: str


@router.get(
    "/voice-lab/insurance-providers",
    response_model=ResponseModel[list[ProviderOption]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_call_providers(
    session: TenantSession,
    _caller: VerifiedIdentity = require("voice_lab:sandbox"),
) -> ResponseModel[list[ProviderOption]]:
    """Active insurance providers a tenant operator can pick when starting an IVR call. The
    insurance_provider table is GLOBAL (no RLS), so it resolves on the tenant-scoped session;
    the provider's active playbook is then applied server-side at call start."""
    rows = (
        await session.execute(
            select(InsuranceProvider.id, InsuranceProvider.name)
            .where(InsuranceProvider.status == "active")
            .order_by(InsuranceProvider.name)
        )
    ).all()
    return ok([ProviderOption(id=row.id, name=row.name) for row in rows])


@router.post(
    "/voice-lab/sessions",
    response_model=ResponseModel[VoiceSessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def start_voice_session(
    body: StartVoiceSessionRequest,
    request: Request,
    tenant_id: TenantId,
    livekit: LiveKit,
    session: TenantSession,
    kms: Kms,
    caller: VerifiedIdentity = require("voice_lab:sandbox"),
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
        "enable_ivr_navigation": body.enable_ivr_navigation,
    }
    # When navigating, specialize the navigator with the provider's active playbook if one exists;
    # otherwise the worker falls back to the generic navigator (no ivr_playbook key).
    if body.enable_ivr_navigation:
        await add_active_playbook_metadata(session, body.insurance_provider_id, metadata)
    await livekit.create_call_room(room_name, metadata=metadata)
    logger.info(
        "voice-lab: created room + dispatched agent for room %s (outbound=%s)",
        room_name,
        outbound is not None,
    )
    if outbound is not None:
        phone_number, trunk_id = outbound
        settings = request.app.state.settings
        # Place + watch the call in the BACKGROUND: create_sip_participant(wait_until_answered)
        # blocks until answered or failed, so we must not hold the HTTP response on it. On
        # failure the watcher reflects the reason to the browser (room metadata) and deletes
        # the room. The dialer observing its own call closes the worker cold-start race (a
        # busy/decline that drops before a just-started worker subscribes would be missed).
        _spawn_tracked(
            request.app,
            _watch_outbound_dial(
                livekit,
                room_name,
                phone_number,
                trunk_id,
                dial_timeout_s=settings.outbound_dial_timeout_s,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
            ),
        )
        logger.info("voice-lab: watching outbound dial for room %s (background)", room_name)

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
    _caller: VerifiedIdentity = require("voice_lab:sandbox"),
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
    allowed = "voice_lab:sandbox" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="transcript",
            resource_id=room_name,
            permission_key="voice_lab:sandbox",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission voice_lab:sandbox"
        )

    async def _events() -> AsyncIterator[str]:
        async for entry_id, event in service.consume(room_name):
            yield _sse_frame(entry_id, event)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
