"""Cascade agents.

VeraAgent: the infertility-verification chat persona; greets on enter and carries the
inert PHI-wall node overrides (stt_node redact FINAL+PREFLIGHT before the LLM; tts_node
hydrate the TTS-bound text only — both route through PHIBoundaryProtocol, today
PassthroughPHIBoundary/no-op).

The generic IVR navigator (IvrNavigatorAgent) lives in `ivr_agent.py`; once it reaches a
live human rep it hands off to VeraAgent (a one-way swap; from then on the PHI-wall overrides
apply). build_agent() picks the initial persona from the dispatch metadata.
"""

import logging
from collections.abc import AsyncIterable

from livekit import rtc
from livekit.agents import (
    Agent,
    ModelSettings,
    llm,
    stt,
)

from agent_worker.ivr_agent import IvrNavigatorAgent
from agent_worker.ivr_prompt import parse_ivr_playbook
from agent_worker.prompt import build_instructions, resolve_greeting
from agent_worker.seams import hydrate_stream, redact_event
from vera_core.phi import PHIBoundaryProtocol

logger = logging.getLogger("agent_worker")


class VeraAgent(Agent):
    def __init__(
        self,
        boundary: PHIBoundaryProtocol,
        session_id: str,
        *,
        instructions: str | None = None,
        greeting: str | None = None,
    ) -> None:
        self._boundary = boundary
        self._session_id = session_id
        self._greeting = greeting if greeting is not None else resolve_greeting()
        super().__init__(
            instructions=instructions if instructions is not None else build_instructions(),
        )

    @llm.function_tool(
        name="end_call",
        description=(
            "End the phone call. Call this tool IMMEDIATELY after you say your "
            "closing line (e.g. 'thanks so much for your help, have a good one'). "
            "This hangs up the call for all participants."
        ),
    )
    async def _end_call(self) -> str:
        """Drain pending TTS audio then shut down the session."""
        self.session.shutdown(drain=True)
        return "Call ended."

    async def on_enter(self) -> None:
        self.session.say(self._greeting)

    async def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ) -> AsyncIterable[stt.SpeechEvent | str]:
        async for ev in Agent.default.stt_node(self, audio, model_settings):
            if isinstance(ev, stt.SpeechEvent):
                ev = await redact_event(self._boundary, self._session_id, ev)
            yield ev

    def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Inert seam: pass-through today. Future: tap tokenized assistant
        # segments here for the live-transcript stream. Kept deliberately so
        # that integration is a one-line change inside this method.
        return Agent.default.transcription_node(self, text, model_settings)

    def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterable[rtc.AudioFrame]:
        hydrated = hydrate_stream(self._boundary, self._session_id, text)
        return Agent.default.tts_node(self, hydrated, model_settings)


def build_agent(
    meta: dict[str, object],
    *,
    boundary: PHIBoundaryProtocol,
    session_id: str,
    instructions: str | None = None,
    greeting: str | None = None,
) -> Agent:
    """Pick the agent persona from dispatch metadata: the IVR navigator when `enable_ivr_navigation`
    is set or a per-provider `ivr_playbook` overlay is present (a plain agent, no phiwall, the
    playbook specializing its prompt), otherwise the chat persona (with the PHI-wall overrides
    and any persona-tweak instructions/greeting)."""
    playbook = parse_ivr_playbook(meta.get("ivr_playbook"))
    if meta.get("enable_ivr_navigation") or playbook is not None:
        return IvrNavigatorAgent(
            boundary,
            session_id,
            playbook=playbook,
            verification_instructions=instructions,
            verification_greeting=greeting,
        )
    return VeraAgent(boundary, session_id, instructions=instructions, greeting=greeting)
