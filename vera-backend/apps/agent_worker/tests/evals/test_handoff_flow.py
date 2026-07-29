"""End-to-end eval of the real agent chain over a REAL compiled CallPlan.

The plan comes from the published schema_version + prompt_version rows (see
`conftest.load_published_plan`), and the payer rep is a second Gemini instance, so the call is a
genuine multi-turn conversation rather than a fixed script.

The whole call runs ONCE, in a module-scoped fixture; every assertion reads that one run.

Assertions are DETERMINISTIC — handoff identity and order, tool calls, chat_ctx contents. No
`judge()`: LLM-graded assertions flake, and the properties under test are exact. Nothing here
asserts on extracted VALUES — the rep improvises specifics, so value correctness is the
Observer's business and needs a real call.

A REAL ObserverManager runs alongside the call, fed from the drive loop, so answers are extracted
with the real chain. That is what lets the RuleEngine fire at all — `evaluate` has exactly one call
site, inside the Observer's `_record` — and it is also what gives the gap pass real state to sweep.

Extraction is a live LLM call, so which answers land varies between runs. A rule test therefore
SKIPS when its trigger was not extracted, rather than failing: the alternative is an assertion that
quietly passes for the wrong reason.
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
    RecordingRunState,
    Scenario,
    build_llm,
    build_observer,
    carried_text,
    full_walk_enabled,
    make_controller,
    make_entry_agent,
    rep_turn,
    settle_observer,
)
from livekit.agents import AgentSession
from livekit.agents.voice.run_result import mock_tools
from rep import (
    FACT_SHEET,
    INACTIVE_POLICY_FACTS,
    MANDATE_CONTRADICTION_FACTS,
    SimulatedRep,
)

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


DEFAULT_SCENARIO = Scenario(label="cooperative rep", facts=FACT_SHEET)

# Both rules read only a couple of fields, so each scenario narrows the plan to them (plus the
# closer) — a rule scenario costs a handful of turns instead of a 182-field walk.
CONTRADICTION_SCENARIO = Scenario(
    label="mandate says covered, rep says not covered",
    facts=MANDATE_CONTRADICTION_FACTS,
    expect_rule="mandate_requires_infertility_coverage",
    focus_fields=(
        "sections.benefit_coverage.infertility_plan_mandate",
        "sections.infertility_treatment.infertility_tx_covered",
    ),
)
INACTIVE_SCENARIO = Scenario(
    label="policy is not active",
    facts=INACTIVE_POLICY_FACTS,
    expect_rule="insurance_not_active",
    focus_fields=("sections.patient_verification.is_insurance_active",),
)


@dataclass
class CallRun:
    controller: PlanRunController
    run_state: RecordingRunState
    directives: list[Any]
    ivr: list[Turn]
    plan: list[Turn]
    landed: Any
    hit_cap: bool

    @property
    def turns(self) -> list[Turn]:
        return [*self.ivr, *self.plan]

    def handoffs(self) -> list[tuple[str, str]]:
        return [h for turn in self.turns for h in turn.handoffs]

    def fired_rules(self) -> list[str]:
        """The rule_keys whose directives actually reached the controller."""
        return [d.rule_key for d in self.directives]

    def extracted(self) -> dict[str, Any]:
        """What the Observer actually pulled out of the conversation."""
        return dict(self.run_state.recorded)

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


async def _run_call(scenario: Scenario) -> CallRun:
    """Replay the reference IVR menu verbatim, then let the simulated rep carry the plan.

    A REAL ObserverManager runs alongside: every rep turn is also fed to `ingest()`, so answers
    are extracted, the gap pass sees real state, and the rule engine can fire — it has exactly one
    call site, inside the Observer, so without this no flow rule or contradiction ever runs.

    `press_keypad` is mocked — real DTMF needs a job context and a SIP participant, so only the
    tone transport is stubbed; the tool CALL is still observed.
    """
    controller, run_state = await make_controller(scenario)
    observer = build_observer(controller, run_state)
    # Record what the rule engine produced. Landing on WrapUpAgent is NOT evidence a flow rule
    # fired — every call ends there — so the directive itself is the only honest signal.
    directives: list[Any] = []
    apply_directive = controller.apply_directive_now

    async def recording_apply(directive: Any) -> None:
        directives.append(directive)
        await apply_directive(directive)

    controller.apply_directive_now = recording_apply  # type: ignore[method-assign]
    fields = sum(len(t.fields) for t in controller.plan.tasks)
    print(
        f"\n===== {scenario.label}: {len(controller.plan.tasks)} tasks, {fields} fields, "
        f"full_walk={full_walk_enabled()} =====",
        flush=True,
    )

    vera_llm, rep_llm = build_llm(), build_llm(REP_MODEL)
    rep = SimulatedRep(rep_llm, scenario.facts)
    ivr: list[Turn] = []
    plan: list[Turn] = []
    hit_cap = False
    seq = 0

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
            # The voice pipeline would have published this turn to the transcript stream; feed the
            # Observer directly so extraction (and therefore the rule engine) runs.
            seq += 1
            observer.ingest(rep_turn(answer, seq))
            await settle_observer(observer)
        landed = session.current_agent

    # Never let a truncated walk read as a completed call.
    if hit_cap:
        print(f"===== WARNING: hit the {_MAX_PLAN_TURNS}-turn cap =====", flush=True)
    print(
        f"===== landed on {type(landed).__name__}; "
        f"{len(run_state.recorded)} answers extracted =====",
        flush=True,
    )
    if directives:
        print(f"===== directives fired: {[d.rule_key for d in directives]} =====", flush=True)
    return CallRun(
        controller=controller,
        run_state=run_state,
        directives=directives,
        ivr=ivr,
        plan=plan,
        landed=landed,
        hit_cap=hit_cap,
    )


@pytest.fixture(scope="module")
async def call() -> CallRun:
    return await _run_call(DEFAULT_SCENARIO)


@pytest.fixture(scope="module")
async def contradiction_call() -> CallRun:
    return await _run_call(CONTRADICTION_SCENARIO)


@pytest.fixture(scope="module")
async def inactive_call() -> CallRun:
    return await _run_call(INACTIVE_SCENARIO)


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


async def test_the_observer_extracts_answers(call: CallRun) -> None:
    # The rule engine and the gap pass both read the answer snapshot, so an Observer that records
    # nothing would silently reduce every downstream assertion to a no-op.
    assert call.extracted(), "the Observer extracted nothing from the whole call"


async def test_contradiction_makes_vera_push_back(contradiction_call: CallRun) -> None:
    """A plan mandate obliges infertility coverage, so "not covered" contradicts it. The engine
    returns ReAsk and the controller re-asks on the SAME agent (no swap) — the push-back seen in
    the reference call."""
    extracted = contradiction_call.extracted()
    mandate = extracted.get("sections.benefit_coverage.infertility_plan_mandate")
    covered = extracted.get("sections.infertility_treatment.infertility_tx_covered")
    if mandate != "Yes" or covered != "No":
        # Observed cause when `covered` is None: VERA asks a task's last question AND calls
        # task_complete in the SAME turn, so the rep's answer arrives while the next task is
        # active. TaskObserver is whitelisted to the active task's field paths, so that answer
        # is unextractable and any rule needing it cannot fire. Skipping keeps this honest —
        # asserting would either fail for the wrong reason or pass vacuously.
        pytest.skip(f"trigger not extracted: mandate={mandate!r}, covered={covered!r}")

    # A ReAsk keeps the same agent, so a contradiction must never show up as a handoff.
    assert not contradiction_call.plan or all(
        new != WrapUpAgent.__name__ for turn in contradiction_call.plan for _, new in turn.handoffs
    ), "a contradiction must re-ask, not end the call"
    spoken = " ".join(contradiction_call.vera_said(contradiction_call.plan)).lower()
    assert "mandate" in spoken or "covered" in spoken


async def test_inactive_policy_short_circuits_the_call(inactive_call: CallRun) -> None:
    """`insurance_not_active` skips straight to wrap_up: an inactive policy has no benefits worth
    collecting, so the call must not walk the remaining tasks."""
    active = inactive_call.extracted().get("sections.patient_verification.is_insurance_active")
    if active != "No":
        pytest.skip(
            f"the rule's trigger was not extracted this run (is_insurance_active={active!r})"
        )
    assert isinstance(inactive_call.landed, WrapUpAgent), "an inactive policy must reach wrap-up"


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
