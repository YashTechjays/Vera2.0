"""Tests for the cascade agents — the chat persona (with PHI-wall node overrides) and
the IVR navigator (a plain agent, no phiwall), plus the metadata-driven selector."""

from livekit.agents import Agent

from agent_worker.agent import IvrNavigatorAgent, VeraAgent, build_agent
from agent_worker.prompt import build_instructions
from vera_core.phi import PassthroughPHIBoundary
from vera_core.schemas import PersonaTweak


def test_vera_agent_is_chat_only_with_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    assert list(agent.tools) == []
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
    agent = IvrNavigatorAgent()
    # navigator carries the generic eligibility/benefits instructions, no tools
    assert list(agent.tools) == []
    assert "eligibility" in agent.instructions.lower()
    assert "infertility" not in agent.instructions.lower()
    # the navigator listens first: it does NOT greet, so on_enter stays the base no-op
    assert type(agent).on_enter is Agent.on_enter
    # it is a plain agent — NO PHI-wall node overrides
    assert type(agent).stt_node is Agent.stt_node
    assert type(agent).tts_node is Agent.tts_node


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
