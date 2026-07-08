"""Tests for the cascade agents — the chat persona (with PHI-wall node overrides) and
the IVR navigator (a plain agent, no phiwall), plus the metadata-driven selector."""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from livekit.agents import Agent, StopResponse
from livekit.agents.llm import FunctionTool
from livekit.agents.utils import is_given

from agent_worker.agent import VeraAgent, build_agent
from agent_worker.ivr_agent import (
    _IVR_MAX_TURNS,
    IvrNavigatorAgent,
    _strip_silence_token,
    ivr_turn_handling,
)
from agent_worker.ivr_prompt import SILENCE_TOKEN
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
    # NO PHI-wall on the inbound path: stt_node stays the base default (no redaction override)
    assert type(agent).stt_node is Agent.stt_node
    # ...but tts/transcription ARE overridden — only to strip the silence sentinel (not to
    # hydrate PHI, which the navigator has none of), so a "stay silent" turn makes no sound
    assert type(agent).tts_node is not Agent.tts_node
    assert type(agent).transcription_node is not Agent.transcription_node
    # ...but it CAN press keypad digits (DTMF), hand off to the verifier, and give up
    tool_names = {getattr(getattr(t, "info", None), "name", None) for t in agent.tools}
    assert "press_keypad" in tool_names
    assert "transfer_to_verification" in tool_names
    assert "give_up" in tool_names


@pytest.mark.asyncio
async def test_give_up_ends_the_call() -> None:
    # give_up is the navigator's bail-out for an unresolvable IVR loop — it hangs up cleanly.
    agent = _navigator()
    mock_session = MagicMock()
    give_up_tool = next(
        t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == "give_up"
    )
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        result = await give_up_tool()
    assert result == "Ending the call."  # the tool signals the model the call is over
    mock_session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.asyncio
async def test_turn_cap_grants_one_grace_turn_before_ending() -> None:
    # The cap is deterministic, but the turn that first trips it gets a grace turn: if a live
    # rep answers exactly then, the model still gets to generate and hand off instead of being
    # hung up on. So the first over-cap turn neither shuts down nor raises StopResponse.
    agent = _navigator()
    agent._turns = _IVR_MAX_TURNS  # the next completed turn trips the cap
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await agent.on_user_turn_completed(MagicMock(), MagicMock())  # grace turn
    mock_session.shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_turn_cap_backstop_ends_the_call_after_the_grace_turn() -> None:
    # If the IVR is still looping the turn after the grace turn (no human reached), the
    # deterministic backstop hangs up and suppresses the reply via StopResponse.
    agent = _navigator()
    agent._turns = _IVR_MAX_TURNS
    agent._final_turn_used = True  # grace turn already spent
    mock_session = MagicMock()
    with (
        patch.object(type(agent), "session", new=property(lambda self: mock_session)),
        pytest.raises(StopResponse),
    ):
        await agent.on_user_turn_completed(MagicMock(), MagicMock())
    mock_session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.asyncio
async def test_under_the_turn_cap_does_not_end_the_call() -> None:
    agent = _navigator()
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        # one normal turn well under the cap — no shutdown, no StopResponse
        await agent.on_user_turn_completed(MagicMock(), MagicMock())
    mock_session.shutdown.assert_not_called()


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


def test_ivr_turn_handling_is_patient() -> None:
    # The IVR phase uses vad end-of-turn + a moderately patient delay; barge-in stays on (so real
    # IVR prompts still interrupt) but preemptive is off + min_words raised to stop the SIP
    # self-echo from clipping the start of answers.
    th = ivr_turn_handling()
    assert th["turn_detection"] == "vad"  # not the human-trained EnglishModel
    # patient, but not so long that answers land on the next prompt (key tunable)
    assert 0.6 <= th["endpointing"]["min_delay"] <= 1.5
    # preemptive OFF: keeps a tiny output buffer so a false-interruption pause can't discard the
    # start of the utterance (self-echo clip: "Medical" → "dical").
    assert th["preemptive_generation"]["enabled"] is False
    assert th["interruption"]["enabled"] is True  # real IVR prompts still supersede a stale answer
    assert th["interruption"]["min_words"] >= 2  # short self-echo transcripts don't trip the pause


def test_ivr_navigator_wires_patient_turn_config_and_vera_does_not() -> None:
    # The navigator carries the patient override; VeraAgent leaves it unset so it inherits the
    # snappy human session default — which is how the config reverts automatically at the handoff.
    nav = _navigator()
    assert nav.turn_detection == "vad"
    vera = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    assert not is_given(vera.turn_detection)


@pytest.mark.asyncio
async def test_press_keypad_sends_dtmf_without_echoing_digits() -> None:
    agent = _navigator()
    participant = _FakeParticipant()
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
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
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        result = await _press(agent, "1")
    assert "could not send" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("digits", ["", "   "])
