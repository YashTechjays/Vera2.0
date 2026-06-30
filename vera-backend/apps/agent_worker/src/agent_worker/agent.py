"""Cascade agents.

VeraAgent: the infertility-verification chat persona; greets on enter and carries the
inert PHI-wall node overrides (stt_node redact FINAL+PREFLIGHT before the LLM; tts_node
hydrate the TTS-bound text only — both route through PHIBoundaryProtocol, today
PassthroughPHIBoundary/no-op).

IvrNavigatorAgent: the generic IVR navigator. The payer's IVR talks first, so it listens
on enter and responds prompt-by-prompt. It runs as a plain agent — no PHI-wall overrides.
Once it reaches a live human rep it hands off to VeraAgent via the `transfer_to_verification`
tool (a one-way swap; from then on the PHI-wall overrides apply).

build_agent() picks the initial persona from the dispatch metadata.
"""

import functools
import logging
from collections.abc import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, function_tool, get_job_context, llm, stt

from agent_worker.dtmf import DtmfTransportError, InvalidDtmfError, send_dtmf
from agent_worker.prompt import (
    build_instructions,
    build_ivr_instructions,
    resolve_greeting,
)
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


class IvrNavigatorAgent(Agent):
    """Generic IVR navigator: the payer's IVR talks first, so the navigator stays silent
    on enter (default no-op on_enter) and responds prompt-by-prompt, speaking menu choices
    and pressing keypad digits (DTMF) via the `press_keypad` tool. Runs as a plain agent —
    no PHI-wall node overrides.

    It holds a factory for its VeraAgent handoff target so that, once a live human rep
    answers, `transfer_to_verification` can hand off to a VeraAgent (which greets the rep
    and runs the benefits conversation)."""

    def __init__(
        self,
        boundary: PHIBoundaryProtocol,
        session_id: str,
        *,
        verification_instructions: str | None = None,
        verification_greeting: str | None = None,
    ) -> None:
        # The navigator runs plain (no PHI-wall overrides); it keeps only a factory for the
        # VeraAgent it hands off to once a human answers (see transfer_to_verification).
        self._make_verification_agent = functools.partial(
            VeraAgent,
            boundary,
            session_id,
            instructions=verification_instructions,
            greeting=verification_greeting,
        )
        super().__init__(instructions=build_ivr_instructions(), tools=[])

    @function_tool
    async def transfer_to_verification(self) -> Agent:
        """Hand the call to the verification agent. Call this ONLY when a live human
        representative has clearly greeted you — a personal name paired with an open request
        for your info (e.g. "Hi, this is Martha, who am I speaking with?"). This handoff is
        FINAL: do not call it for menus, recordings, a named virtual assistant (e.g. Avery),
        or a bare "hello". The verification agent greets the rep and takes over."""
        logger.info("handoff: IVR navigator -> verification agent")
        return self._make_verification_agent()

    @function_tool
    async def press_keypad(self, digits: str) -> str:
        """Press keypad digits on the phone menu (sends DTMF tones). Use ONLY for digits
        the IVR actually offered (e.g. "press 1 for eligibility"); never invent an account,
        member, or ID number. `digits` may contain 0-9, * or #."""
        # Log the count only — a DTMF sequence can be a member ID/NPI (PHI), and the
        # return string feeds the LLM/traces, so neither echoes the raw digits.
        count = len(digits.strip())
        try:
            await send_dtmf(get_job_context().room.local_participant, digits)
        except InvalidDtmfError as exc:
            return f"Could not send those keys: {exc}"
        except DtmfTransportError:
            # Surface the failure (the logged exception carries the real cause) instead of
            # letting the tool runner swallow it — the historical reason a failed press
            # looked like "nothing happened" on the line.
            logger.exception("press_keypad: DTMF publish failed (%d tone(s))", count)
            return "Could not send the keypad tones over the call; continue without pressing."
        logger.info("press_keypad: sent %d DTMF tone(s)", count)
        return "Sent the keypad tones."


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
        return IvrNavigatorAgent(
            boundary,
            session_id,
            verification_instructions=instructions,
            verification_greeting=greeting,
        )
    return VeraAgent(boundary, session_id, instructions=instructions, greeting=greeting)
