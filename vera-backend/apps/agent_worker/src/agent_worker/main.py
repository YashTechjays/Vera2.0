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
from dataclasses import dataclass, replace

from google.genai.types import ThinkingConfig
from livekit import rtc
from livekit.agents import (
    NOT_GIVEN,
    Agent,
    JobContext,
    JobProcess,
    NotGiven,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram
from opentelemetry import trace
from redis.asyncio import Redis

from agent_worker.agent import build_agent
from agent_worker.cascade import _build_vad, build_session
from agent_worker.coaching import CoachingListener
from agent_worker.health_observer import CallHealthObserver, build_health_observer
from agent_worker.intervention import AgentTakeoverController, intervener_present
from agent_worker.observer import ObserverManager, ResilientAnswerExtractor
from agent_worker.plan_runtime import PlanRunController
from agent_worker.prompt import parse_persona_tweak
from agent_worker.takeover_transcript import TakeoverTranscriber
from agent_worker.transcript_publisher import (
    FanOutTurnPublisher,
    ReorderingEmitter,
    TurnPublisher,
    attach_transcript_publisher,
)
from vera_core.call_stream import CallStreamService, RedisCallStreamStore
from vera_core.config import EnvSecretProvider
from vera_core.config.settings import get_settings
from vera_core.events import (
    CallAnsweredEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
    WorkerEventBus,
)
from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM
from vera_core.observability.correlation import (
    PARTICIPANT_MODE_ATTR,
    call_trace_attributes,
    is_observer_identity,
    parse_room_name,
)
from vera_core.observability.otel import configure_observability
from vera_core.plan_store import (
    CallPlanService,
    PlanRunStateService,
    RedisCallPlanStore,
    RedisPlanRunStateStore,
)
from vera_core.redis import create_redis

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
    """Collapse the enabled stream sinks (the call stream today) behind one
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

    # Declared here (not inside try) so the except below can always safely reference them,
    # even if setup fails before their blocks run.
    call_stream_redis: Redis | None = None
    plan_redis: Redis | None = None
    observer_redis: Redis | None = None
    coaching_redis: Redis | None = None
    observer_manager: ObserverManager | None = None
    extract_llm: ResilientLLM | None = None
    health_observer: CallHealthObserver | None = None

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

        # Tenant persona overlay arrives as opaque dispatch metadata (set by the control
        # plane). Fail-safe: bad/missing metadata yields the no-op tweak.
        tweak = parse_persona_tweak(ctx.job.metadata if ctx.job is not None else None)

        # Compiled Call Plan (dispatcher opt-in via use_call_plan): the control plane
        # staged it in Redis at dispatch. PLAN-ONLY — the compiled plan is the sole
        # verification prompt source. If the plan can't be loaded/built, the call
        # fails fast (hangs up) instead of running any generic script; it never runs
        # a verification without a plan.
        plan_service: CallPlanService | None = None
        run_state: PlanRunStateService | None = None
        controller: PlanRunController | None = None
        if meta.get("use_call_plan"):
            plan_redis = create_redis(settings.redis_url)
            plan_service = CallPlanService(
                RedisCallPlanStore(plan_redis, ttl_seconds=settings.call_plan_ttl_seconds)
            )
            plan = await plan_service.get(room_name)
            if plan is None:
                logger.warning("use_call_plan set but no plan for %s — failing fast", room_name)
            else:
                run_state = PlanRunStateService(
                    RedisPlanRunStateStore(plan_redis, ttl_seconds=settings.call_plan_ttl_seconds)
                )
                # A RETRY call carries no retry framing: the control plane stages a
                # plan already narrowed to the still-missing fields (focus_call_plan),
                # so the agent simply runs the plan it's given and is never told a
                # prior call happened — nothing leaks to the payer rep.
                try:
                    controller = PlanRunController(
                        plan,
                        room_name=room_name,
                        run_state=run_state,
                        # An explicit tenant greeting overrides the plan's first-task
                        # intro; extra_instructions overlay every plan agent.
                        greeting=tweak.greeting,
                        extra_instructions=tweak.extra_instructions or None,
                    )
                except Exception:
                    logger.exception(
                        "call plan for %s failed to build a runtime — failing fast", room_name
                    )
        else:
            logger.info("no use_call_plan for %s — voice-lab preview (plan-less)", room_name)

        session = build_session(
            vad=ctx.proc.userdata.get("vad"),
            key_terms=controller.plan.stt_key_terms if controller is not None else None,
        )

        # THE call event stream: transcript turns + call_status frames, feeding the
        # /calls/{id}/events SSE, the Voice-Lab SSE, the transcript finalizer, and the
        # Observer. Written for the SSE opt-ins (publish_events / publish_transcript) AND
        # unconditionally for every plan-backed call — the Observer tails it to extract
        # answers, so a plan call must always populate vera:call-events:{room}, opt-in or not.
        call_stream: CallStreamService | None = None
        if meta.get("publish_events") or meta.get("publish_transcript") or controller is not None:
            call_stream_redis = create_redis(settings.redis_url)
            call_stream = CallStreamService(
                RedisCallStreamStore(
                    call_stream_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )

        # The Observer TAILS the call-event stream (with its OWN read client, so tearing
        # down the write path at shutdown can't kill the reader) to extract answers from the
        # live call. It routes turns per active task and does nothing during IVR/wrap-up, so
        # it is inert until the conversation path begins. Started for a plan-backed canonical
        # room; the controller gets the session so a rule fire can interrupt/redirect the bot.
        if controller is not None and run_state is not None and bus is not None:
            controller.attach_session(session)
            observer_redis = create_redis(settings.redis_url)
            # Out-of-pipeline extraction chain (Gemini primary → OpenAI fallback), the
            # mandated seam for non-cascade LLM calls. thinking_budget=0 is pinned on a
            # Gemini primary to keep extraction low-latency (ignored by the OpenAI fallback);
            # a missing OPENAI_API_KEY safely degrades the chain to Gemini-only.
            extract_primary = LLMSpec.parse(settings.observer_extract_primary_model)
            if extract_primary.provider == "google":
                extract_primary = replace(
                    extract_primary, extra={"thinking_config": ThinkingConfig(thinking_budget=0)}
                )
            extract_llm = ResilientLLM(
                extract_primary,
                [LLMSpec.parse(s) for s in settings.observer_extract_fallback_models],
                options=FallbackOptions(
                    attempt_timeout=settings.observer_extract_attempt_timeout_seconds
                ),
                secrets=EnvSecretProvider(),
            )
            observer_manager = ObserverManager(
                controller.plan,
                controller=controller,
                run_state=run_state,
                bus=bus,
                extractor=ResilientAnswerExtractor(extract_llm),
                transcript=RedisCallStreamStore(
                    observer_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                ),
                room_name=room_name,
            )
            observer_manager.start()

        # Call-health observer (real /calls flow only: needs both the per-call
        # event stream and the worker-event bus). An extra fan-out sink — it sees
        # the same ordered turns as every other stream and never blocks them.
        if call_stream is not None and bus is not None:
            try:
                health_observer = build_health_observer(
                    session,
                    room_name=room_name,
                    settings=settings,
                    call_stream=call_stream,
                    bus=bus,
                )
            except Exception as exc:
                # A static misconfiguration (bad model selector, etc.) must degrade to
                # "no observer", never break call setup. Type name only (PHI rule).
                logger.warning(
                    "build_health_observer failed for %s (%s); continuing without an observer",
                    room_name,
                    type(exc).__name__,
                )

        # One reordering emitter driving the stream — the barge-in reorder state machine
        # lives once per job (see transcript_publisher). Kept as a fan-out seam so a second
        # sink can be added without reshaping the emitter wiring.
        # (The Observer is NOT a sink — it reads the stream this sink writes.)
        sinks: list[TurnPublisher] = [
            svc for svc in (call_stream, health_observer) if svc is not None
        ]
        turn_sink = _fan_out_sink(sinks)
        turn_emitter: ReorderingEmitter | None = None
        if turn_sink is not None:
            turn_emitter = attach_transcript_publisher(session, turn_sink, room_name)

        # After a supervisor takes over, the bot's STT is muted; a dedicated per-track
        # STT transcribes the caller + supervisor so the live transcript keeps going.
        if call_stream is not None and speaker is not None:
            # the callee already answered during wait_for_speaker
            await call_stream.publish_status(room_name, "active", ts=int(time.time() * 1000))

        takeover_transcriber: TakeoverTranscriber | None = None
        if turn_sink is not None and speaker is not None:
            takeover_transcriber = TakeoverTranscriber(
                ctx.room,
                turn_sink,
                room_name,
                stt_factory=lambda: deepgram.STT(model="nova-3"),
                callee_identity=speaker.identity,
            )

        # Coaching listener started once the session is running (below, after
        # session.start) — declared here so _on_shutdown can always reference it.
        coaching_task: asyncio.Task[None] | None = None

        async def _flush_turn_emitter() -> None:
            # Order is load-bearing: flush held turns BEFORE any service's end(). end()
            # appends the sentinel that stops readers, so a turn flushed after it would be
            # stranded behind the sentinel. One flush covers every fanned-out sink.
            if turn_emitter is not None:
                try:
                    await turn_emitter.aclose()
                except Exception:  # best-effort; never block shutdown
                    logger.exception("failed to flush turn emitter for %s", room_name)

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

        async def _end_observer() -> None:
            # Runs AFTER _end_call_stream (so the tail drains through the end sentinel it
            # writes — without that the drain would burn its full timeout and lose trailing
            # turns) and BEFORE the plan-run state is cleared (its final drain writes
            # record_answer). Safe against _end_call_stream closing its Redis client: the
            # Observer reads on its own.
            if observer_manager is not None:
                try:
                    await observer_manager.aclose()
                except Exception:  # best-effort; never block shutdown
                    logger.exception("failed to close observer for %s", room_name)
            if observer_redis is not None:
                try:
                    await observer_redis.aclose()
                except Exception:
                    logger.exception("failed to close observer redis for %s", room_name)
            if extract_llm is not None:
                try:
                    await extract_llm.aclose()  # close the provider chain's aiohttp sessions
                except Exception:
                    logger.exception("failed to close extract llm for %s", room_name)

        async def _end_plan_run() -> None:
            # Best-effort cleanup of the plan blob + run state; the rolling TTL is
            # the backstop when this is skipped (hard crash) or Redis is down.
            if controller is not None:
                with contextlib.suppress(Exception):
                    await controller.drain_cursor_writes()
            if run_state is not None:
                try:
                    await run_state.clear(room_name)
                except Exception:
                    logger.exception("failed to clear plan run state for %s", room_name)
            if plan_service is not None:
                try:
                    await plan_service.clear(room_name)
                except Exception:
                    logger.exception("failed to clear call plan for %s", room_name)
            if plan_redis is not None:
                try:
                    await plan_redis.aclose()
                except Exception:
                    logger.exception("failed to close plan redis for %s", room_name)

        async def _on_shutdown() -> None:
            # Sequential, spec-pinned order: flush the shared emitter once, then the takeover
            # transcriber (so its turns land before any sentinel), then call-stream teardown
            # (which WRITES the end sentinel), then the Observer — its tail drains through
            # that sentinel, so it must come after — then the plan run's Redis keys. Each step
            # inside the helpers is best-effort (own try/except), so a failure never skips the
            # rest.
            await _flush_turn_emitter()
            if takeover_transcriber is not None:
                try:
                    await takeover_transcriber.aclose()  # before end(): flush its turns first
                except Exception:
                    logger.exception("failed to close takeover transcriber for %s", room_name)
            if coaching_task is not None:
                coaching_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await coaching_task
            if coaching_redis is not None:
                try:
                    await coaching_redis.aclose()
                except Exception:
                    logger.exception("failed to close coaching redis for %s", room_name)
            if health_observer is not None:
                try:
                    await health_observer.aclose()  # before the call stream ends
                except Exception:
                    logger.exception("failed to close health observer for %s", room_name)
            await _end_call_stream()
            await _end_observer()
            await _end_plan_run()

            # Last: signal the terminal event. Normally call.ended (the consumer
            # completes the form and refills the slot). But when the dispatcher
            # staged a plan (use_call_plan) yet the worker couldn't build one, the
            # call failed fast without a session — that's an infra fault (plan
            # missing from Redis / build failure), not a completed verification.
            # Emit call.failed instead so the control plane RE-DISPATCHES it
            # (re-staging the plan, which self-heals) rather than banking a
            # completed-with-no-answers form.
            # A hard worker crash skips this — the pipeline sweeper reconciles that.
            now_ms = int(time.time() * 1000)
            if meta.get("use_call_plan") and controller is None and bus is not None:
                logger.warning("no usable plan for %s — emitting call.failed", room_name)
                await _emit_call_failed(bus, room_name, CallFailureReason.FAILED, now_ms=now_ms)
            elif lifecycle is not None:
                await lifecycle.ended(now_ms=now_ms)
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
        if plan_redis is not None:
            with contextlib.suppress(Exception):
                await plan_redis.aclose()
        if observer_manager is not None:
            # Cancel/drain the tail task BEFORE closing the client it reads from.
            with contextlib.suppress(Exception):
                await observer_manager.aclose()
        if observer_redis is not None:
            with contextlib.suppress(Exception):
                await observer_redis.aclose()
        if extract_llm is not None:
            with contextlib.suppress(Exception):
                await extract_llm.aclose()
        if health_observer is not None:
            with contextlib.suppress(Exception):
                await health_observer.aclose()
        raise
    # record=False disables livekit-agents session recording. Left unset it defers to the
    # server's enable_recording flag, which uploads a session report — including call AUDIO
    # and the transcript (PHI) — to the LiveKit Cloud observability endpoint at call end. That
    # crosses the trust boundary (audio off-box to LiveKit Cloud); our only sanctioned
    # observability is the self-hosted Langfuse/OTel pipeline (configure_observability), which
    # is independent of this. Disabling it also removes the recording byte-stream sends that
    # error with "engine is closed" as the room is torn down.
    # A REAL call that expected a plan (use_call_plan) but has no controller FAILS
    # FAST — real calls never dispatch plan-less (the dispatcher skips a form whose
    # plan can't be prepared), so this is a Redis-loss race / build failure. Return
    # without starting a session; the shutdown callback emits call.failed so the
    # control plane re-dispatches it (self-heal).
    if controller is None and meta.get("use_call_plan"):
        logger.warning("no usable plan for %s — failing fast without a session", room_name)
        return
    # build_agent picks the agent: the plan chain when a controller is present, or a
    # conversational VoiceLabAgent for a Voice Lab preview (no plan) — see its docstring.
    agent: Agent = build_agent(
        meta,
        controller=controller,
        tweak=tweak,
        # A successful IVR keypad press rides the live transcript as a dtmf turn
        # (evidence of the action); rooms with no stream enabled report nowhere.
        on_keypress=turn_emitter.on_keypress if turn_emitter is not None else None,
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=build_room_input_options(speaker.identity if speaker else NOT_GIVEN),
        record=False,
    )

    # Coaching mode: fold a supervisor's coaching/whisper message into Vera's
    # context on her next turn. Same real-call gate as the takeover transcriber
    # (publish_events) — coaching only makes sense where the call-event stream
    # (and thus the control plane's /coach endpoint) is actually wired up.
    if call_stream is not None:

        def _log_coaching_exit(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                logger.error(
                    "coaching listener exited unexpectedly for %s",
                    room_name,
                    exc_info=task.exception(),
                )

        # Own read client, like the Observer — a blocking XREAD would otherwise
        # pin a pooled connection shared with the turn publishes.
        coaching_redis = create_redis(settings.redis_url)
        coaching_stream = CallStreamService(
            RedisCallStreamStore(
                coaching_redis,
                ttl_seconds=settings.transcript_stream_ttl_seconds,
                end_grace_seconds=settings.transcript_end_grace_seconds,
            )
        )
        coaching_listener = CoachingListener(session, coaching_stream, room_name)
        coaching_task = asyncio.create_task(coaching_listener.run())
        coaching_task.add_done_callback(_log_coaching_exit)

    # Supervisor takeover: the first time a participant carries the intervene mode
    # attribute, silence the agent for the rest of the call (one-way, never resumes)
    # and start transcribing the human conversation.
    takeover_ctl = AgentTakeoverController(
        session,
        on_engage=takeover_transcriber.start if takeover_transcriber is not None else None,
    )

    def _check_takeover(*_args: object) -> None:
        if intervener_present(
            p.attributes.get(PARTICIPANT_MODE_ATTR) for p in ctx.room.remote_participants.values()
        ):
            takeover_ctl.engage()

    ctx.room.on("participant_connected", _check_takeover)
    ctx.room.on("participant_attributes_changed", _check_takeover)
    _check_takeover()  # an intervener may already be present


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