async def test_press_keypad_empty_reports_nothing_pressed(digits: str) -> None:
    # An empty/whitespace sequence sends no tones; the tool must tell the model nothing was
    # pressed instead of a false "sent" — and must not publish anything.
    agent = _navigator()
    participant = _FakeParticipant()
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        result = await _press(agent, digits)
    assert participant.sent == []
    assert "nothing" in result.lower()
    assert "sent the keypad tones" not in result.lower()


@pytest.mark.asyncio
async def test_press_keypad_rejects_invalid_keys_without_echoing_them() -> None:
    # Invalid keypad input must not be sent, and — since the return feeds the LLM/traces —
    # the offending characters must never appear in the return string (PHI hygiene).
    agent = _navigator()
    participant = _FakeParticipant()
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        # digits chosen to not appear in the fixed help text ("0-9, * or #")
        result = await _press(agent, "17x4")
    assert participant.sent == []  # rejected up front, nothing published
    # neither the invalid character nor the raw input digits are echoed back
    for ch in "17x4":
        assert ch not in result


async def _astream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def _drain(stream: AsyncIterable[str]) -> str:
    return "".join([c async for c in stream])


@pytest.mark.asyncio
async def test_strip_silence_token_swallows_a_silent_turn() -> None:
    # A turn whose entire output is the sentinel yields nothing — so tts_node makes no sound.
    assert await _drain(_strip_silence_token(_astream(SILENCE_TOKEN))) == ""
    # even split across streamed chunks
    assert await _drain(_strip_silence_token(_astream("[[SIL", "ENT]]"))) == ""


@pytest.mark.asyncio
async def test_strip_silence_token_passes_real_speech_through() -> None:
    # A real answer is spoken verbatim (the sentinel filter is transparent to normal output).
    assert await _drain(_strip_silence_token(_astream("Med", "ical"))) == "Medical"


@pytest.mark.asyncio
async def test_strip_silence_token_swallows_label_variant() -> None:
    # Regression: on a silent turn the model sometimes emits the sentinel's LABEL
    # ("SILENCE_TOKEN:") instead of [[SILENT]]. The exact-match stripper let it through, so
    # "SILENCE_TOKEN:" was spoken + transcribed into a live call. All renderings must be silent.
    assert await _drain(_strip_silence_token(_astream("SILENCE_TOKEN:"))) == ""
    assert await _drain(_strip_silence_token(_astream("SILENCE_TOKEN: [[SILENT]]"))) == ""
    assert await _drain(_strip_silence_token(_astream("silence_token :"))) == ""
    # a real answer that merely follows the sentinel still gets spoken (remainder is kept)
    assert await _drain(_strip_silence_token(_astream("[[SILENT]] Provider"))) == " Provider"


@pytest.mark.asyncio
async def test_strip_silence_token_label_is_word_boundaried() -> None:
    # The label alternative must strip only the standalone label, never a word that merely
    # contains it — an unanchored regex turned "the SILENCE_TOKENS list" into "the S list".
    assert (
        await _drain(_strip_silence_token(_astream("read the SILENCE_TOKENS list")))
        == "read the SILENCE_TOKENS list"
    )


def test_build_agent_selects_by_ivr_navigation_flag() -> None:
    boundary = PassthroughPHIBoundary()
    nav = build_agent({"enable_ivr_navigation": True}, boundary=boundary, session_id="s1")
    assert isinstance(nav, IvrNavigatorAgent)
    # absent or false → the default chat persona
    assert isinstance(build_agent({}, boundary=boundary, session_id="s1"), VeraAgent)
    assert isinstance(
        build_agent({"enable_ivr_navigation": False}, boundary=boundary, session_id="s1"),
        VeraAgent,
    )


def test_build_agent_playbook_specializes_but_never_selects() -> None:
    boundary = PassthroughPHIBoundary()
    # With the flag on, the playbook specializes the navigator's instructions.
    nav = build_agent(
        {"enable_ivr_navigation": True, "ivr_playbook": {"rep_keyword": "Advocate"}},
        boundary=boundary,
        session_id="s1",
    )
    assert isinstance(nav, IvrNavigatorAgent)
    assert "<rep_keyword>Advocate</rep_keyword>" in nav.instructions
    # The flag is the sole selector: a playbook without it — even with the flag explicitly
    # false — never overrides the opt-out into a silent-on-connect navigator.
    for meta in (
        {"ivr_playbook": {"rep_keyword": "Advocate"}},
        {"enable_ivr_navigation": False, "ivr_playbook": {"rep_keyword": "Advocate"}},
    ):
        assert isinstance(build_agent(meta, boundary=boundary, session_id="s1"), VeraAgent)
    # A malformed playbook is fail-safe: the navigator still runs, just generic.
    generic = build_agent(
        {"enable_ivr_navigation": True, "ivr_playbook": {"bogus": "x"}},
        boundary=boundary,
        session_id="s1",
    )
    assert isinstance(generic, IvrNavigatorAgent)
    assert "<provider_playbook" not in generic.instructions
