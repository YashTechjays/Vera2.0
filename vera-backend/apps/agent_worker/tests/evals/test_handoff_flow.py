"""End-to-end eval of the real agent chain over a REAL compiled CallPlan.

The plan comes from the published schema_version + prompt_version rows (see
`conftest.load_published_plan`), and the payer rep is a second Gemini instance, so the call is a
genuine multi-turn conversation rather than a fixed script.

The whole call runs ONCE, in a module-scoped fixture; every assertion reads that one run.

Assertions are DETERMINISTIC — handoff identity and order, tool calls, chat_ctx contents. No
`judge()`: LLM-graded assertions flake, and the properties under test are exact. Nothing here
asserts on extracted VALUES — the rep improvises specifics, so value correctness is the
Observer's business and needs a real call.

KNOWN LIMITATION — no Observer runs here, so `PlanRunController._answers` stays at its prefill
seed and `gap_fields()` sees nearly every field as unanswered. The gap pass therefore always
fires and re-asks things the rep already answered. That is the harness, NOT a product bug: these
evals can prove a gap agent's *shape* (it runs mid-call, it never signals the end) but say
nothing about whether it picked the right fields.
"""

import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from conftest import (
    CASE,
    DISCLOSURE,
    HUMAN_PICKUP,
    IVR_TURNS,
    REP_MODEL,
    build_llm,
    carried_text,
    full_walk_enabled,
    make_controller,
    make_entry_agent,
)
from livekit.agents import AgentSession
from livekit.agents.voice.run_result import mock_tools
from rep import SimulatedRep

from agent_worker.intervention import TakeoverState
from agent_worker.ivr_agent import IvrNavigatorAgent
from agent_worker.plan_runtime import GapTaskAgent, PlanRunController, PlanTaskAgent, WrapUpAgent

pytestmark = [
    pytest.mark.evals,
    pytest.mark.skipif(
        not os.getenv("VERA_EVALS_ENABLED"),
        reason="set VERA_EVALS_ENABLED=1 (needs Vertex ADC and a seeded local Postgres)",
    ),
]

# Hard stop so a plan that never reaches wrap-up cannot run forever. Generous: the full IBV walk
# is 7 tasks over 184 fields.
_MAX_PLAN_TURNS = 250


@dataclass
class Turn:
    """One exchange: what the rep said, and what VERA did with it."""

    rep: str
    vera: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    handoffs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CallRun:
    controller: PlanRunController
    ivr: list[Turn]
    plan: list[Turn]
    landed: Any
    hit_cap: bool

    @property
    def turns(self) -> list[Turn]:
        return [*self.ivr, *self.plan]

    def handoffs(self) -> list[tuple[str, str]]:
        return [h for turn in self.turns for h in turn.handoffs]

    def vera_said(self, turns: list[Turn] | None = None) -> list[str]:
        return [line for turn in (turns if turns is not None else self.turns) for line in turn.vera]


def _collect(rep_said: str, result: Any) -> Turn:
    """Fold one RunResult into a readable Turn."""
    turn = Turn(rep=rep_said)
    for ev in result.events:
        if ev.type == "message" and ev.item.role == "assistant":
            turn.vera.append(ev.item.text_content or "")
        elif ev.type == "function_call":
            turn.tools.append(ev.item.name)
        elif ev.type == "agent_handoff":
            turn.handoffs.append((type(ev.old_agent).__name__, type(ev.new_agent).__name__))
    return turn


def _echo(phase: str, turn: Turn) -> None:
    """Live transcript, visible under `pytest -s`. flush=True so a piped run still streams —
    without it the call buffers and a stall looks identical to slow progress."""
    print(f"[{phase}] REP  : {turn.rep}", flush=True)
    for line in turn.vera:
        print(f"[{phase}] VERA : {line}", flush=True)
    for name in turn.tools:
        print(f"[{phase}] TOOL : {name}", flush=True)
    for old, new in turn.handoffs:
        print(f"[{phase}] >>>> HANDOFF {old} -> {new}", flush=True)


@pytest.fixture(scope="module")
async def call() -> CallRun:
    """Replay the reference IVR menu verbatim, then let the simulated rep carry the plan.

    `press_keypad` is mocked — real DTMF needs a job context and a SIP participant, so only the
    tone transport is stubbed; the tool CALL is still observed.
    """
    controller = await make_controller()
    fields = sum(len(t.fields) for t in controller.plan.tasks)
    print(
        f"\n===== plan: {len(controller.plan.tasks)} tasks, {fields} fields, "
        f"full_walk={full_walk_enabled()} =====",
        flush=True,
    )

    vera_llm, rep_llm = build_llm(), build_llm(REP_MODEL)
    rep = SimulatedRep(rep_llm)
    ivr: list[Turn] = []
    plan: list[Turn] = []
    hit_cap = False

    # userdata MUST be set at construction, exactly as `cascade.py:build_session` does: every
    # agent's on_enter reads the takeover latch on its first line, and without it on_enter raises
    # into an unretrieved task — the call then stalls with no visible error.
    async with (
        vera_llm,
        rep_llm,
        AgentSession(userdata=TakeoverState(), llm=vera_llm) as session,
    ):
        with mock_tools(IvrNavigatorAgent, {"press_keypad": lambda digits: "Sent the tones."}):
            await session.start(make_entry_agent(controller))
            for machine_turn in [*IVR_TURNS, HUMAN_PICKUP]:
                turn = _collect(machine_turn, await session.run(user_input=machine_turn))
                ivr.append(turn)
                _echo("IVR", turn)

        hit_cap = True
        for _ in range(_MAX_PLAN_TURNS):
            if isinstance(session.current_agent, WrapUpAgent):
                hit_cap = False
                break
            asked = " ".join(plan[-1].vera) if plan else " ".join(ivr[-1].vera)
            answer = await rep.reply(asked or "Go ahead.")
            try:
                result = await session.run(user_input=answer)
            except RuntimeError:
                hit_cap = False  # end_call closed the session: the call ended normally
                break
            turn = _collect(answer, result)
            plan.append(turn)
            _echo("PLAN", turn)
        landed = session.current_agent

    # Never let a truncated walk read as a completed call.
    if hit_cap:
        print(f"===== WARNING: hit the {_MAX_PLAN_TURNS}-turn cap =====", flush=True)
    print(f"===== landed on {type(landed).__name__} =====", flush=True)
    return CallRun(controller=controller, ivr=ivr, plan=plan, landed=landed, hit_cap=hit_cap)


