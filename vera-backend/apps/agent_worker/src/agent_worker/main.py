"""Vera agent worker — Deepgram->Gemini->Cartesia cascade over LiveKit.

Explicit dispatch only (agent_name set): the control plane dispatches this worker
into a room named by vera_core.observability.correlation.room_name_for_call. The
room name IS the session id and the Langfuse correlation key.
"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import (
    NOT_GIVEN,
    JobContext,
    JobProcess,
    NotGiven,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from opentelemetry import trace
from redis.asyncio import Redis

from agent_worker.agent import build_agent
from agent_worker.cascade import _build_vad, build_session
from agent_worker.prompt import build_instructions, parse_persona_tweak, resolve_greeting
from agent_worker.transcript_publisher import (
    FanOutTurnPublisher,
    ReorderingEmitter,
    TurnPublisher,
    attach_transcript_publisher,
)
from vera_core.call_stream import CallStreamService, RedisCallStreamStore
from vera_core.config.settings import get_settings
from vera_core.events import (
    CallAnsweredEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
    WorkerEventBus,
)
from vera_core.observability.correlation import (
    call_trace_attributes,
    is_observer_identity,
    parse_room_name,
)
from vera_core.observability.otel import configure_observability
from vera_core.phi import build_phi_boundary
from vera_core.redis import create_redis
from vera_core.transcript import RedisTranscriptStore, TranscriptService

logger = logging.getLogger("agent_worker")

# Bound the wait so a never-answered outbound call doesn't pin a worker forever.
_SPEAKER_TIMEOUT_S = 60.0

# A SIP participant joins the room while it is still *ringing* (JOINING state) and
# only sets sip.callStatus="active" once the callee answers. Greeting on mere
# presence would talk into a ringing phone, so the SIP callee counts as ready only
# once answered. (LiveKit's AMD uses the same signal.)
_SIP_CALL_STATUS_ATTR = "sip.callStatus"
_SIP_CALL_STATUS_ACTIVE = "active"


def _is_ready_speaker(participant: rtc.Participant) -> bool:
    """A participant whose presence means the agent may greet: a browser caller
    (ready as soon as it joins) or a SIP callee that has answered. Observers
    (monitor, supervisor) never qualify, and a SIP callee that is merely ringing
    does not yet."""
    if is_observer_identity(participant.identity):
        return False
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return participant.attributes.get(_SIP_CALL_STATUS_ATTR) == _SIP_CALL_STATUS_ACTIVE
    return True


@dataclass(frozen=True)
class SpeakerReady:
    """A ready, non-monitor participant is present — the agent may start/greet."""

    participant: rtc.RemoteParticipant


@dataclass(frozen=True)
class CallFailed:
    """The outbound call did not connect (busy/declined/no-answer/trunk error)."""

    reason: CallFailureReason


type WaitResult = SpeakerReady | CallFailed

# SIP disconnect reason (int enum) → user-facing failure class. Anything not listed
# (incl. None and SIP_TRUNK_FAILURE) is an opaque failure.
_SIP_FAILURE_REASONS: dict[int, CallFailureReason] = {
    rtc.DisconnectReason.USER_REJECTED: CallFailureReason.BUSY_OR_DECLINED,
    rtc.DisconnectReason.USER_UNAVAILABLE: CallFailureReason.NO_ANSWER,
}


def classify_sip_disconnect(reason: int | None) -> CallFailureReason:
    if reason is None:
        return CallFailureReason.FAILED
    return _SIP_FAILURE_REASONS.get(reason, CallFailureReason.FAILED)


async def wait_for_speaker(ctx: JobContext, timeout_s: float = _SPEAKER_TIMEOUT_S) -> WaitResult:
    """Block until the call is ready to run or has failed.

    Returns SpeakerReady once the browser caller joins or the SIP callee answers
    (sip.callStatus == "active"). Returns CallFailed if the SIP callee drops before
    answering (busy/declined/trunk error) or nobody becomes ready within timeout_s
    (treated as no-answer). Subscribe to events BEFORE scanning existing participants
    so a join/attr/disconnect in the gap is never missed.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[WaitResult] = loop.create_future()

    def _resolve_ready(participant: rtc.Participant) -> None:
        if result.done() or not _is_ready_speaker(participant):
            return
        remote = ctx.room.remote_participants.get(participant.identity)
        if remote is not None:
            result.set_result(SpeakerReady(remote))

    def _on_connected(participant: rtc.RemoteParticipant) -> None:
        logger.info(
            "wait_for_speaker[%s]: participant_connected identity=%s kind=%s sip.callStatus=%s",
            ctx.room.name,
            participant.identity,
            participant.kind,
            participant.attributes.get(_SIP_CALL_STATUS_ATTR),
        )
        _resolve_ready(participant)

    def _on_attributes_changed(_changed: dict[str, str], participant: rtc.Participant) -> None:
        _resolve_ready(participant)

    def _on_disconnected(participant: rtc.RemoteParticipant) -> None:
        # A SIP callee dropping before it answered means the outbound call failed.
        logger.info(
            "wait_for_speaker[%s]: participant_disconnected identity=%s kind=%s reason=%s",
            ctx.room.name,
            participant.identity,
            participant.kind,
            participant.disconnect_reason,
        )
        if result.done() or participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        result.set_result(CallFailed(classify_sip_disconnect(participant.disconnect_reason)))

    def _on_room_disconnected(reason: object = None) -> None:
        # The whole room went away mid-dial (user canceled from Live Monitoring,
        # sweeper teardown, server-side delete). Resolve immediately so the
        # entrypoint exits cleanly and publishes an outcome — left unresolved,
        # the framework force-cancels the job and no call.failed is ever emitted.
        # The consumer no-ops if the control plane already closed the call.
        logger.info(
            "wait_for_speaker[%s]: room disconnected (%s) — resolving as failed",
            ctx.room.name,
            reason,
        )
        if not result.done():
            result.set_result(CallFailed(CallFailureReason.FAILED))

    ctx.room.on("participant_connected", _on_connected)
    ctx.room.on("participant_attributes_changed", _on_attributes_changed)
    ctx.room.on("participant_disconnected", _on_disconnected)
    ctx.room.on("disconnected", _on_room_disconnected)
    try:
        logger.info(
            "wait_for_speaker[%s]: participants at scan = %s",
            ctx.room.name,
            {
                p.identity: (str(p.kind), p.attributes.get(_SIP_CALL_STATUS_ATTR))
                for p in ctx.room.remote_participants.values()
            },
        )
        for p in ctx.room.remote_participants.values():
            _resolve_ready(p)
        if result.done():
            return result.result()
        async with asyncio.timeout(timeout_s):
            return await result
    except TimeoutError:
        return CallFailed(CallFailureReason.NO_ANSWER)
    finally:
        ctx.room.off("participant_connected", _on_connected)
        ctx.room.off("participant_attributes_changed", _on_attributes_changed)
        ctx.room.off("participant_disconnected", _on_disconnected)
        ctx.room.off("disconnected", _on_room_disconnected)


