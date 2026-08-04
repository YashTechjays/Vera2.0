"""Rule engine: flow rules fire once, contradictions re-arm, flow beats contradiction."""

import uuid

from agent_worker.directives import ReAsk, SkipToTask, Terminate
from agent_worker.rule_engine import RuleEngine
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison, Contradiction, FlowRule, NumericConsistency


def _plan(
    *,
    flow_rules: list[FlowRule] | None = None,
    contradictions: list[Contradiction] | None = None,
    numeric_consistencies: list[NumericConsistency] | None = None,
) -> CallPlan:
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(task_key="t1", title="T1", prompt="."),
            PlanTask(task_key="t2", title="T2", prompt="."),
        ],
        flow_rules=flow_rules or [],
        contradictions=contradictions or [],
        numeric_consistencies=numeric_consistencies or [],
    )


def test_terminate_flow_rule_fires_then_is_silent() -> None:
    rule = FlowRule(
        rule_key="not_covered",
        when=Comparison(field="sections.a.covered", op="eq", value="No"),
        action="terminate_call",
    )
    engine = RuleEngine(_plan(flow_rules=[rule]))
    assert engine.evaluate({"sections.a.covered": "No"}) == Terminate(rule_key="not_covered")
    # fire-once: the same holding condition does not re-fire
    assert engine.evaluate({"sections.a.covered": "No"}) is None


def test_skip_flow_rule_returns_skip_to_task() -> None:
    rule = FlowRule(
        rule_key="jump",
        when=Comparison(field="sections.a.oon", op="eq", value="Yes"),
        action="terminate_call",
        skip_to_task="t2",
    )
    engine = RuleEngine(_plan(flow_rules=[rule]))
    assert engine.evaluate({"sections.a.oon": "Yes"}) == SkipToTask(rule_key="jump", task_key="t2")


def test_flow_rule_does_not_fire_until_condition_holds() -> None:
    rule = FlowRule(
        rule_key="not_covered",
        when=Comparison(field="sections.a.covered", op="eq", value="No"),
        action="terminate_call",
    )
    engine = RuleEngine(_plan(flow_rules=[rule]))
    assert engine.evaluate({}) is None  # unanswered → no fire, no crash
    assert engine.evaluate({"sections.a.covered": "Yes"}) is None
    assert engine.evaluate({"sections.a.covered": "No"}) == Terminate(rule_key="not_covered")


def test_contradiction_fires_and_rearms_on_new_values() -> None:
    contradiction = Contradiction(
        rule_key="ded_conflict",
        when=Comparison(field="sections.a.ded_met", op="eq", value="Yes"),
        fields=["sections.a.ded_met", "sections.a.ded_remaining"],
        reason="Deductible is met but a remaining balance was given.",
        clarify="Which is correct?",
    )
    engine = RuleEngine(_plan(contradictions=[contradiction]))
    first = engine.evaluate({"sections.a.ded_met": "Yes", "sections.a.ded_remaining": "500"})
    assert first == ReAsk(
        rule_key="ded_conflict",
        reason="Deductible is met but a remaining balance was given.",
        clarify="Which is correct?",
    )
    # same conflicting values → do not badger the rep again
    assert engine.evaluate({"sections.a.ded_met": "Yes", "sections.a.ded_remaining": "500"}) is None
    # a genuinely new conflicting combination re-arms the push-back
    refired = engine.evaluate({"sections.a.ded_met": "Yes", "sections.a.ded_remaining": "800"})
    assert refired is not None


def test_flow_rule_beats_contradiction_in_the_same_pass() -> None:
    flow = FlowRule(
        rule_key="term",
        when=Comparison(field="sections.a.covered", op="eq", value="No"),
        action="terminate_call",
    )
    contradiction = Contradiction(
        rule_key="c",
        when=Comparison(field="sections.a.covered", op="eq", value="No"),
        fields=["sections.a.covered"],
        reason="r",
    )
    engine = RuleEngine(_plan(flow_rules=[flow], contradictions=[contradiction]))
    assert engine.evaluate({"sections.a.covered": "No"}) == Terminate(rule_key="term")


LTM = "sections.lifetime_maximum"
LTM_RULE = NumericConsistency(
    rule_key="ltm_consistency", triplet=LTM, clarify="Could you double-check those amounts?"
)


def test_numeric_consistency_fires_reask_with_dynamic_reason() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    directive = engine.evaluate(
        {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    )
    assert isinstance(directive, ReAsk)
    assert directive.rule_key == "ltm_consistency"
    assert "$300.00" in directive.reason and "$100.00" in directive.reason
    assert directive.clarify == "Could you double-check those amounts?"


def test_numeric_consistency_silent_on_consistent_or_partial_values() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    assert engine.evaluate({}) is None
    assert engine.evaluate({f"{LTM}.total": "$25,000"}) is None
    consistent = {
        f"{LTM}.total": "$25,000",
        f"{LTM}.met_amount": "$5,000",
        f"{LTM}.remaining": "$20,000",
    }
    assert engine.evaluate(consistent) is None
    no_limit = {
        f"{LTM}.total": "No Limit",
        f"{LTM}.met_amount": "$300",
        f"{LTM}.remaining": "$300",
    }
    assert engine.evaluate(no_limit) is None


def test_numeric_consistency_rearms_on_new_values_only() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    bad = {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    assert engine.evaluate(bad) is not None
    # same impossible combination restated → do not badger the rep again
    assert engine.evaluate(bad) is None
    # a genuinely new impossible combination re-arms the push-back
    still_bad = dict(bad, **{f"{LTM}.met_amount": "$500"})
    assert engine.evaluate(still_bad) is not None


def test_contradiction_beats_numeric_consistency_in_the_same_pass() -> None:
    contradiction = Contradiction(
        rule_key="c",
        when=Comparison(field=f"{LTM}.total", op="eq", value="$100"),
        fields=[f"{LTM}.total"],
        reason="r",
    )
    engine = RuleEngine(_plan(contradictions=[contradiction], numeric_consistencies=[LTM_RULE]))
    directive = engine.evaluate(
        {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    )
    assert directive == ReAsk(rule_key="c", reason="r", clarify=None)