async def test_plan_came_from_a_published_schema_version(call: CallRun) -> None:
    # Compiled, not hand-written: real lineage and real per-form prefill.
    plan = call.controller.plan
    assert plan.insurance_type == "infertility_treatment"
    assert plan.schema_version_id is not None
    assert plan.prefilled, "fuse_prefill produced no prefill — intake paths did not match"
    assert CASE["patient_name"] in (plan.known_information or "")


async def test_navigator_presses_the_keypad_for_provider(call: CallRun) -> None:
    assert any("press_keypad" in turn.tools for turn in call.ivr)


async def test_no_handoff_while_the_machine_is_talking(call: CallRun) -> None:
    # "Avery" is conversational enough to look human — handing off during the menu would start
    # the plan's greeting at a robot. Only the human pickup may hand off.
    assert not [h for turn in call.ivr[: len(IVR_TURNS)] for h in turn.handoffs]


async def test_human_pickup_hands_off_into_the_plan(call: CallRun) -> None:
    assert call.ivr[-1].handoffs == [("IvrNavigatorAgent", "PlanTaskAgent")]


async def test_walk_reaches_wrap_up(call: CallRun) -> None:
    assert not call.hit_cap, "the walk never reached wrap-up"
    assert isinstance(call.landed, WrapUpAgent)
    assert call.handoffs()[-1][1] == WrapUpAgent.__name__


async def test_tasks_are_visited_in_compiled_order(call: CallRun) -> None:
    visited = sorted(call.controller._visited_tasks)
    assert visited, "no task was ever entered"
    assert visited == list(range(visited[0], visited[-1] + 1)), "a task was skipped mid-plan"


async def test_no_task_reintroduces_itself(call: CallRun) -> None:
    # Spoken once, by whichever agent opens the plan conversation. Any repeat across the
    # handoffs is the re-introduction bug the window must not cause.
    assert sum(DISCLOSURE in line for line in call.vera_said()) <= 1


async def test_opening_survives_to_the_end_of_the_call(call: CallRun) -> None:
    # Whatever the handoff carry strategy, the greeting and disclosure must still be in front of
    # the agent that closes the call, or it re-introduces itself to a rep it already greeted.
    carried = carried_text(call.landed)
    assert DISCLOSURE in carried, "the opening did not survive to the end of the call"
    # The rep's most recent answer is the live end of the window.
    assert call.plan[-1].rep[:40] in carried


@pytest.mark.skipif(not full_walk_enabled(), reason="the gap pass needs the unfocused plan")
async def test_gap_pass_never_signals_the_end_of_the_call(call: CallRun) -> None:
    # A gap agent is a mid-call follow-up: it must not say goodbye (see _gap_block).
    entered_gap = False
    gap_lines: list[str] = []
    for turn in call.plan:
        if any(new == GapTaskAgent.__name__ for _, new in turn.handoffs):
            entered_gap = True
        if entered_gap:
            gap_lines.extend(turn.vera)
        if any(new == PlanTaskAgent.__name__ for _, new in turn.handoffs):
            entered_gap = False
    if not gap_lines:
        pytest.skip("the rep answered every required field — no gap pass this run")
    assert not any("goodbye" in line.lower() for line in gap_lines)


@pytest.mark.xfail(
    reason=(
        "Pre-existing, out of scope here: the closing task's outro and WrapUpAgent's generated "
        "goodbye both fire, so the call signs off twice — as in the reference transcript. "
        "Present with the full context carried too, so it is a wrap-up prompt/flow bug, not a "
        "context-volume one. strict=False because the outro is spoken by a NON-AWAITED "
        "session.say (plan_runtime.py:169), so whether it lands in this run's event window is a "
        "race: the bug is consistent, its visibility here is not."
    ),
    strict=False,
)
async def test_call_says_goodbye_once(call: CallRun) -> None:
    sign_offs = [
        line
        for line in call.vera_said()
        if any(s in line.lower() for s in ("goodbye", "have a good day", "have a wonderful day"))
    ]
    assert len(sign_offs) <= 1, f"signed off {len(sign_offs)}x: {sign_offs}"
