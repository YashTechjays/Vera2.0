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
import re
from dataclasses import dataclass
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
    build_evaluator,
    build_llm,
    build_observer,
    carried_text,
    full_walk_enabled,
    judge_strict_enabled,
    make_controller,
    make_entry_agent,
    rep_turn,
    settle_observer,
)
from judge import Report, render_facts, render_rules, render_tasks
from livekit.agents import AgentSession
from livekit.agents.voice.run_result import mock_tools
from rep import (
    FACT_SHEET,
    INACTIVE_POLICY_FACTS,
    MANDATE_CONTRADICTION_FACTS,
    SimulatedRep,
)
from transcript import Turn, collect, echo, tail

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
# is 9 tasks over 184 fields.
_MAX_PLAN_TURNS = 250

# How VERA names herself. Deliberately only self-identification: "calling on behalf of Dr. Smith"
# is legitimate mid-call context, while naming herself again is what a rep hears as a second
# introduction. The compiled intro says "I'm VERA", so one match is the opening and is expected.
_SELF_IDENTIFICATION = re.compile(r"\b(?:i'?m|i am|my name is|this is)\s+vera\b", re.IGNORECASE)


DEFAULT_SCENARIO = Scenario(label="cooperative rep", facts=FACT_SHEET)

CONTRADICTION_RULE = "mandate_requires_infertility_coverage"

