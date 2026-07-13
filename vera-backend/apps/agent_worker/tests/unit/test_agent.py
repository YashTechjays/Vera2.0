"""Tests for the cascade agents — the plan-only conversational path, the IVR
navigator, the apology agent, and the metadata-driven selector."""

import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from livekit.agents import Agent, StopResponse
from livekit.agents.llm import FunctionTool
from livekit.agents.utils import is_given

from agent_worker.agent import APOLOGY_LINE, ApologyAgent, VeraAgent, build_agent
from agent_worker.ivr_agent import (
    _IVR_MAX_TURNS,
    IvrNavigatorAgent,
    _strip_silence_token,
    ivr_turn_handling,
)
from agent_worker.ivr_prompt import SILENCE_TOKEN
from agent_worker.plan_runtime import PlanRunController, PlanTaskAgent
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask


def _plan_controller() -> PlanRunController:
    """A minimal live controller — the required input to build_agent / the IVR handoff."""
    plan = CallPlan(
        schema_name="T",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[PlanTask(task_key="t1", title="T1", prompt="ask")],
    )
    return PlanRunController(
        plan,
        room_name="call--t--c",
        run_state=cast(Any, None),  # never touched before on_enter
    )


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


def _navigator(**kwargs: Any) -> IvrNavigatorAgent:
    """An IVR navigator with a plan-task handoff factory (required)."""
    controller = _plan_controller()
    kwargs.setdefault("verification_agent_factory", controller.first_agent)
    return IvrNavigatorAgent(**kwargs)


def test_vera_agent_carries_only_the_end_call_tool() -> None:
    agent = VeraAgent(instructions="do things")
    tool_names = [t.info.name for t in agent.tools if isinstance(t, FunctionTool)]
    assert tool_names == ["end_call"]
    assert agent.instructions == "do things"


@pytest.mark.asyncio
async def test_apology_agent_speaks_one_line_and_hangs_up() -> None:
    agent = ApologyAgent()
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await agent.on_enter()
    mock_session.say.assert_called_once_with(APOLOGY_LINE)
    mock_session.shutdown.assert_called_once_with(drain=True)


def test_ivr_navigator_agent_is_generic_and_silent_on_enter() -> None:
    agent = _navigator()
    # navigator carries the generic eligibility/benefits instructions
    assert "eligibility" in agent.instructions.lower()
    assert "infertility" not in agent.instructions.lower()
    # the navigator listens first: it does NOT greet, so on_enter stays the base no-op
    assert type(agent).on_enter is Agent.on_enter
    # stt stays the base default; tts/transcription ARE overridden — only to strip the
    # silence sentinel, so a "stay silent" turn makes no sound
    assert type(agent).stt_node is Agent.stt_node
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


def test_ivr_navigator_wires_patient_turn_config_and_plan_agent_does_not() -> None:
    # The navigator carries the patient override; the plan task agent leaves it unset so it
    # inherits the snappy human session default — the config reverts automatically at handoff.
    nav = _navigator()
    assert nav.turn_detection == "vad"
    plan_agent = _plan_controller().first_agent()
    assert not is_given(plan_agent.turn_detection)


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


@pytest.mark.asyncio
async def test_press_keypad_reports_a_successful_press_to_on_keypress() -> None:
    # The transcript needs evidence of the action: a successful press reports the digits
    # actually sent (normalized) to the injected callback, which feeds the live transcript.
    pressed: list[str] = []
    agent = _navigator(on_keypress=pressed.append)
    participant = _FakeParticipant()
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        await _press(agent, " 3 ")
    assert pressed == ["3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("digits", ["", "   ", "17x4"])
async def test_press_keypad_does_not_report_a_press_that_sent_nothing(digits: str) -> None:
    # Empty and invalid sequences emit no tones — nothing to evidence in the transcript.
    pressed: list[str] = []
    agent = _navigator(on_keypress=pressed.append)
    participant = _FakeParticipant()
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        await _press(agent, digits)
    assert pressed == []


@pytest.mark.asyncio
async def test_press_keypad_does_not_report_a_failed_press() -> None:
    # A transport failure means no tones reached the line — reporting it would fabricate
    # evidence of an action that never happened.
    pressed: list[str] = []
    agent = _navigator(on_keypress=pressed.append)
    participant = _FakeParticipant(raise_on_publish=True)
    with patch("agent_worker.ivr_agent.get_job_context", return_value=_job_ctx(participant)):
        await _press(agent, "3")
    assert pressed == []


def test_build_agent_passes_on_keypress_to_the_navigator() -> None:
    def _cb(digits: str) -> None:  # pragma: no cover - never fired here
        pass

    agent = build_agent(
        {"enable_ivr_navigation": True}, controller=_plan_controller(), on_keypress=_cb
    )
    assert isinstance(agent, IvrNavigatorAgent)
    assert agent._on_keypress is _cb


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
    controller = _plan_controller()
    nav = build_agent({"enable_ivr_navigation": True}, controller=controller)
    assert isinstance(nav, IvrNavigatorAgent)
    # absent or false → the plan's first task agent directly
    assert build_agent({}, controller=controller) is controller.agents[0]
    assert (
        build_agent({"enable_ivr_navigation": False}, controller=controller) is controller.agents[0]
    )


def test_build_agent_playbook_specializes_but_never_selects() -> None:
    controller = _plan_controller()
    # With the flag on, the playbook specializes the navigator's instructions.
    nav = build_agent(
        {"enable_ivr_navigation": True, "ivr_playbook": {"provider_subflows": "Press 3"}},
        controller=controller,
    )
    assert isinstance(nav, IvrNavigatorAgent)
    assert "<provider_subflows>Press 3</provider_subflows>" in nav.instructions
    # The flag is the sole selector: a playbook without it — even with the flag explicitly
    # false — never overrides the opt-out into a silent-on-connect navigator.
    for meta in (
        {"ivr_playbook": {"provider_subflows": "Press 3"}},
        {"enable_ivr_navigation": False, "ivr_playbook": {"provider_subflows": "Press 3"}},
    ):
        assert build_agent(meta, controller=controller) is controller.agents[0]
    # A malformed playbook is fail-safe: the navigator still runs, just generic.
    generic = build_agent(
        {"enable_ivr_navigation": True, "ivr_playbook": {"bogus": "x"}},
        controller=controller,
    )
    assert isinstance(generic, IvrNavigatorAgent)
    assert "<provider_playbook" not in generic.instructions


def test_build_agent_with_controller_starts_on_the_first_plan_task() -> None:
    controller = _plan_controller()
    agent = build_agent({}, controller=controller)
    assert isinstance(agent, PlanTaskAgent)
    assert agent is controller.agents[0]


@pytest.mark.asyncio
async def test_ivr_hands_off_to_the_first_plan_task_when_a_plan_is_active() -> None:
    controller = _plan_controller()
    agent = build_agent({"enable_ivr_navigation": True}, controller=controller)
    assert isinstance(agent, IvrNavigatorAgent)
    call = cast("Callable[[], Awaitable[Agent]]", agent.transfer_to_verification)
    handoff = await call()
    assert isinstance(handoff, PlanTaskAgent)
    assert handoff is controller.agents[0]
