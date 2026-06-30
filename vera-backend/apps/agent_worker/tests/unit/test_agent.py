"""Tests for the cascade agents — the chat persona (with PHI-wall node overrides) and
the IVR navigator (a plain agent, no phiwall), plus the metadata-driven selector."""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from livekit.agents import Agent
from livekit.agents.llm import FunctionTool

from agent_worker.agent import IvrNavigatorAgent, VeraAgent, build_agent
from agent_worker.prompt import build_instructions
from vera_core.phi import PassthroughPHIBoundary
from vera_core.schemas import PersonaTweak


async def _press(agent: IvrNavigatorAgent, digits: str) -> str:
    """Call the press_keypad tool. The @function_tool descriptor binds the method at
    runtime, but its type stub doesn't model that, so cast to the real call signature."""
    call = cast("Callable[[str], Awaitable[str]]", agent.press_keypad)
    return await call(digits)


class _FakeParticipant:
    """Records publish_dtmf calls; raise_on_publish makes each call fail like a real
    transport error (e.g. missing canPublishData grant)."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.sent: list[str] = []
        self._raise = raise_on_publish

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        if self._raise:
            # send_dtmf wraps any publish failure as DtmfTransportError.
            raise RuntimeError("publish failed")
        self.sent.append(digit)


def _job_ctx(participant: _FakeParticipant) -> object:
    """A stand-in JobContext whose room.local_participant is the fake participant."""
    return SimpleNamespace(room=SimpleNamespace(local_participant=participant))


def _navigator() -> IvrNavigatorAgent:
    """An IVR navigator with a (no-op) boundary + session_id so it can build its
    VeraAgent handoff target."""
    return IvrNavigatorAgent(PassthroughPHIBoundary(), "s1")


def test_vera_agent_has_end_call_tool_and_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    tool_names = [t.info.name for t in agent.tools if isinstance(t, FunctionTool)]
    assert tool_names == ["end_call"]
    assert "infertility" in agent.instructions.lower()
    # the chat persona greets on enter (overrides the base no-op)
    assert type(agent).on_enter is not Agent.on_enter
    # ...and carries the PHI-wall node overrides
    assert type(agent).stt_node is not Agent.stt_node
    assert type(agent).tts_node is not Agent.tts_node


def test_vera_agent_accepts_overlaid_instructions() -> None:
    instructions = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    agent = VeraAgent(
        boundary=PassthroughPHIBoundary(),
        session_id="s1",
        instructions=instructions,
        greeting="Hello there.",
    )
    assert "Confirm member ID twice." in agent.instructions


def test_ivr_navigator_agent_is_generic_and_silent_on_enter() -> None:
    agent = _navigator()
    # navigator carries the generic eligibility/benefits instructions
    assert "eligibility" in agent.instructions.lower()
    assert "infertility" not in agent.instructions.lower()
    # the navigator listens first: it does NOT greet, so on_enter stays the base no-op
    assert type(agent).on_enter is Agent.on_enter
    # it is a plain agent — NO PHI-wall node overrides
    assert type(agent).stt_node is Agent.stt_node
    assert type(agent).tts_node is Agent.tts_node
    # ...but it CAN press keypad digits (DTMF) and hand off to the verifier
    tool_names = {getattr(getattr(t, "info", None), "name", None) for t in agent.tools}
    assert "press_keypad" in tool_names
    assert "transfer_to_verification" in tool_names


@pytest.mark.asyncio
async def test_transfer_to_verification_hands_off_to_vera_agent() -> None:
    # When the navigator reaches a human, the handoff target is the VeraAgent verification
    # persona — and it carries the PHI-wall overrides the navigator lacks, so the wall turns
    # on for the rest of the call.
    agent = _navigator()
    call = cast("Callable[[], Awaitable[Agent]]", agent.transfer_to_verification)
    handoff = await call()
    assert isinstance(handoff, VeraAgent)
    assert type(handoff).on_enter is not Agent.on_enter  # greets the rep on enter
    assert type(handoff).stt_node is not Agent.stt_node
    assert type(handoff).tts_node is not Agent.tts_node


@pytest.mark.asyncio
async def test_press_keypad_sends_dtmf_without_echoing_digits() -> None:
    agent = _navigator()
    participant = _FakeParticipant()
    with patch("agent_worker.agent.get_job_context", return_value=_job_ctx(participant)):
        result = await _press(agent, "1")
    # the tone actually went to the participant...
    assert participant.sent == ["1"]
    # ...but the LLM-/trace-facing return never echoes the raw digit (PHI hygiene)
    assert "1" not in result


@pytest.mark.asyncio
async def test_press_keypad_surfaces_publish_failure_instead_of_swallowing() -> None:
    # Regression: a transport failure used to propagate and be silently swallowed by the
    # tool runner, looking exactly like "no DTMF". press_keypad must catch it and return.
    agent = _navigator()
    participant = _FakeParticipant(raise_on_publish=True)
    with patch("agent_worker.agent.get_job_context", return_value=_job_ctx(participant)):
        result = await _press(agent, "1")
    assert "could not send" in result.lower()


def test_build_agent_selects_by_ivr_navigation_flag() -> None:
    boundary = PassthroughPHIBoundary()
    nav = build_agent({"ivr_navigation": True}, boundary=boundary, session_id="s1")
    assert isinstance(nav, IvrNavigatorAgent)
    # absent or false → the default chat persona
    assert isinstance(build_agent({}, boundary=boundary, session_id="s1"), VeraAgent)
    assert isinstance(
        build_agent({"ivr_navigation": False}, boundary=boundary, session_id="s1"),
        VeraAgent,
    )