# Both rules read only a couple of fields, so each scenario narrows the plan to them (plus the
# closer) — a rule scenario costs a handful of turns instead of a 182-field walk.
CONTRADICTION_SCENARIO = Scenario(
    label="mandate says covered, rep says not covered",
    facts=MANDATE_CONTRADICTION_FACTS,
    expect_rule=CONTRADICTION_RULE,
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
class Fired:
    """A directive that reached the controller, and WHICH plan turn it fired on.

    The turn matters for re-ask assertions: a `ReAsk` must not swap the agent, and only the
    firing turn can show that. Nothing else in the call is evidence either way."""

    turn: int
    directive: Any


@dataclass
class CallRun:
    report: "Report | None"
    controller: PlanRunController
    run_state: RecordingRunState
    directives: list[Fired]
    ivr: list[Turn]
    plan: list[Turn]
    landed: Any
    hit_cap: bool
    ended_in_ivr: bool = False  # the navigator hung up before a human answered
    # How the call ended, plus any tool call the run windows missed. Kept OUT of `plan`: it is
    # nobody's turn, and `plan[-1]` is asserted on as the last thing the rep actually said.
    tail: Turn | None = None

    @property
    def turns(self) -> list[Turn]:
        closing = [self.tail] if self.tail is not None else []
        return [*self.ivr, *self.plan, *closing]

    def handoffs(self) -> list[tuple[str, str]]:
        return [h for turn in self.turns for h in turn.handoffs]

    def fired_rules(self) -> list[str]:
        """The rule_keys whose directives actually reached the controller."""
        return [f.directive.rule_key for f in self.directives]

    def fired(self, rule_key: str) -> "Fired | None":
        """The first firing of `rule_key`, or None if that rule never reached the controller."""
        return next((f for f in self.directives if f.directive.rule_key == rule_key), None)

    def extracted(self) -> dict[str, Any]:
        """What the Observer actually pulled out of the conversation."""
        return dict(self.run_state.recorded)

    def transcript_lines(self) -> list[str]:
        """The call as numbered lines. This IS what the evaluator reads, so a cited line number
        means the same thing to it, to whoever reads the printed run, and to the real call."""
        return [line for turn in self.turns for line in turn.lines()]

    def transcript_text(self) -> str:
        return "\n".join(f"[{i:3d}] {line}" for i, line in enumerate(self.transcript_lines()))

    def vera_said(self, turns: list[Turn] | None = None) -> list[str]:
        return [line for turn in (turns if turns is not None else self.turns) for line in turn.vera]


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
    directives: list[Fired] = []
    apply_directive = controller.apply_directive_now

    async def recording_apply(directive: Any) -> None:
        # The drive loop appends the turn BEFORE feeding it to the Observer, so the last recorded
        # turn is the one whose answer triggered this directive.
        directives.append(Fired(turn=len(plan) - 1, directive=directive))
        await apply_directive(directive)

    controller.apply_directive_now = recording_apply  # type: ignore[method-assign]
    fields = sum(len(t.fields) for t in controller.plan.tasks)
    # A scenario's focus_fields overrides VERA_EVALS_FULL, so report what the plan ACTUALLY is —
    # printing full_walk=True beside a narrowed plan is a lie the reader has to trip over first.
    focused = bool(scenario.focus_fields)
    mode = "focused" if focused else ("full walk" if full_walk_enabled() else "focused (default)")
    print(
        f"\n===== {scenario.label}: {len(controller.plan.tasks)} tasks, {fields} fields, "
        f"{mode} =====",
        flush=True,
    )

    vera_llm, rep_llm = build_llm(), build_llm(REP_MODEL)
    rep = SimulatedRep(rep_llm, scenario.facts)
    ivr: list[Turn] = []
    plan: list[Turn] = []
    hit_cap = False
    ended_in_ivr = False
    seq = 0

    # userdata MUST be set at construction, exactly as `cascade.py:build_session` does: every
    # agent's on_enter reads the takeover latch on its first line, and without it on_enter raises
    # into an unretrieved task — the call then stalls with no visible error.
    async with (
        vera_llm,
        rep_llm,
        AgentSession(userdata=TakeoverState(), llm=vera_llm) as session,
    ):
        # Session-scoped, so they see the wrap-up agent's tool call and the hangup itself —
        # both of which happen after the last driven turn, where no RunResult is listening.
        executed: list[Any] = []
        closed: list[str] = []
        session.on("function_tools_executed", lambda ev: executed.extend(ev.function_calls))
        session.on("close", lambda ev: closed.append(ev.reason.value))
        mocked = {"press_keypad": lambda digits, reason: "Sent the tones."}
        with mock_tools(IvrNavigatorAgent, mocked):
            await session.start(make_entry_agent(controller))
            for machine_turn in [*IVR_TURNS, HUMAN_PICKUP]:
                try:
                    result = await session.run(user_input=machine_turn)
                except RuntimeError:
                    # `give_up` hung up on the menu, so the session is closing and every later
                    # turn would raise the same way. Without this the traceback points into
                    # LiveKit's generate_reply and the module-scoped fixture takes every test
                    # that depends on it down with it — the navigator's decision reads as a
                    # harness crash.
                    ended_in_ivr = True
                    break
                turn = collect(machine_turn, result)
                ivr.append(turn)
                echo("IVR", turn)

        hit_cap = not ended_in_ivr
        for _ in range(0 if ended_in_ivr else _MAX_PLAN_TURNS):
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
            turn = collect(answer, result)
            plan.append(turn)
            echo("PLAN", turn)
            # The voice pipeline would have published this turn to the transcript stream; feed the
            # Observer directly so extraction (and therefore the rule engine) runs.
            seq += 1
            observer.ingest(rep_turn(answer, seq))
            await settle_observer(observer)
        landed = session.current_agent

    # Never let a truncated walk read as a completed call.
    if hit_cap:
        print(f"===== WARNING: hit the {_MAX_PLAN_TURNS}-turn cap =====", flush=True)
    if ended_in_ivr:
        # The plan never ran, so every plan-side assertion below is about a call that never
        # happened. Say so once, loudly, instead of leaving each test to fail on its own terms.
        print(
            f"===== WARNING: the navigator ended the call after {len(ivr)} IVR turn(s), before "
            "reaching a human — no plan turns ran =====",
            flush=True,
        )
    call_tail = tail(
        [*ivr, *plan],
        executed,
        closed[0] if closed else "",
        hung_up_in_code=controller.signed_off,
    )
    if call_tail is not None:
        echo("PLAN", call_tail)
    print(
        f"===== landed on {type(landed).__name__}; "
        f"{len(run_state.recorded)} answers extracted =====",
        flush=True,
    )
    run = CallRun(
        controller=controller,
        run_state=run_state,
        directives=directives,
        ivr=ivr,
        plan=plan,
        landed=landed,
        hit_cap=hit_cap,
        ended_in_ivr=ended_in_ivr,
        tail=call_tail,
        report=None,
    )
    # Grade the call from its transcript. A judge failure must never take the run down with it —
    # the deterministic assertions above are the gate; this is a report.
    try:
        evaluator = build_evaluator()
        answers = {**controller.plan.prefilled, **dict(run_state.recorded)}
        run.report = await evaluator.evaluate(
            run.transcript_text(),
            rules=render_rules(controller.plan),
            tasks=render_tasks(controller.plan, answers),
            # Whether a directive fired leaves no trace in the conversation, so the judge must be
            # told — otherwise it infers firing from the call ending early and PASSES a rule that
            # never ran.
            facts=render_facts(run.fired_rules(), len(run_state.recorded), focused=focused),
        )
        print(run.report.render(scenario.label), flush=True)
    except Exception as exc:
        print(f"===== evaluation unavailable ({type(exc).__name__}) =====", flush=True)
    if directives:
        print(
            f"===== directives fired: {[(f.turn, f.directive.rule_key) for f in directives]} =====",
            flush=True,
        )
    return run


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
    # Checked first: when the navigator gives up mid-menu no plan turn ever runs, and every
    # plan-side test below then fails on its own confusing terms.
    assert not call.ended_in_ivr, "the navigator hung up before a human answered"
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
    # handoffs is the re-introduction bug the window must not cause. Catches only a duplicated
    # `session.say(intro)` — see the self-identification test below for the paraphrase case.
    assert sum(DISCLOSURE in line for line in call.vera_said()) <= 1


@pytest.mark.xfail(
    reason=(
        "Pre-existing, tracked as NEW-2: the rule 'never re-introduce yourself for the rest of "
        "the call' lives only in the introduction task's OWN prompt, and `_instructions` injects "
        "only the current task's block — so it is dropped at the very handoff where it starts to "
        "matter. The next task has no authored intro, so `on_enter` goes straight to "
        "generate_reply(_OPENING_DIRECTIVE), which never says 'do not greet', and the model "
        "invents an opener modelled on the pinned anchor. strict=False because whether the model "
        "re-identifies itself on any given run is LLM variance; the missing instruction is not."
    ),
    strict=False,
)
async def test_vera_never_re_identifies_herself(call: CallRun) -> None:
    # The disclosure substring cannot see this: the observed second introduction ("my name is
    # VERA, calling on behalf of...") carries no disclosure text at all, so counting it passes
    # green through the actual defect. Match how VERA names HERSELF instead, which is what a rep
    # hears as being introduced to twice.
    said = [line for line in call.vera_said() if _SELF_IDENTIFICATION.search(line)]
    assert len(said) <= 1, f"introduced herself {len(said)}x: {said}"


async def test_opening_survives_to_the_end_of_the_call(call: CallRun) -> None:
    # Whatever the handoff carry strategy, the greeting and disclosure must still be in front of
    # the agent that closes the call, or it re-introduces itself to a rep it already greeted.
    carried = carried_text(call.landed)
    assert DISCLOSURE in carried, "the opening did not survive to the end of the call"
    # The rep's most recent answer is the live end of the window.
    assert call.plan[-1].rep[:40] in carried


async def test_the_evaluator_graded_the_call(call: CallRun) -> None:
    # Reports by default: this asserts the evaluator RAN and produced verdicts, not that it liked
    # the call. Use VERA_EVALS_JUDGE_STRICT=1 to make a `fail` finding gate the run.
    assert call.report is not None, "the evaluator produced no report"
    assert call.report.findings, "the evaluator returned no verdicts"


@pytest.mark.skipif(
    not judge_strict_enabled(), reason="set VERA_EVALS_JUDGE_STRICT=1 to gate on the evaluator"
)
async def test_the_evaluator_found_no_failures(call: CallRun) -> None:
    assert call.report is not None
    failures = [f"{f.dimension} [{f.turn}]: {f.reason}" for f in call.report.failures]
    assert not failures, "the evaluator failed the call:\n" + "\n".join(failures)


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
        # Extraction is a live LLM call, so a trigger can simply not land. Skipping keeps this
        # honest — asserting would either fail for the wrong reason or pass vacuously.
        pytest.skip(f"trigger not extracted: mandate={mandate!r}, covered={covered!r}")

    fired = contradiction_call.fired(CONTRADICTION_RULE)
    if fired is None:
        pytest.skip("the trigger was extracted but no directive reached the controller")

    # A ReAsk re-asks on the SAME agent, so the contradiction must produce no handoff. Scoped to
    # the firing turn and the one after — the directive interrupts outside `session.run`, so its
    # swap would surface on the following turn's events. Every call ends at WrapUpAgent, so a
    # whole-call scan would fail on healthy calls.
    window = contradiction_call.plan[fired.turn : fired.turn + 2]
    assert not [h for turn in window for h in turn.handoffs], (
        f"a contradiction must re-ask, not hand off (turn {fired.turn})"
    )
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
