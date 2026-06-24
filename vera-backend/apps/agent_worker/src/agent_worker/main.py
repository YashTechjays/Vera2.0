"""Vera agent worker — Deepgram->Gemini->Cartesia cascade over LiveKit.

Explicit dispatch only (agent_name set): the control plane dispatches this worker
into a room named by vera_core.observability.correlation.room_name_for_call. The
room name IS the session id and the Langfuse correlation key.
"""

import asyncio
import json
import logging

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

from agent_worker.agent import VeraAgent
from agent_worker.cascade import _build_vad, build_session
from agent_worker.transcript_publisher import attach_transcript_publisher
from vera_core.config.settings import get_settings
from vera_core.observability.correlation import (
    call_trace_attributes,
    is_listen_only_identity,
    parse_room_name,
)
from vera_core.observability.otel import configure_observability
from vera_core.phi import build_phi_boundary
from vera_core.redis import create_redis
from vera_core.transcript import RedisTranscriptStore, TranscriptService

logger = logging.getLogger("agent_worker")

AGENT_NAME = "vera-agent"

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
    (ready as soon as it joins) or a SIP callee that has answered. The listen-only
    monitor never qualifies, and a SIP callee that is merely ringing does not yet."""
    if is_listen_only_identity(participant.identity):
        return False
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return participant.attributes.get(_SIP_CALL_STATUS_ATTR) == _SIP_CALL_STATUS_ACTIVE
    return True


async def wait_for_speaker(
    ctx: JobContext, timeout_s: float = _SPEAKER_TIMEOUT_S
) -> rtc.RemoteParticipant | None:
    """Block until a ready, non-monitor remote participant is present, or the timeout.

    Returns that participant once the browser caller has joined or the SIP callee has
    answered, or None on timeout. The caller pins the agent's audio input to the
    returned participant so it listens to the speaker and not the listen-only monitor.
    """
    for p in ctx.room.remote_participants.values():
        if _is_ready_speaker(p):
            return p

    loop = asyncio.get_running_loop()
    arrived: asyncio.Future[rtc.RemoteParticipant] = loop.create_future()

    def _resolve(participant: rtc.Participant) -> None:
        if arrived.done() or not _is_ready_speaker(participant):
            return
        # Look up the RemoteParticipant (filters out the agent's own local participant,
        # which never appears in remote_participants).
        remote = ctx.room.remote_participants.get(participant.identity)
        if remote is not None:
            arrived.set_result(remote)

    def _on_connected(participant: rtc.RemoteParticipant) -> None:
        _resolve(participant)

    def _on_attributes_changed(_changed: dict[str, str], participant: rtc.Participant) -> None:
        # The SIP callee's ring → active transition arrives here, not as a new join.
        _resolve(participant)

    ctx.room.on("participant_connected", _on_connected)
    ctx.room.on("participant_attributes_changed", _on_attributes_changed)
    try:
        async with asyncio.timeout(timeout_s):
            return await arrived
    except TimeoutError:
        return None
    finally:
        ctx.room.off("participant_connected", _on_connected)
        ctx.room.off("participant_attributes_changed", _on_attributes_changed)


def session_id_for(room_name: str) -> str:
    """The room name is the session id (correlation key shared with the control plane)."""
    return room_name


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
        speaker = await wait_for_speaker(ctx)
        if speaker is None:
            logger.warning("no speaker joined room %s within timeout — not starting", room_name)
            return

    boundary = build_phi_boundary(settings)
    await boundary.open_session(session_id)

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
        attach_transcript_publisher(session, transcript_service, room_name)

    async def _on_shutdown() -> None:
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
        await boundary.close_session(session_id)

    ctx.add_shutdown_callback(_on_shutdown)
    # Pin the agent's audio input to the speaker. Otherwise RoomIO links to the first
    # eligible participant — in outbound mode that can be the listen-only monitor
    # (which publishes no audio), leaving the agent deaf to the SIP callee. NOT_GIVEN
    # keeps the default auto-link for the /calls path (no speaker pinned).
    room_input_options: RoomInputOptions | NotGiven = (
        RoomInputOptions(participant_identity=speaker.identity)
        if speaker is not None
        else NOT_GIVEN
    )
    await session.start(
        agent=VeraAgent(boundary=boundary, session_id=session_id),
        room=ctx.room,
        room_input_options=room_input_options,
    )


def build_worker_options() -> WorkerOptions:
    return WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME)


if __name__ == "__main__":
    cli.run_app(build_worker_options())
