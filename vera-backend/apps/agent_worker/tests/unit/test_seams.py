from typing import Any

import pytest
from livekit.agents import stt

from agent_worker.seams import hydrate_stream, redact_event
from vera_core.phi import PassthroughPHIBoundary


class SpyBoundary(PassthroughPHIBoundary):
    def __init__(self) -> None:
        self.redacted: list[str] = []

    async def redact(self, session_id: str, text: str) -> str:
        self.redacted.append(text)
        return f"[redacted:{text}]"


def _event(kind: stt.SpeechEventType, text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=kind,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


@pytest.mark.asyncio
async def test_redact_event_rewrites_final_and_preflight() -> None:
    spy = SpyBoundary()
    final = await redact_event(spy, "s1", _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Jane Doe"))
    assert final.alternatives[0].text == "[redacted:Jane Doe]"
    assert spy.redacted == ["Jane Doe"]


@pytest.mark.asyncio
async def test_redact_event_skips_interim() -> None:
    spy = SpyBoundary()
    interim = await redact_event(spy, "s1", _event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "Ja"))
    assert interim.alternatives[0].text == "Ja"  # untouched
    assert spy.redacted == []


@pytest.mark.asyncio
async def test_hydrate_stream_passthrough_is_identity() -> None:
    async def gen() -> Any:
        for c in ["Hello ", "[[NAME_1]]"]:
            yield c

    out = [c async for c in hydrate_stream(PassthroughPHIBoundary(), "s1", gen())]
    assert "".join(out) == "Hello [[NAME_1]]"
