"""Vera agent worker — LiveKit Agents skeleton.

The worker REGISTERS itself with LiveKit under AGENT_NAME and waits. Setting
agent_name in WorkerOptions disables automatic dispatch: the worker joins a
room only when something (later: the control plane creating an outbound call)
requests this agent explicitly via RoomAgentDispatch. The control plane never
"starts" the worker process.

Identity: LiveKit credentials come from LIVEKIT_URL / LIVEKIT_API_KEY /
LIVEKIT_API_SECRET. On GKE the worker runs as a GCP service principal
(workload identity) and pulls these from Secret Manager — it has no GCIP user
and never passes through the human RBAC chain.

This session's scope is register + join + echo. The voice pipeline attaches
later:
  TODO(vera-2.x): Deepgram/Gemini/Cartesia cascade (STT -> LLM -> TTS) wrapped
      in vera_core.phi.PHIBoundary — redact() before the LLM,
      SpeechStreamHydrator before TTS, hydrate_raw() before payer connectors.
  TODO(vera-2.x): Twilio SIP outbound dispatch (control plane creates the SIP
      participant; this worker is dispatched into the same room).
  TODO(vera-2.x): STT Registry + Context Bridge.
  TODO(vera-2.x): supervisor oversight (coaching / whisper / takeover).
  TODO(vera-2.x): recording-to-GCS, data extraction into form templates.
"""

import asyncio
import logging

from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli

logger = logging.getLogger("vera.agent_worker")

AGENT_NAME = "vera-agent"
ECHO_TOPIC = "vera.echo"


async def entrypoint(ctx: JobContext) -> None:
    """Dispatched to a room: connect and echo any data packet back."""
    await ctx.connect()
    logger.info("joined room %s as %s", ctx.room.name, AGENT_NAME)

    echo_tasks: set[asyncio.Task[None]] = set()

    @ctx.room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        async def echo() -> None:
            await ctx.room.local_participant.publish_data(packet.data, topic=ECHO_TOPIC)

        task = asyncio.create_task(echo())
        echo_tasks.add(task)
        task.add_done_callback(echo_tasks.discard)


def build_worker_options() -> WorkerOptions:
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        # Explicit dispatch: with agent_name set, LiveKit only sends this
        # worker jobs that name it in a RoomAgentDispatch request.
        agent_name=AGENT_NAME,
    )


if __name__ == "__main__":
    cli.run_app(build_worker_options())
