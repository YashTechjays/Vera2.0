"""VeraAgent — the cascade agent with inert PHI-wall node overrides.

stt_node: redact FINAL+PREFLIGHT before the LLM (preemptive-safe).
tts_node: hydrate the TTS-bound text only (audio stays the only PHI surface).
Both route through PHIBoundaryProtocol — today PassthroughPHIBoundary (no-op).
"""

from collections.abc import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, stt

from agent_worker.prompt import build_instructions, resolve_greeting
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