def build_room_input_options(speaker_identity: str | NotGiven) -> RoomInputOptions:
    """Audio-input + teardown policy for the AgentSession, shared by every path.

    Pin the agent's audio input to the resolved speaker when there is one. Otherwise RoomIO
    links to the first eligible participant — in Voice Lab outbound mode that can be the
    listen-only monitor (which publishes no audio), leaving the agent deaf to the SIP callee.
    A NOT_GIVEN identity (the /calls path) keeps RoomIO's auto-link to the sole participant.

    close_on_disconnect (the framework default) closes the session when that linked speaker
    hangs up; delete_room_on_close then deletes the room — dropping any listen-only monitor
    and hanging up the SIP leg — so a phone or browser hangup ends the whole call. This is the
    framework's own close→drain→delete path; a hand-rolled participant_disconnected handler
    that called delete_room directly raced this teardown and tore the engine down mid-drain.
    """
    return RoomInputOptions(
        participant_identity=speaker_identity,
        close_on_disconnect=True,
        delete_room_on_close=True,
    )


def session_id_for(room_name: str) -> str:
    """The room name is the session id (correlation key shared with the control plane)."""
    return room_name


async def _emit_call_failed(
    bus: WorkerEventBus, room_name: str, reason: CallFailureReason, *, now_ms: int
) -> None:
    """Publish the call.failed event the control plane consumes to tear the room down."""
    await bus.emit(CallFailedEvent(room_name=room_name, reason=reason, ts=now_ms))


