"""Deterministic tests for the evaluator's plumbing — parsing and citation checks.

Deliberately NOT marked `evals`: these need no LLM and no database, so they run in `just check`.
Only the judgement itself is nondeterministic; the machinery around it should not be.
"""

import uuid

from judge import (
    DIMENSIONS,
    GATED_OUT,
    Finding,
    Report,
    _parse,
    render_rules,
    render_tasks,
    verify_citations,
)

from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison, Contradiction, FlowRule


def test_parses_a_plain_json_array() -> None:
    findings = _parse('[{"dimension": "tool_calls", "verdict": "pass", "reason": "ok", "turn": 3}]')
    assert findings == [Finding(dimension="tool_calls", verdict="pass", reason="ok", turn=3)]


def test_tolerates_a_code_fence() -> None:
    # The prompt forbids a fence; models add one anyway, and losing a whole evaluation to
    # formatting would be worse than accepting it.
    reply = (
        '```json\n[{"dimension": "closing", "verdict": "fail", "reason": "twice", "turn": 9}]\n```'
    )
    assert [f.dimension for f in _parse(reply)] == ["closing"]


def test_unparseable_reply_yields_no_findings() -> None:
    assert _parse("I think the call went well, honestly.") == []


def test_drops_unknown_dimensions_and_verdicts() -> None:
    # A hallucinated dimension or a freeform verdict must not reach the report.
    reply = (
        '[{"dimension": "vibes", "verdict": "pass", "reason": "", "turn": 1},'
        ' {"dimension": "closing", "verdict": "excellent", "reason": "", "turn": 1}]'
    )
    assert _parse(reply) == []


def test_null_turn_is_allowed() -> None:
    # Some dimensions genuinely apply to the call as a whole rather than one line.
    findings = _parse(
        '[{"dimension": "overall", "verdict": "pass", "reason": "fine", "turn": null}]'
    )
    assert findings[0].turn is None


def test_citation_outside_the_transcript_is_discarded() -> None:
    findings = [
        Finding(dimension="closing", verdict="fail", reason="real", turn=4),
        Finding(dimension="overall", verdict="fail", reason="invented", turn=99),
    ]
    kept, discarded = verify_citations(findings, line_count=10)
    assert [f.reason for f in kept] == ["real"]
    assert discarded == 1


def test_a_null_citation_survives_verification() -> None:
    kept, discarded = verify_citations([Finding("overall", "pass", "fine", None)], line_count=3)
    assert len(kept) == 1 and discarded == 0


def test_failures_lists_only_fails() -> None:
    report = Report(
        findings=[
            Finding("closing", "pass", "", None),
            Finding("tool_calls", "fail", "bad", 2),
            Finding("gap_conduct", "n/a", "", None),
        ]
    )
    assert [f.dimension for f in report.failures] == ["tool_calls"]


def test_render_marks_failures_and_notes_discards() -> None:
    report = Report(findings=[Finding("closing", "fail", "signed off twice", 12)], discarded=2)
    rendered = report.render("some scenario")
    assert "closing" in rendered and "FAIL" in rendered and "[12]" in rendered
    assert "2 discarded" in rendered


def test_every_dimension_has_a_question() -> None:
    # The prompt is built from this mapping, so an empty entry would silently ask nothing.
    assert all(question.strip() for question in DIMENSIONS.values())


# --- H1: applicability must reach the brief -------------------------------------------------
# The evaluator failed a CORRECT gate-driven skip because gates were absent from its brief. These
# pin the fix without an LLM: the marker is present, and the "all gated" header appears.


def _plan_with_gate(gate_value: str) -> CallPlan:
    """A two-field task whose second field is gated on the first."""
    return CallPlan(
        schema_name="T",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="t1",
                title="Task One",
                prompt="ask",
                fields=[
                    PlanFieldDescriptor(
                        path="sections.a.trigger", title="Trigger", type="text", role="ask"
                    ),
                    PlanFieldDescriptor(
                        path="sections.a.gated",
                        title="Gated Question",
                        type="text",
                        role="ask",
                        gates=(Comparison(field="sections.a.trigger", op="eq", value=gate_value),),
                    ),
                ],
            )
        ],
    )


def test_a_gated_out_question_is_marked() -> None:
    plan = _plan_with_gate("Yes")
    rendered = render_tasks(plan, {"sections.a.trigger": "No"})
    assert GATED_OUT in rendered
    # The line that carries the marker must be the gated one, not its sibling.
    gated_line = next(line for line in rendered.splitlines() if GATED_OUT in line)
    assert "Gated Question" in gated_line


def test_an_applicable_question_is_not_marked() -> None:
    plan = _plan_with_gate("Yes")
    rendered = render_tasks(plan, {"sections.a.trigger": "Yes"})
    assert GATED_OUT not in rendered


def test_a_fully_gated_task_says_completing_it_is_correct() -> None:
    # This is the male-partner case: every question excluded, so asking nothing is right.
    plan = _plan_with_gate("Yes")
    plan.tasks[0].fields[0].gates = (
        Comparison(field="sections.a.trigger", op="eq", value="never"),
    )
    rendered = render_tasks(plan, {"sections.a.trigger": "No"})
    assert "CORRECT" in rendered.splitlines()[0]


def test_rules_render_note_and_clarify() -> None:
    # `note` and `clarify` state what correct looks like; dropping them left the judge inventing
    # its own standard for good push-back.
    plan = CallPlan(
        schema_name="T",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[PlanTask(task_key="t1", title="T", prompt="p")],
        flow_rules=[
            FlowRule(
                rule_key="stop",
                when=Comparison(field="sections.a.x", op="eq", value="No"),
                action="terminate_call",
                skip_to_task="wrap_up",
                note="Skip everything and close.",
            )
        ],
        contradictions=[
            Contradiction(
                rule_key="conflict",
                when=Comparison(field="sections.a.y", op="eq", value="Yes"),
                fields=["sections.a.y"],
                reason="Those cannot both hold.",
                clarify="Could you double-check that?",
            )
        ],
    )
    rendered = render_rules(plan)
    assert "Skip everything and close." in rendered
    assert "Could you double-check that?" in rendered
    assert "Those cannot both hold." in rendered
