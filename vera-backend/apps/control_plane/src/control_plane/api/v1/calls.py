"""Verification-call endpoints: join-token, active-list, live event stream,
publish, and end.

`publish` is owner-only: the caller must hold `calls:publish` *and* be the call's
`initiated_by_id` (explicit 403 in-handler). A publish-capable join token
(`?intervene=true`) additionally requires `calls:intervene`, checked after the
visibility 404s, and claims the call's single-intervener lock.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import (
    AppSettings,
    Audit,
    CallPlans,
    LiveKit,
    TenantId,
    TenantSession,
    emit_phi_read_audit,
)
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver, require
from control_plane.call_authz import authorize_or_403 as _authorize_or_403
from control_plane.call_authz import call_hidden_from as _call_hidden_from
from control_plane.call_authz import visible_to as _visible_to
from control_plane.call_closeout import TERMINAL_VALUES, announce_terminal_status, close_call
from control_plane.call_summary import (
    CallSummaryResponse,
    SummaryCache,
    summarize_call,
    transcript_role,
)
from control_plane.deps import (
    current_elevation,
    current_identity,
    get_call_stream_service,
    get_kms,
    get_sessionmaker,
    get_summary_cache,
    get_summary_llm,
)
from control_plane.dispatch import run_dispatch_pass
from control_plane.exceptions import (
    ConflictError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.post_call import resolve_ai_processing
from control_plane.recording_storage import SigningUnavailable, parse_gcs_uri
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from control_plane.sse import frames_with_keepalive
from control_plane.transcript_finalizer import finalize_transcript
from vera_core.audit import AuditRecord, AuditSink
from vera_core.call_stream import (
    TYPE_CALL_STATUS,
    TYPE_TRANSCRIPT,
    CallStreamEvent,
    CallStreamService,
)
from vera_core.config.kms import KeyManagementService
from vera_core.db.rls import tenant_session
from vera_core.llm import LLMUnavailableError, ResilientLLM
from vera_core.models import Call, InterventionEvent, PatientForm, Recording, Transcript
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import AccountType, CallStatus, InterventionType, RecordingStatus
from vera_core.observability.correlation import (
    PARTICIPANT_MODE_ATTR,
    PARTICIPANT_MODE_INTERVENER,
    PARTICIPANT_MODE_LISTENER,
    SUPERVISOR_IDENTITY_PREFIX,
    room_name_for_call,
)
from vera_core.schemas import CallStats, CallSummary, JoinTokenResponse, RecordingPlayback

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

# Bound on tailing a stream that may never appear (no stream yet, call still live;
# or the stream vanished between the EXISTS check and the read). Pre-answer ring/IVR
# wait is bounded ~60s; 180s sits safely above any legitimate pre-first-publish
# silence, so a genuinely stuck/never-dispatched room still lets go instead of
# pinning the SSE connection open forever. The deadline is checked after each idle
# XREAD BLOCK window, so worst-case termination latency is deadline + block_ms
# (~185s with the store's default 5s block).
_LIVE_TAIL_FIRST_ENTRY_DEADLINE_S: float = 180


def _epoch_ms(dt: datetime | None) -> int:
    return int(dt.timestamp() * 1000) if dt is not None else 0


def _sse_frame(entry_id: str, event: CallStreamEvent) -> str:
    """One SSE frame: the entry id line plus the full de-identified envelope as JSON."""
    return f"id: {entry_id}\ndata: {event.model_dump_json()}\n\n"


def _sse_response(frames: AsyncIterator[str]) -> StreamingResponse:
    """SSE response with the no-store / no-buffering headers every call-events branch returns."""
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _supervisor_identity(user_id: UUID) -> str:
    """LiveKit participant identity for a VA listening in on a call. Uses the
    shared observer prefix so the worker never treats a supervisor as the call's
    speaker (see vera_core.observability.correlation.is_observer_identity)."""
    return f"{SUPERVISOR_IDENTITY_PREFIX}{user_id}"


async def _holder_still_present(livekit: LiveKit, room_name: str, holder: UUID) -> bool:
    """Is the lock holder still in the room? On timeout, assume yes (don't steal)."""
    try:
        async with asyncio.timeout(_PRESENCE_PROBE_TIMEOUT):
            identities = (await livekit.room_participant_identities(room_name)) or []
    except TimeoutError:
        return True
    return _supervisor_identity(holder) in identities


# A just-minted intervene token belongs to a user not yet connected to LiveKit, so
# a presence probe would wrongly call the lock stale. Inside this window a held
# lock is refused outright; past it, the holder must be in the room or it's stolen.
_INTERVENE_CONNECT_GRACE = timedelta(seconds=30)
_LISTEN_TOKEN_TTL = timedelta(minutes=5)  # listen-only can't publish, no race
_PRESENCE_PROBE_TIMEOUT = 3.0  # cap the LiveKit probe so it can't hold the row lock


async def _intervener_lock_live(
    session: AsyncSession, livekit: LiveKit, room_name: str, call: Call, holder: UUID
) -> bool:
    """Whether the claim still counts: inside the connect grace, or the holder is in the room."""
    claimed_at = call.intervener_claimed_at
    db_now = (await session.execute(select(func.now()))).scalar_one()
    if claimed_at is not None and db_now - claimed_at < _INTERVENE_CONNECT_GRACE:
        return True
    return await _holder_still_present(livekit, room_name, holder)


def _summary(
    call: Call,
    patient_name: str | None,
    caller_id: UUID,
    insurance_provider: str | None = None,
    insurance_type: str | None = None,
) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        form_id=call.form_id,
        status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name,
        insurance_provider=insurance_provider,
        insurance_type=insurance_type,
        started_at=call.started_at,
        ended_at=call.ended_at,
        created_at=call.created_at,
        published=call.published,
        is_owner=call.initiated_by_id == caller_id,
        health_score=call.health_score,
        health_flag=call.health_flag,
        health_reason=call.health_reason,
        health_analyzed_at=call.health_analyzed_at,
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
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    intervene: bool = False,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[JoinTokenResponse]:
    response.headers["Cache-Control"] = "no-store"  # publish-capable JWT + email; never cache
    # Intervening claims the single-intervener lock — the row lock serializes
    # concurrent claims while the listen path stays lock-free.
    stmt = select(Call).where(Call.id == call_id)
    if intervene:
        stmt = stmt.with_for_update()
    call = (await session.execute(stmt)).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if _call_hidden_from(call, caller.user_id):
        raise NotFoundError(message="call not found")  # 404 not 403: don't reveal a private call
    room_name = room_name_for_call(tenant_id, call.id)
    stolen_from: UUID | None = None
    if intervene:
        # Checked AFTER the visibility 404s so a private call never turns into a 403.
        await _authorize_or_403(call, tenant_id, caller, session, resolver, audit, request)
        if call.current_status in TERMINAL_VALUES:
            raise ConflictError(message="call already ended")

        holder = call.intervener_user_id
        if holder == caller.user_id:
            # The holder reconnecting (tab refresh/crash) — refresh the claim only.
            call.intervener_claimed_at = func.now()
        else:
            if holder is not None:
                if await _intervener_lock_live(session, livekit, room_name, call, holder):
                    raise ConflictError(
                        message="another supervisor is currently intervening on this call"
                    )
                stolen_from = holder  # claim aged past grace and holder left the room
            call.intervener_user_id = caller.user_id
            call.intervener_claimed_at = func.now()
            # Intervention audit trail: a row = an intervention occurred (ADR §6).
            session.add(
                InterventionEvent(
                    tenant_id=tenant_id,
                    call_id=call.id,
                    supervisor_id=caller.user_id,
                    type=InterventionType.TAKEOVER.value,
                    payload_ref={},
                )
            )
    # Every join is audited (owner included — their join is a PHI access too); the
    # event name carries the mode: listen-only or intervene.
    detail: dict[str, object] = {
        "owner_id": str(call.initiated_by_id) if call.initiated_by_id else None
    }
    if stolen_from is not None:
        detail["stale_lock_released"] = str(stolen_from)
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=(
                AuditEvent.CALL_INTERVENE_JOIN if intervene else AuditEvent.CALL_LISTEN_ONLY_JOIN
            ).value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:intervene" if intervene else "calls:read",
            decision="allow",
            request_id=current_request_id(request),
            detail=detail,
        )
    )
    identity = _supervisor_identity(caller.user_id)
    # Watch-only tokens are server-side mute; only ?intervene=true may publish.
    token = livekit.mint_join_token(
        room_name=room_name,
        identity=identity,
        can_publish=intervene,
        name=caller.email or caller.subject,
        attributes={
            PARTICIPANT_MODE_ATTR: (
                PARTICIPANT_MODE_INTERVENER if intervene else PARTICIPANT_MODE_LISTENER
            )
        },
        # Cap the intervene token at the grace so a stale token can't outlive a stolen lock.
        ttl=_INTERVENE_CONNECT_GRACE if intervene else _LISTEN_TOKEN_TTL,
    )
    return ok(JoinTokenResponse(token=token, url=livekit.url, room_name=room_name))