class CallLifecycleEmitter:
    """Best-effort lifecycle signals to the control plane. A bus failure must never
    break a live call — log and continue (mirrors the transcript publisher's posture)."""

    def __init__(self, bus: WorkerEventBus, room_name: str) -> None:
        self._bus = bus
        self._room_name = room_name

    async def answered(self, *, now_ms: int) -> None:
        await self._emit(CallAnsweredEvent(room_name=self._room_name, ts=now_ms))

    async def ended(self, *, now_ms: int) -> None:
        await self._emit(CallEndedEvent(room_name=self._room_name, ts=now_ms))

    async def _emit(self, event: CallAnsweredEvent | CallEndedEvent) -> None:
        try:
            await self._bus.emit(event)
        except Exception:
            logger.exception("failed to emit %s for %s", event.type, self._room_name)


def resolve_session(room_name: str, *, is_local: bool) -> str | None:
    """Decide the correlation session id for a connected room, or None to reject it.

    A canonical vera call room (`call--<tenant>--<call>`) always runs. A foreign room
    name only runs in local dev — that's the livekit `console`/`connect` mic test, which
    gets a synthetic session id so the cascade can be exercised without a real call. In
    any non-local environment a foreign room is rejected: the agent never attaches to a
    room it wasn't dispatched to.
    """
    if parse_room_name(room_name) is not None:
        return session_id_for(room_name)
    if is_local:
        return room_name or "console"
    return None


def _fan_out_sink(sinks: list[TurnPublisher]) -> TurnPublisher | None:
    """Collapse the enabled stream sinks (transcript service, call stream) behind one
    TurnPublisher, so at most one ReorderingEmitter is ever attached per job: None with
    nothing enabled, the sink itself with exactly one, a FanOutTurnPublisher otherwise."""
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    return FanOutTurnPublisher(sinks)


