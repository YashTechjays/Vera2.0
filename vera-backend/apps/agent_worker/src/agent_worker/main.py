"""Vera agent worker — Deepgram->Gemini->Cartesia cascade over LiveKit.

Explicit dispatch only (agent_name set): the control plane dispatches this worker
into a room named by vera_core.observability.correlation.room_name_for_call. The
room name IS the session id and the Langfuse correlation key.
"""

import logging

from livekit.agents import JobContext, JobProcess, WorkerOptions, cli
from opentelemetry import trace

from agent_worker.agent import VeraAgent
from agent_worker.cascade import _build_vad, build_session
from vera_core.config.settings import get_settings
from vera_core.observability.correlation import call_trace_attributes, parse_room_name
from vera_core.observability.otel import configure_observability
from vera_core.phi import build_phi_boundary

logger = logging.getLogger("agent_worker")

AGENT_NAME = "vera-agent"


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

    boundary = build_phi_boundary(settings)
    await boundary.open_session(session_id)

    session = build_session(vad=ctx.proc.userdata.get("vad"))

    async def _on_shutdown() -> None:
        await boundary.close_session(session_id)

    ctx.add_shutdown_callback(_on_shutdown)
    await session.start(agent=VeraAgent(boundary=boundary, session_id=session_id), room=ctx.room)


def build_worker_options() -> WorkerOptions:
    return WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME)


if __name__ == "__main__":
    cli.run_app(build_worker_options())
