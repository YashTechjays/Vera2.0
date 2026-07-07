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


def test_greets_on_enter_and_is_plain() -> None:
    agent = _agent(_state())
    assert type(agent).on_enter is not Agent.on_enter
    # plain agent: no PHI-wall node overrides (removed by request)
    assert type(agent).stt_node is Agent.stt_node
    assert type(agent).tts_node is Agent.tts_node


def test_pending_field_is_first_applicable_collect() -> None:
    field = _agent(_state()).pending_field()
    assert field is not None and field.field_path == COVERED


@pytest.mark.asyncio
async def test_record_answer_stores_and_returns_next_question() -> None:
    state = _state()
    agent = _agent(state)
    tool = next(
        t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == "record_answer"
    )
    result = await tool(value="Yes")
    assert state.answers[COVERED] == "Yes"
    assert "copay" in str(result).lower()


@pytest.mark.asyncio
async def test_cascade_no_collapses_children_then_hands_off() -> None:
    state = _state()
    agent = _agent(state)
    tool = next(
        t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == "record_answer"
    )
    mock_session = MagicMock()
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        result = await tool(value="No")
    # covered=No → copay auto-filled inapplicable, task exhausted → hands off to next task
    assert state.answers[COVERED] == "No"
    assert state.answers[COPAY] == "$0"
    assert isinstance(result, PlanTaskAgent)
    assert result._task.task_key == "close"


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