def prewarm(proc: JobProcess) -> None:
    # Initialize OTel once per worker process so span attributes set in entrypoint
    # are exported to Langfuse.  No-op when settings.langfuse_host is None (local/CI).
    configure_observability(get_settings())
    proc.userdata["vad"] = _build_vad()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    room_name = ctx.room.name
    settings = get_settings()
    session_id = resolve_session(room_name, is_local=settings.is_local)
    if session_id is None:
        logger.warning("foreign room name %s — not a vera call room", room_name)
        return

    # One worker-event bus per job for canonical rooms (real calls AND voice-lab
    # rooms — the consumer no-ops when no Call row exists). Foreign/console rooms
    # get none.
    events_redis: Redis | None = None
    bus: WorkerEventBus | None = None
    lifecycle: CallLifecycleEmitter | None = None
    if parse_room_name(room_name) is not None:
        events_redis = create_redis(settings.redis_url)
        bus = WorkerEventBus(events_redis, maxlen=settings.worker_events_stream_maxlen)
        lifecycle = CallLifecycleEmitter(bus, room_name)

    # Declared here (not inside try) so the except below can always safely reference it,
    # even if setup fails before the publish_events block runs.
    call_stream_redis: Redis | None = None

    try:
        # Attach correlation attributes to the active OTel span so every pipeline
        # span is grouped under langfuse.session.id = room_name in Langfuse. For a
        # console/connect mic test (foreign room) this sets only `vera.room`.
        trace.get_current_span().set_attributes(call_trace_attributes(room_name))

        # Dispatch metadata gates greeting timing. The /calls path passes none, so it
        # keeps the immediate behavior; Voice Lab passes {"wait_for_speaker": true} so
        # the agent holds until the human/phone participant can actually hear it.
        speaker: rtc.RemoteParticipant | None = None
        meta = json.loads(ctx.job.metadata or "{}")
        if meta.get("wait_for_speaker"):
            # Log the metadata WITHOUT agent_context — that key carries raw patient/provider
            # identifiers (PHI) that must never reach a log. The rest of meta is non-PHI config.
            logger.info(
                "wait_for_speaker: entering for room %s (meta=%s)",
                room_name,
                {k: v for k, v in meta.items() if k != "agent_context"},
            )
            outcome = await wait_for_speaker(ctx)
            logger.info(
                "wait_for_speaker: outcome for room %s = %s", room_name, type(outcome).__name__
            )
            if isinstance(outcome, CallFailed):
                logger.warning(
                    "outbound call failed for room %s: %s", room_name, outcome.reason.value
                )
                if events_redis is not None and bus is not None:
                    try:
                        await _emit_call_failed(
                            bus, room_name, outcome.reason, now_ms=int(time.time() * 1000)
                        )
                    finally:
                        await events_redis.aclose()
                return
            speaker = outcome.participant
            # The SIP callee answering is the "call is live" signal; a browser caller
            # (voice-lab browser mode) is not an answered phone call.
            if lifecycle is not None and speaker.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                await lifecycle.answered(now_ms=int(time.time() * 1000))

        boundary = build_phi_boundary(settings)
        await boundary.open_session(session_id)

        # Tenant persona overlay arrives as opaque dispatch metadata (set by the control
        # plane). Fail-safe: bad/missing metadata falls back to the base persona.
        tweak = parse_persona_tweak(ctx.job.metadata if ctx.job is not None else None)
        instructions = build_instructions(tweak)
        greeting = resolve_greeting(tweak)

        session = build_session(vad=ctx.proc.userdata.get("vad"))

        # Live transcript publishing (Voice Lab opt-in via dispatch metadata; /calls unset).
        transcript_redis: Redis | None = None
        transcript_service: TranscriptService | None = None
        if meta.get("publish_transcript"):
            transcript_redis = create_redis(settings.redis_url)
            transcript_service = TranscriptService(
                RedisTranscriptStore(
                    transcript_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )

        # Real-call envelope stream (dispatcher opt-in via publish_events): transcript
        # turns + call_status frames for the /calls/{id}/events SSE. Reuses the
        # transcript TTL settings — same lifecycle, different stream.
        call_stream: CallStreamService | None = None
        if meta.get("publish_events"):
            call_stream_redis = create_redis(settings.redis_url)
            call_stream = CallStreamService(
                RedisCallStreamStore(
                    call_stream_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )

        # One reordering emitter fanned out to every enabled sink — the barge-in reorder
        # state machine lives once per job, not once per stream (see transcript_publisher).
        sinks: list[TurnPublisher] = [
            svc for svc in (transcript_service, call_stream) if svc is not None
        ]
        turn_sink = _fan_out_sink(sinks)
        turn_emitter: ReorderingEmitter | None = None
        if turn_sink is not None:
            turn_emitter = attach_transcript_publisher(session, turn_sink, room_name)

        if call_stream is not None and speaker is not None:
            # the callee already answered during wait_for_speaker
            await call_stream.publish_status(room_name, "active", ts=int(time.time() * 1000))

        async def _flush_turn_emitter() -> None:
            # Order is load-bearing: flush held turns BEFORE any service's end(). end()
            # appends the sentinel that stops readers, so a turn flushed after it would be
            # stranded behind the sentinel. One flush covers every fanned-out sink.
            if turn_emitter is not None:
                try:
                    await turn_emitter.aclose()
                except Exception:  # best-effort; never block shutdown
                    logger.exception("failed to flush turn emitter for %s", room_name)

        async def _end_transcript_stream() -> None:
            if transcript_service is not None:
                try:
                    await transcript_service.end(room_name)
                except Exception:  # best-effort; never block shutdown
                    logger.exception("failed to mark transcript ended for %s", room_name)
            if transcript_redis is not None:
                try:
                    await transcript_redis.aclose()
                except Exception:
                    logger.exception("failed to close transcript redis for %s", room_name)

        async def _end_call_stream() -> None:
            if call_stream is not None:
                try:
                    await call_stream.publish_status(room_name, "ended", ts=int(time.time() * 1000))
                    await call_stream.end(room_name)
                except Exception:
                    logger.exception("failed to end call stream for %s", room_name)
            if call_stream_redis is not None:
                try:
                    await call_stream_redis.aclose()
                except Exception:
                    logger.exception("failed to close call stream redis for %s", room_name)

        async def _on_shutdown() -> None:
            # Sequential, spec-pinned order: flush the shared emitter once, then
            # transcript-stream teardown, then call-stream teardown, then the PHI boundary.
            # Each step inside the helpers is best-effort (own try/except), so a failure
            # never skips the rest.
            await _flush_turn_emitter()
            await _end_transcript_stream()
            await _end_call_stream()
            await boundary.close_session(session_id)

            # Last: signal call end (the consumer completes the form and refills the
            # slot), then release the events client. A hard worker crash skips this —
            # the control plane's pipeline sweeper reconciles that case (room gone +
            # call still non-terminal → failed).
            if lifecycle is not None:
                await lifecycle.ended(now_ms=int(time.time() * 1000))
            if events_redis is not None:
                try:
                    await events_redis.aclose()
                except Exception:
                    logger.exception("failed to close events redis for %s", room_name)

        ctx.add_shutdown_callback(_on_shutdown)
    except BaseException:
        # Setup failed before the shutdown callback took ownership of the events
        # (and call-stream) clients — close them here so no connection outlives the
        # job. (Once _on_shutdown is registered, it owns the close; session.start
        # failures still run the registered shutdown callbacks.)
        if events_redis is not None:
            with contextlib.suppress(Exception):
                await events_redis.aclose()
        if call_stream_redis is not None:
            with contextlib.suppress(Exception):
                await call_stream_redis.aclose()
        raise
    # record=False disables livekit-agents session recording. Left unset it defers to the
    # server's enable_recording flag, which uploads a session report — including call AUDIO
    # and the transcript (PHI) — to the LiveKit Cloud observability endpoint at call end. That
    # crosses the trust boundary (audio off-box to LiveKit Cloud); our only sanctioned
    # observability is the self-hosted Langfuse/OTel pipeline (configure_observability), which
    # is independent of this. Disabling it also removes the recording byte-stream sends that
    # error with "engine is closed" as the room is torn down.
    await session.start(
        agent=build_agent(
            meta,
            boundary=boundary,
            session_id=session_id,
            instructions=instructions,
            greeting=greeting,
            # A successful IVR keypad press rides the live transcript as a dtmf turn
            # (evidence of the action); rooms with no stream enabled report nowhere.
            on_keypress=turn_emitter.on_keypress if turn_emitter is not None else None,
        ),
        room=ctx.room,
        room_input_options=build_room_input_options(speaker.identity if speaker else NOT_GIVEN),
        record=False,
    )


def build_worker_options() -> WorkerOptions:
    # agent_name must match the control plane's dispatch name. Configurable via
    # VERA_LIVEKIT_AGENT_NAME (default "vera-agent") so a laptop sharing a LiveKit
    # project with a deployed worker can isolate its own dispatch pool.
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=get_settings().livekit_agent_name,
    )


if __name__ == "__main__":
    cli.run_app(build_worker_options())