async def _authorize_call_read(
    call_id: UUID,
    request: Request,
    identity: VerifiedIdentity,
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: PermissionResolver,
    audit: AuditSink,
    *,
    resource_type: str,
) -> Call:
    """Shared read gate for the live-monitoring surfaces (event stream, summary):
    tenant caller + calls:read + owner-or-published visibility, with the folded
    authz+PHI audit record both endpoints must emit. Raises the same 404/403
    shapes as stream_call_events."""
    if identity.account_type is not AccountType.TENANT or identity.tenant_id is None:
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
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type=resource_type,
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
    return call


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
    published/ownerless call. Authorization runs in a SHORT-LIVED tenant session
    released before streaming (an SSE is long-lived and must not pin a DB
    connection — mirrors voice_lab.stream_transcript)."""
    call = await _authorize_call_read(
        call_id, request, identity, sessionmaker, resolver, audit, resource_type="call_events"
    )
    tenant_id = call.tenant_id
    room_name = room_name_for_call(tenant_id, call.id)

    # Task 16 persists transcripts to the DB and deletes the stream at closeout, so
    # a terminal call's stream is normally already gone — one EXISTS round-trip
    # decides which record to serve. Race: the stream is deleted between this check
    # and the tail branch's read (every live->terminal transition opens this
    # window) — the tail terminates at its first-entry deadline instead of hanging.
    # Or a stream is (re)created between this EXISTS(false) and the terminal
    # branch's DB read below — the finalizer will persist those rows too; the
    # client just gets the DB snapshot as it stood at read time.
    stream_exists = await service.exists(room_name)

    if not stream_exists and call.current_status in TERMINAL_VALUES:
        async with tenant_session(sessionmaker, tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(Transcript)
                        .where(Transcript.call_id == call.id)
                        .order_by(Transcript.seq)
                    )
                )
                .scalars()
                .all()
            )
        db_events = [
            (
                f"db-{row.seq}",
                CallStreamEvent(
                    type=TYPE_TRANSCRIPT,
                    data={
                        "role": transcript_role(row),
                        "source": row.source,
                        "text": row.message,
                    },
                    ts=_epoch_ms(row.spoke_at),
                ),
            )
            for row in rows
        ]
        db_events.append(
            (
                "db-status",
                CallStreamEvent(
                    type=TYPE_CALL_STATUS,
                    data={"status": call.current_status},
                    ts=_epoch_ms(call.ended_at) or int(time.time() * 1000),
                ),
            )
        )

        async def _db_frames() -> AsyncIterator[str]:
            for entry_id, event in db_events:
                yield _sse_frame(entry_id, event)

        return _sse_response(_db_frames())

    # Tail branch (stream exists, OR no stream but the call is still live — the
    # worker may not have published its first event yet, or never will after a
    # crashed dispatch). BOTH cases carry the first-entry deadline: a stream that
    # exists always has >= 1 entry, so the replay-from-0 first read marks it seen
    # immediately and the deadline can never fire for a genuinely live stream — it
    # only bounds the exists->deleted TOCTOU window above, where a None deadline on
    # a now-vanished, never-seen stream would pin the SSE connection open forever.

    return _sse_response(
        frames_with_keepalive(
            service.consume(room_name, first_entry_deadline_s=_LIVE_TAIL_FIRST_ENTRY_DEADLINE_S),
            _sse_frame,
        )
    )


@router.get(
    "/calls/{call_id}/summary",
    response_model=ResponseModel[CallSummaryResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.SERVICE_UNAVAILABLE,
    ),
)
async def get_call_summary(
    call_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    stream: Annotated[CallStreamService, Depends(get_call_stream_service)],
    summary_llm: Annotated[ResilientLLM, Depends(get_summary_llm)],
    summary_cache: Annotated[SummaryCache, Depends(get_summary_cache)],
    settings: AppSettings,
) -> ResponseModel[CallSummaryResponse]:
    """On-demand supervisor-handoff summary of the call's transcript so far
    (Live Monitoring's Summary tab). Same visibility/authz/audit gate as the
    event stream; result is cached a few seconds (settings.summary_cache_ttl_seconds)
    so repeated tab flips don't fan out LLM calls."""
    call = await _authorize_call_read(
        call_id, request, identity, sessionmaker, resolver, audit, resource_type="call_summary"
    )
    response.headers["Cache-Control"] = "no-store"
    try:
        async with asyncio.timeout(settings.summary_total_timeout_seconds):
            result = await summarize_call(
                llm=summary_llm,
                cache=summary_cache,
                stream=stream,
                sessionmaker=sessionmaker,
                tenant_id=call.tenant_id,
                call_id=call.id,
                ttl_seconds=settings.summary_cache_ttl_seconds,
            )
    except (LLMUnavailableError, TimeoutError) as exc:
        raise CustomAPIException(
            DefaultExceptionCode.SERVICE_UNAVAILABLE,
            message="summary temporarily unavailable",
        ) from exc
    return ok(result)


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
    row = (
        await session.execute(
            select(
                PatientForm.patient_name,
                PatientForm.insurance_provider,
                FormSchema.insurance_type,
            )
            .join(SchemaVersion, SchemaVersion.id == PatientForm.schema_version_id)
            .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
            .where(PatientForm.id == call.form_id)
        )
    ).one_or_none()
    patient_name, insurance_provider, insurance_type = row if row else (None, None, None)
    await emit_phi_read_audit(
        audit,
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="call",
        resource_id=str(call.id),
        fields=["patient_name", "insurance_provider", "health_reason"],
    )
    return ok(_summary(call, patient_name, caller.user_id, insurance_provider, insurance_type))


@router.post(
    "/calls/{call_id}/end",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def end_call(
    call_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    call_stream: Annotated[CallStreamService, Depends(get_call_stream_service)],
    kms: Annotated[KeyManagementService, Depends(get_kms)],
    call_plans: CallPlans,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[None]:
    """End a call from Live Monitoring.

    LIVE call (answered — started_at set): stamp the caller's end intent on the
    row (durable: if the worker's call.ended never arrives, the sweeper closes
    the call as CANCELED instead of FAILED, so a user-ended call is never
    auto-redialed), then delete the room; the worker's shutdown emits call.ended
    and the consumer runs the one true closeout.

    PRE-ANSWER call (still dialing): no worker session exists, so no call.ended
    will ever come — close synchronously as CANCELED through the shared
    close_call path FIRST, then delete the room (order is load-bearing: room
    deletion makes the worker publish call.failed, which must find the row
    already terminal and no-op).

    Visibility matches join-token (`_call_hidden_from`): anyone who may watch
    the call may end it if it is published (VR2-59 tightens this to just the
    owner — see below); a hidden call 404s so it is never revealed. While a
    takeover is live, only the intervening supervisor may end the call.

    VR2-59: before anyone has EVER intervened (`intervener_user_id` still
    null), only the call's owner may end it — a published/"visible to all"
    call let every watching VA end a call they never joined. A stale/abandoned
    takeover (crashed supervisor) keeps the existing crash-recovery openness:
    any viewer may still end it, same as before this change. Ownerless calls
    (dispatcher-created, no `initiated_by_id`) are unaffected: there is no
    owner to restrict to.
    """
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None or _call_hidden_from(call, caller.user_id):
        raise NotFoundError(message="call not found")
    if call.current_status in TERMINAL_VALUES:
        return ok(None, message="Call already ended.")  # idempotent no-op
    room_name = room_name_for_call(tenant_id, call.id)
    # Lock-free read by design: holding the row lock across the presence probe is worse
    # than the benign race with a fresh claim (worst case: a moments-ago-legal end).
    holder = call.intervener_user_id
    if (
        holder is not None
        and holder != caller.user_id
        and await _intervener_lock_live(session, livekit, room_name, call, holder)
    ):
        raise ConflictError(message="only the intervening supervisor can end this call")
    # Only gates the never-intervened case: a stale/abandoned holder (crashed
    # supervisor) already falls through the check above, and must stay endable
    # by any viewer — that's the crash-recovery path, distinct from VR2-59.
    if (
        holder is None
        and call.initiated_by_id is not None
        and call.initiated_by_id != caller.user_id
    ):
        raise ConflictError(message="only the call's owner can end this call before intervention")
    pre_answer = call.started_at is None
    actor_label = caller.email or caller.subject
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=actor_label,
            event_type=AuditEvent.CALL_END.value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:read",
            decision="allow",
            request_id=current_request_id(request),
            detail={
                "owner_id": str(call.initiated_by_id) if call.initiated_by_id else None,
                "phase": "pre_answer" if pre_answer else "live",
            },
        )
    )
    if pre_answer:
        closed = await close_call(
            sessionmaker,
            audit,
            room_name,
            CallStatus.CANCELED,
            trigger="user_end_call",
            actor_label=actor_label,
            end_requested_by=caller.user_id,
        )
        await livekit.delete_room(room_name)
        if closed is not None:  # freed a concurrency slot — let queued forms use it
            ref, _ = closed  # a stamped close is always applied as CANCELED
            # Tell anyone tailing the live SSE before the finalizer deletes the
            # stream (the worker never publishes for a pre-answer call).
            await announce_terminal_status(call_stream, room_name, CallStatus.CANCELED)
            await finalize_transcript(sessionmaker, call_stream, ref, room_name)
            # The cancel parked the form in AI_PROCESSING (the transcript rides
            # the normal post-call pipeline); resolve it to EXCEPTION_REVIEW
            # now — the resolver's canceled gate never auto-requeues.
            await resolve_ai_processing(
                sessionmaker, audit, ref, trigger="user_end_call", actor_label=actor_label
            )
            await run_dispatch_pass(
                sessionmaker, tenant_id, livekit, kms, audit, plan_service=call_plans
            )
        return ok(None, message="Call canceled.")
    async with tenant_session(sessionmaker, tenant_id) as stamp_session:
        locked = (
            await stamp_session.execute(select(Call).where(Call.id == call_id).with_for_update())
        ).scalar_one_or_none()
        if locked is not None and locked.current_status not in TERMINAL_VALUES:
            locked.end_requested_by_id = caller.user_id
    # Idempotent server-side: deleting an already-gone room is a no-op, and the
    # in-flight call.ended event resolves the call's status either way.
    await livekit.delete_room(room_name)
    return ok(None, message="Call is ending.")


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
    scope: Literal["live", "history"] = "live",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    """`scope=live` (default) lists in-flight calls — unbounded unless `limit`
    is passed (capping it by default could silently hide live calls from
    monitoring); `scope=history` returns the most recent terminal calls,
    capped at `limit` (default 50)."""
    response.headers["Cache-Control"] = "no-store"
    status_cond = (
        Call.current_status.in_(list(_ACTIVE_STATUSES))
        if scope == "live"
        else Call.current_status.in_(TERMINAL_VALUES)
    )
    query = (
        select(
            Call,
            PatientForm.patient_name,
            PatientForm.insurance_provider,
            FormSchema.insurance_type,
        )
        .join(PatientForm, PatientForm.id == Call.form_id)
        .join(SchemaVersion, SchemaVersion.id == PatientForm.schema_version_id)
        .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
        .where(status_cond)
        .where(_visible_to(caller.user_id))
        .order_by(Call.created_at.desc())
    )
    effective_limit = (limit or 50) if scope == "history" else limit
    if effective_limit is not None:
        query = query.limit(effective_limit)
    rows = (await session.execute(query)).all()
    # PHI disclosure — audit field names, mirroring list_patient_forms.
    await emit_phi_read_audit(
        audit,
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="call",
        resource_id="list",
        fields=["patient_name", "insurance_provider", "health_reason"],
    )
    return ok(
        [
            _summary(c, name, caller.user_id, provider, insurance_type)
            for c, name, provider, insurance_type in rows
        ]
    )


@router.get(
    "/calls/stats",
    response_model=ResponseModel[CallStats],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def call_stats(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[CallStats]:
    """Counts for the Live Monitoring stat cards, over the same calls the list
    shows the caller. Pure counts (no PHI), so no disclosure audit; "today" is
    the DB clock's UTC day."""
    response.headers["Cache-Control"] = "no-store"
    # Structural UTC "today": date_trunc truncates in the session TimeZone, so
    # shift to UTC, truncate, then re-anchor the naive result as UTC.
    utc_midnight = func.timezone("UTC", func.date_trunc("day", func.timezone("UTC", func.now())))
    row = (
        await session.execute(
            select(
                func.count().filter(Call.created_at >= utc_midnight),
                func.count().filter(Call.current_status.in_(list(_ACTIVE_STATUSES))),
                func.count().filter(Call.current_status == CallStatus.CRITICAL),
            )
            .select_from(Call)
            .where(_visible_to(caller.user_id))
        )
    ).one()
    total_today, live, critical = row
    return ok(CallStats(total_today=total_today, live=live, critical=critical))


@router.get(
    "/calls/{call_id}/recording",
    response_model=ResponseModel[RecordingPlayback],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def get_recording_playback(
    call_id: UUID,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    settings: AppSettings,
    caller: VerifiedIdentity = require("recordings:read"),
) -> ResponseModel[RecordingPlayback]:
    """Mint a TTL-bounded signed URL for the call's recording.

    Authorization is permission AND call visibility (spec decision 6): the
    recording is never more visible than the call itself. Every issuance is a
    PHI disclosure → RECORDING_ACCESSED on the append-only audit trail.
    """
    response.headers["Cache-Control"] = "no-store"
    call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if _call_hidden_from(call, caller.user_id):
        raise NotFoundError(message="call not found")  # don't reveal a private call

    storage = request.app.state.recording_storage
    if storage is None:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="call recording is not configured"
        )

    # Latest AVAILABLE wins: a newer FAILED/PENDING attempt must not shadow a
    # playable recording (only its absence makes the call unplayable).
    recording = (
        await session.execute(
            select(Recording)
            .where(Recording.call_id == call_id)
            .order_by(
                (Recording.status == RecordingStatus.AVAILABLE.value).desc(),
                Recording.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if recording is None:
        raise NotFoundError(message="no recording for this call")
    if recording.status != RecordingStatus.AVAILABLE.value:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=f"recording is not available (status: {recording.status})",
        )

    try:
        bucket, object_path = parse_gcs_uri(recording.gcs_uri)
    except ValueError as exc:
        # A malformed stored pointer is an operational defect, not a caller error —
        # surface a clean envelope instead of an unhandled 500. URI is UUID-only.
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="recording pointer is invalid"
        ) from exc
    ttl = settings.recording_signed_url_ttl_seconds
    try:
        url = await storage.signed_url(bucket, object_path, ttl_seconds=ttl)
    except SigningUnavailable as exc:
        # User ADC / missing signBlob grant — a clean envelope, not a raw 500.
        logger.error("signed-url minting failed (%s)", exc)
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="recording storage cannot mint playback URLs in this environment",
        ) from exc
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.RECORDING_ACCESSED.value,
            resource_type="recording",
            resource_id=str(recording.id),
            permission_key="recordings:read",
            decision="allow",
            request_id=current_request_id(request),
            elevation_session_id=current_elevation(request),
            detail={"call_id": str(call_id), "ttl_seconds": ttl},
        )
    )
    # expires_at is informational for the client; the URL's own signature is the
    # enforcement (GCS rejects after expiry regardless of this field).
    return ok(
        RecordingPlayback(
            url=url,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
    )
