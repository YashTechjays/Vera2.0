"""Coaching mode: a supervisor sends Vera a short instruction (typed, or spoken
via hold-to-whisper) that she folds into her next natural reply — the customer
never hears or sees it. Same authorization rule as Intervene (owner OR
`calls:intervene`, see `control_plane.call_authz`), but NOT the single-intervener
lock: any number of authorized supervisors may coach the same call concurrently.

Whisper transcription (`/on-demand-transcribe`) is a separate step — it turns
audio into text for the supervisor to review/edit, and shares this router's
authorization + rate limit, but does not itself write anything to the
transcript/intervention record. Only an actual send (this router's `/coach`,
`origin="whisper"`) does that; see `on_demand_transcribe`'s docstring.
"""

import logging
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from opentelemetry import trace
from pydantic import BaseModel, Field
from sqlalchemy import select

from control_plane.api.v1.common import Audit, TenantId, TenantSession, emit_phi_read_audit
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver, require
from control_plane.call_authz import authorize_or_403, call_hidden_from
from control_plane.call_closeout import TERMINAL_VALUES
from control_plane.deps import (
    get_call_rate_limiter,
    get_call_stream_service,
    get_trace_link_store,
    get_whisper_stt,
)
from control_plane.exceptions import (
    ConflictError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.rate_limit import CallRateLimiter, check_rate_limit
from control_plane.responses import ResponseModel, ok
from vera_core.call_stream import CallStreamService
from vera_core.models import Call, InterventionEvent
from vera_core.models.enums import InterventionType
from vera_core.observability import TraceLinkStore, call_trace_attributes, room_name_for_call
from vera_core.stt import ResilientSTT, STTUnavailableError
from vera_core.transcript import ROLE_COACHING, ROLE_WHISPER, SOURCE_SUPERVISOR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calls"])
_tracer = trace.get_tracer("vera.control_plane.coaching")

# Content is PHI — kept bounded so an unbounded body can't be smuggled through
# a text field. 2000 chars comfortably covers even a rambling whisper transcript.
_MAX_MESSAGE_LENGTH = 2000
# Reject an oversized whisper upload before it ever reaches the STT provider —
# PHI-bearing audio should never be buffered unbounded.
_MAX_WHISPER_AUDIO_BYTES = 5 * 1024 * 1024


class CoachRequest(BaseModel):
    message: str = Field(min_length=1, max_length=_MAX_MESSAGE_LENGTH)
    # Internal-only distinction (proposal "Transcript Representation") — both
    # origins go through this same send path; whisper is typed text by the time
    # it gets here (the supervisor reviewed/edited the STT output first).
    origin: Literal["typed", "whisper"] = "typed"


class WhisperTranscribeResponse(BaseModel):
    text: str


async def _load_live_call(call_id: UUID, caller_user_id: UUID, session: TenantSession) -> Call:
    """Visibility-gated (404, never 403 — no enumeration of a private call) fetch
    of a call that hasn't ended yet (409 if it has)."""
    call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
    if call is None or call_hidden_from(call, caller_user_id):
        raise NotFoundError(message="call not found")
    if call.current_status in TERMINAL_VALUES:
        raise ConflictError(message="call already ended")
    return call


@router.post(
    "/calls/{call_id}/coach",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.RATE_LIMIT_EXCEEDED,
    ),
)
async def coach_call(
    call_id: UUID,
    body: CoachRequest,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    call_stream: Annotated[CallStreamService, Depends(get_call_stream_service)],
    rate_limiter: Annotated[CallRateLimiter, Depends(get_call_rate_limiter)],
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[None]:
    response.headers["Cache-Control"] = "no-store"
    call = await _load_live_call(call_id, caller.user_id, session)
    await authorize_or_403(
        call, tenant_id, caller, session, resolver, audit, request, audit_log_allows=False
    )
    await check_rate_limit(rate_limiter, call_id)

    role = ROLE_WHISPER if body.origin == "whisper" else ROLE_COACHING
    intervention_type = (
        InterventionType.WHISPER if body.origin == "whisper" else InterventionType.COACH
    )
    # The audit/reporting trail for coaching — NOT the WORM audit_log (that stays
    # reserved for session-level events like CALL_INTERVENE_JOIN; a per-message
    # entry there would be high-frequency noise). category is None: InterventionCategory
    # enumerates failure causes (hallucination, off_script, ...), which don't apply here.
    session.add(
        InterventionEvent(
            tenant_id=tenant_id,
            call_id=call.id,
            supervisor_id=caller.user_id,
            type=intervention_type.value,
            category=None,
            payload_ref={"message": body.message},
        )
    )
    # This single publish reaches the live SSE transcript, the DB finalizer
    # (once the call ends), and the agent worker's coaching listener - no extra
    # plumbing per consumer. Not atomic with the InterventionEvent row above:
    # that commits when the request ends, this runs inline before it - a crash
    # in between would publish the note with no row behind it (rare).
    await call_stream.publish_turn(
        room_name_for_call(tenant_id, call.id),
        role,
        body.message,
        ts=int(time.time() * 1000),
        source=SOURCE_SUPERVISOR,
        user_id=str(caller.user_id),
    )
    return ok(None)


@router.post(
    "/calls/{call_id}/on-demand-transcribe",
    response_model=ResponseModel[WhisperTranscribeResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.RATE_LIMIT_EXCEEDED,
        DefaultExceptionCode.SERVICE_UNAVAILABLE,
    ),
)
async def on_demand_transcribe(
    call_id: UUID,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    rate_limiter: Annotated[CallRateLimiter, Depends(get_call_rate_limiter)],
    whisper_stt: Annotated[ResilientSTT, Depends(get_whisper_stt)],
    trace_links: Annotated[TraceLinkStore, Depends(get_trace_link_store)],
    audio: UploadFile,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[WhisperTranscribeResponse]:
    """Hold-to-whisper transcription only — the supervisor reviews/edits the
    returned text and sends it through `/coach` (`origin="whisper"`) themselves.
    Shares `/coach`'s authorization and rate-limit budget (one combined 15/min
    pool per call, per the confirmed product decision), but never writes an
    InterventionEvent itself — auditing only the actual send avoids double-
    counting one whisper action as two records."""
    response.headers["Cache-Control"] = "no-store"
    call = await _load_live_call(call_id, caller.user_id, session)
    await authorize_or_403(
        call, tenant_id, caller, session, resolver, audit, request, audit_log_allows=False
    )
    await check_rate_limit(rate_limiter, call_id)

    audio_bytes = await audio.read(_MAX_WHISPER_AUDIO_BYTES + 1)
    if len(audio_bytes) > _MAX_WHISPER_AUDIO_BYTES:
        raise CustomAPIException(
            DefaultExceptionCode.BAD_REQUEST, message="audio exceeds the size limit"
        )
    room_name = room_name_for_call(tenant_id, call.id)
    try:
        # Joins the agent worker's trace for this call (published at the job entrypoint),
        # so whisper spend sums into the call's total. If the link is missing or expired
        # this is None and the span becomes its own trace root — degraded, not broken.
        # The nested vera.stt.usage generation inherits whichever we get.
        #
        # record_exception/set_status_on_exception are OFF: an STT provider error can
        # embed the request payload (the supervisor's audio), and both would copy its
        # message onto the span.
        with _tracer.start_as_current_span(
            "vera.coaching.whisper",
            context=await trace_links.resolve(room_name),
            attributes=call_trace_attributes(room_name),
            record_exception=False,
            set_status_on_exception=False,
        ):
            text = await whisper_stt.transcribe(
                audio_bytes, mime_type=audio.content_type or "audio/webm"
            )
    except STTUnavailableError as exc:
        raise CustomAPIException(
            DefaultExceptionCode.SERVICE_UNAVAILABLE,
            message="transcription temporarily unavailable",
        ) from exc
    await emit_phi_read_audit(
        audit,
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="whisper_transcript",
        resource_id=str(call.id),
        fields=["text"],
    )
    return ok(WhisperTranscribeResponse(text=text))
