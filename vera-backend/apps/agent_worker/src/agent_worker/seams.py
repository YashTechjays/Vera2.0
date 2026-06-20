"""PHI-wall seam helpers, isolated from LiveKit node plumbing for testability.

Today the boundary is PassthroughPHIBoundary (identity). The placements match the
POC's validated, preemptive-generation-safe design: redact FINAL+PREFLIGHT (never
on_user_turn_completed, which misses PREFLIGHT); hydrate the TTS-bound text only.
"""

from collections.abc import AsyncIterable, AsyncIterator

from livekit.agents import stt

from vera_core.phi import PHIBoundaryProtocol

_REDACT_TYPES = {
    stt.SpeechEventType.FINAL_TRANSCRIPT,
    stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
}


async def redact_event(
    boundary: PHIBoundaryProtocol, session_id: str, ev: stt.SpeechEvent
) -> stt.SpeechEvent:
    if ev.type in _REDACT_TYPES and ev.alternatives:
        alt = ev.alternatives[0]
        alt.text = await boundary.redact(session_id, alt.text)
    return ev


async def hydrate_stream(
    boundary: PHIBoundaryProtocol, session_id: str, text: AsyncIterable[str]
) -> AsyncIterator[str]:
    async for chunk in text:
        yield await boundary.hydrate_for_speech(session_id, chunk)
