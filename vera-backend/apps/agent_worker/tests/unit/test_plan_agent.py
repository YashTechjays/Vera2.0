"""Plan-driven task agent: a generic agent per task, driven only by the CallPlan and a
shared answer map (the two seams — per-answer cascade + state across the handoff)."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from livekit.agents import Agent
from livekit.agents.llm import FunctionTool

from agent_worker.agent import VeraAgent, build_agent
from agent_worker.plan_agent import PlanTaskAgent
from agent_worker.plan_run_state import PlanRunState
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.planning import compile_call_plan
from vera_core.phi import PassthroughPHIBoundary

COVERED = "sections.svc.covered"
COPAY = "sections.svc.copay"


def _plan() -> Any:
    raw: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "T",
        "insurance_type": "infertility_treatment",
        "sections": {
            "svc": {
                "title": "Service",
                "fields": {
                    "covered": {
                        "type": "enum",
                        "title": "Covered",
                        "role": "ask",
                        "values": ["Yes", "No"],
                        "required": True,
                        "prompt": {"ask": "Is this service covered?"},
                    },
                    "copay": {
                        "type": "currency",
                        "title": "Copay",
                        "role": "ask",
                        "required": True,
                        "applicable_when": {"field": COVERED, "op": "eq", "value": "Yes"},
                        "inapplicable_value": "$0",
                        "prompt": {"ask": "What is the copay?"},
                    },
                },
            },
            "close": {
                "title": "Close",
                "fields": {
                    "rep_name": {
                        "type": "text",
                        "title": "Rep",
                        "role": "ask",
                        "required": True,
                        "prompt": {"ask": "May I have your name?"},
                    }
                },
            },
        },
        "tasks": [
            {
                "task_key": "svc",
                "title": "Service",
                "intro": "Coverage details.",
                "outro": "Thanks.",
                "sections": ["svc"],
            },
            {"task_key": "close", "title": "Close", "sections": ["close"]},
        ],
    }
    return compile_call_plan(
        FormSchemaDoc.model_validate(raw), call_id="c", room_name="r", current_year=2026
    )


def _state() -> PlanRunState:
    return PlanRunState(
        plan=_plan(), answers={}, boundary=PassthroughPHIBoundary(), session_id="s1"
    )


def _agent(state: PlanRunState, task_key: str = "svc") -> PlanTaskAgent:
    return PlanTaskAgent(state, task_key)


def test_instructions_carry_the_task_questions() -> None:
    agent = _agent(_state())
    assert "Is this service covered?" in agent.instructions


def test_instructions_exclude_the_static_question_script() -> None:
    # The plan supplies the questions; the static SYSTEM_PROMPT interview (diagnostic gate,
    # CPT-code walkthrough, infertility gate) must NOT leak in, or the agent blends two
    # conflicting scripts. (58340 is intentionally NOT checked — it's a Cartesia markup
    # example in the shared readback guide, not a question.)
    instructions = _agent(_state()).instructions.lower()
    assert "diagnostic testing" not in instructions
    assert "labs, x-ray" not in instructions
    assert "diagnostic cpt codes" not in instructions
    assert "five essential data points" not in instructions


def test_greets_on_enter_and_is_plain() -> None:
    agent = _agent(_state())
    assert type(agent).on_enter is not Agent.on_enter
    # plain agent: no PHI-wall node overrides (removed by request)
    assert type(agent).stt_node is Agent.stt_node
    assert type(agent).tts_node is Agent.tts_node


def test_pending_field_is_first_applicable_collect() -> None:
    field = _agent(_state()).pending_field()
    assert field is not None and field.field_path == COVERED


def _ready_agent(state: PlanRunState, task_key: str = "svc") -> PlanTaskAgent:
    """Agent with `_asked` primed as on_enter would (the first field being solicited)."""
    agent = _agent(state, task_key)
    agent._asked = agent.pending_field()
    return agent


def _record_tool(agent: PlanTaskAgent) -> FunctionTool:
    return next(
        t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == "record_answer"
    )


@pytest.mark.asyncio
async def test_record_answer_stores_against_asked_field_and_returns_next() -> None:
    state = _state()
    agent = _ready_agent(state)
    result = await _record_tool(agent)(value="Yes")
    assert state.answers[COVERED] == "Yes"
    assert "copay" in str(result).lower()


@pytest.mark.asyncio
async def test_record_answer_normalizes_synonym_before_storing() -> None:
    # The transcription-miss fix: a wordy "yes" must gate as the canonical "Yes".
    state = _state()
    agent = _ready_agent(state)
    await _record_tool(agent)(value="yes it's covered")
    assert state.answers[COVERED] == "Yes"


@pytest.mark.asyncio
async def test_unrecognized_answer_reprompts_without_storing() -> None:
    state = _state()
    agent = _ready_agent(state)
    result = await _record_tool(agent)(value="maybe, not sure")
    assert COVERED not in state.answers  # nothing stored on a value that can't be validated
    assert "ask again" in str(result).lower()


@pytest.mark.asyncio
async def test_record_with_no_asked_field_does_not_store() -> None:
    state = _state()
    agent = _agent(state)  # _asked left None (nothing being solicited)
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await _record_tool(agent)(value="Yes")
    assert COVERED not in state.answers


@pytest.mark.asyncio
async def test_cascade_no_collapses_children_then_hands_off() -> None:
    state = _state()
    agent = _ready_agent(state)
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        result = await _record_tool(agent)(value="No")
    # covered=No → copay auto-filled inapplicable, task exhausted → hands off to next task
    assert state.answers[COVERED] == "No"
    assert state.answers[COPAY] == "$0"
    assert isinstance(result, PlanTaskAgent)
    assert result._task.task_key == "close"


MEMBER_ID = "sections.ins.member_id"


def _confirm_state() -> PlanRunState:
    raw: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "T",
        "insurance_type": "infertility_treatment",
        "sections": {
            "patient": {
                "title": "Patient",
                "role": "context",
                "fields": {
                    "patient_name": {"type": "text", "title": "Patient Name", "role": "context"}
                },
            },
            "ins": {
                "title": "Insurance",
                "fields": {
                    "member_id": {
                        "type": "text",
                        "title": "Member ID",
                        "role": "confirm",
                        "prompt": {"confirm": "I have the member ID as {{value}} — correct?"},
                    }
                },
            },
        },
        "tasks": [{"task_key": "ins", "title": "Insurance", "sections": ["ins"]}],
    }
    plan = compile_call_plan(
        FormSchemaDoc.model_validate(raw),
        call_id="c",
        room_name="r",
        current_year=2026,
        prefill={MEMBER_ID: "W1", "sections.patient.patient_name": "Jane Doe"},
    )
    return PlanRunState(plan=plan, answers={}, boundary=PassthroughPHIBoundary(), session_id="s1")


def test_instructions_include_confirm_readback_and_known_info() -> None:
    agent = PlanTaskAgent(_confirm_state(), "ins")
    assert "I have the member ID as W1 — correct?" in agent.instructions  # CONFIRM listed
    assert "Patient Name: Jane Doe" in agent.instructions  # known context injected


@pytest.mark.asyncio
async def test_confirm_affirmation_stores_the_prefilled_value() -> None:
    state = _confirm_state()
    agent = PlanTaskAgent(state, "ins")
    agent._asked = agent.pending_field()
    assert agent._asked is not None and agent._asked.field_path == MEMBER_ID
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await _record_tool(agent)(value="yes, that's right")
    assert state.answers[MEMBER_ID] == "W1"


@pytest.mark.asyncio
async def test_confirm_correction_stores_the_corrected_value() -> None:
    state = _confirm_state()
    agent = PlanTaskAgent(state, "ins")
    agent._asked = agent.pending_field()
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await _record_tool(agent)(value="W99")
    assert state.answers[MEMBER_ID] == "W99"


def _terminate_state() -> PlanRunState:
    raw: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "T",
        "insurance_type": "infertility_treatment",
        "sections": {
            "elig": {
                "title": "Eligibility",
                "fields": {
                    "eligible": {
                        "type": "enum",
                        "title": "Eligible",
                        "role": "ask",
                        "values": ["Yes", "No"],
                        "required": True,
                        "prompt": {"ask": "Eligible?"},
                    },
                    "note": {
                        "type": "text",
                        "title": "Note",
                        "role": "ask",
                        "prompt": {"ask": "Any note?"},
                    },
                },
            },
            "closing": {
                "title": "Closing",
                "fields": {
                    "rep": {
                        "type": "text",
                        "title": "Rep",
                        "role": "ask",
                        "required": True,
                        "prompt": {"ask": "Your name?"},
                    }
                },
            },
        },
        "tasks": [
            {"task_key": "start", "title": "Start", "sections": ["elig"]},
            {"task_key": "wrap_up", "title": "Wrap", "sections": ["closing"]},
        ],
        "flow_rules": [
            {
                "rule_key": "ineligible",
                "when": {"field": "sections.elig.eligible", "op": "eq", "value": "No"},
                "action": "terminate_call",
                "skip_to_task": "wrap_up",
            }
        ],
    }
    plan = compile_call_plan(
        FormSchemaDoc.model_validate(raw), call_id="c", room_name="r", current_year=2026
    )
    return PlanRunState(plan=plan, answers={}, boundary=PassthroughPHIBoundary(), session_id="s1")


@pytest.mark.asyncio
async def test_terminating_answer_hands_off_immediately_mid_task() -> None:
    # A terminate flow rule satisfied mid-task must jump to wrap_up right away — NOT keep
    # asking the rest of the current task (the out-of-network dead-end-verification bug).
    state = _terminate_state()
    agent = PlanTaskAgent(state, "start")
    agent._asked = agent.pending_field()  # eligible
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        result = await _record_tool(agent)(value="No")
    assert isinstance(result, PlanTaskAgent)
    assert result._task.task_key == "wrap_up"  # not "ask the next question"
    assert "sections.elig.note" not in state.answers  # the rest of the task was skipped


def test_handoff_shares_the_answer_map_across_agents() -> None:
    # Seam 2: a second task agent reads the SAME answers the first collected.
    state = _state()
    state.answers[COVERED] = "Yes"
    second = _agent(state, "close")
    assert second._state.answers[COVERED] == "Yes"


def test_build_agent_uses_plan_when_present() -> None:
    boundary = PassthroughPHIBoundary()
    state = _state()
    agent = build_agent({}, boundary=boundary, session_id="s1", plan_state=state)
    assert isinstance(agent, PlanTaskAgent)
    assert agent._task.task_key == "svc"  # first task
    # No plan → the static persona (fallback).
    assert isinstance(build_agent({}, boundary=boundary, session_id="s1"), VeraAgent)
