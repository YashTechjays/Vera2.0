"""Does the evaluator OBEY the recorded-facts block when it contradicts the conversation?

This guards the H4 defect: the judge used to conclude "the flow rule fired" from a call that merely
ended early, and PASSED a rule that never ran. A false pass hides exactly the defects this harness
exists to find, so the property is worth its own test.

A normal scenario run cannot cover this. It only exercises the fact block when the rule genuinely
fired, so the contradiction never arises. Here it is constructed deliberately: a transcript that
looks like a textbook short-circuit, paired with facts saying nothing fired.

One LLM call — cheap next to a full call replay — but still `evals`-marked, so `just check` never
collects it.
"""

import os

import pytest
from conftest import build_evaluator
from judge import render_facts

pytestmark = [
    pytest.mark.evals,
    pytest.mark.skipif(
        not os.getenv("VERA_EVALS_ENABLED"),
        reason="set VERA_EVALS_ENABLED=1 (needs Vertex ADC)",
    ),
]

# Reads as a correct short-circuit: the rep reports an inactive policy, VERA collects the closing
# details and wraps up. Nothing here hints that the rule engine sat idle.
_TRANSCRIPT = "\n".join(
    f"[{i:3d}] {line}"
    for i, line in enumerate(
        [
            "REP  : Yes, that matches.",
            "VERA : Can you confirm the patient's insurance is currently active?",
            "REP  : No, the insurance is not active; it terminated December 31, 2025.",
            "VERA : Thanks so much for your patience — that covers everything on my list.",
            "VERA : May I have your first name and last name initial?",
            "REP  : Martha R.",
            "TOOL : task_complete",
            ">>>> HANDOFF PlanTaskAgent -> WrapUpAgent",
        ]
    )
)

_RULES = (
    "- flow rule `insurance_not_active`: when "
    '`sections.patient_verification.is_insurance_active` is "No" -> VERA must skip ahead to task '
    "`wrap_up`\n"
    "    intent: Skip all remaining tasks, collect the representative name and call reference "
    "number, then end the call."
)


async def test_the_judge_trusts_recorded_facts_over_the_conversation() -> None:
    report = await build_evaluator().evaluate(
        _TRANSCRIPT,
        rules=_RULES,
        tasks="1. `wrap_up` (Wrap Up)",
        # The contradiction: the transcript suggests the rule worked, the facts say it never ran.
        facts=render_facts([], answers_extracted=0),
    )
    flow = next((f for f in report.findings if f.dimension == "flow_rules"), None)
    assert flow is not None, "the evaluator returned no flow_rules verdict"
    assert flow.verdict == "fail", (
        "the judge inferred rule firing from the conversation instead of the fact block "
        f"(got {flow.verdict!r}: {flow.reason})"
    )
