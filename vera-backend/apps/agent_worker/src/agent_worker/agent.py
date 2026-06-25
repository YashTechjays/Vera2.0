"""Cascade agents.

VeraAgent: the infertility-verification chat persona; greets on enter and carries the
inert PHI-wall node overrides (stt_node redact FINAL+PREFLIGHT before the LLM; tts_node
hydrate the TTS-bound text only — both route through PHIBoundaryProtocol, today
PassthroughPHIBoundary/no-op).

IvrNavigatorAgent: the generic IVR navigator. The payer's IVR talks first, so it listens
on enter and responds prompt-by-prompt. It runs as a plain agent — no PHI-wall overrides.

build_agent() picks between them from the dispatch metadata.
"""

from collections.abc import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, stt

from agent_worker.prompt import (
    _IVR_NAVIGATOR_INSTRUCTIONS,
    build_instructions,
    resolve_greeting,
)
from agent_worker.seams import hydrate_stream, redact_event
from vera_core.phi import PHIBoundaryProtocol


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
            tools=[],
        )

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


class IvrNavigatorAgent(Agent):
    """Generic IVR navigator: the payer's IVR talks first, so the navigator stays silent
    on enter (default no-op on_enter) and responds prompt-by-prompt. Runs as a plain
    agent — no PHI-wall node overrides."""

    def __init__(self) -> None:
        super().__init__(instructions=_IVR_NAVIGATOR_INSTRUCTIONS, tools=[])


def build_agent(
    meta: dict[str, object],
    *,
    boundary: PHIBoundaryProtocol,
    session_id: str,
    instructions: str | None = None,
    greeting: str | None = None,
) -> Agent:
    """Pick the agent persona from dispatch metadata: the IVR navigator when
    `ivr_navigation` is set (a plain agent, no phiwall), otherwise the chat persona
    (with the PHI-wall overrides and any persona-tweak instructions/greeting)."""
    if meta.get("ivr_navigation"):
        return IvrNavigatorAgent()
    return VeraAgent(boundary, session_id, instructions=instructions, greeting=greeting)
