"""Phase 2 execution core: the cascade field-walk and the handoff-target resolver.

Pure — driven only by a CallPlan + the shared answer map, zero schema logic. Routing
runs against the real compiled IBV plan so the three design traversals are exercised
end to end without LiveKit.
"""

from pathlib import Path
from typing import Any

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.planning import CallPlan, PlanField, PlanTask, compile_call_plan
from vera_core.forms.runtime import advance, next_task, normalize_answer

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
V2_DOC: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
PLAN: CallPlan = compile_call_plan(V2_DOC, call_id="c", room_name="r", current_year=2026)

COVERAGE = "sections.benefit_coverage.coverage_type"
SPOUSE_GENDER = "sections.patient_information.spouse_gender"


def _svc_plan() -> CallPlan:
    """One service section: covered gates copay + prior_auth (the cascade in miniature)."""
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
                        "prompt": {"ask": "Covered?"},
                    },
                    "copay": {
                        "type": "currency",
                        "title": "Copay",
                        "role": "ask",
                        "required": True,
                        "applicable_when": {
                            "field": "sections.svc.covered",
                            "op": "eq",
                            "value": "Yes",
                        },
                        "inapplicable_value": "$0",
                        "prompt": {"ask": "Copay?"},
                    },
                    "prior_auth": {
                        "type": "enum",
                        "title": "Prior Auth",
                        "role": "ask",
                        "values": ["Yes", "No", "N/A"],
                        "required": True,
                        "applicable_when": {
                            "field": "sections.svc.covered",
                            "op": "eq",
                            "value": "Yes",
                        },
                        "inapplicable_value": "N/A",
                        "prompt": {"ask": "Prior auth?"},
                    },
                },
            }
        },
        "tasks": [{"task_key": "svc", "title": "Service", "sections": ["svc"]}],
    }
    return compile_call_plan(
        FormSchemaDoc.model_validate(raw), call_id="c", room_name="r", current_year=2026
    )


def _task(plan: CallPlan, key: str) -> PlanTask:
    return next(t for t in plan.tasks if t.task_key == key)


class TestAdvanceCascade:
    def test_first_applicable_field_is_asked(self) -> None:
        plan = _svc_plan()
        answers: dict[str, str] = {}
        field = advance(_task(plan, "svc"), plan, answers)
        assert field is not None and field.field_path == "sections.svc.covered"

    def test_no_collapses_children_to_inapplicable_values(self) -> None:
        plan = _svc_plan()
        answers = {"sections.svc.covered": "No"}
        field = advance(_task(plan, "svc"), plan, answers)
        assert field is None  # nothing left to ask
        assert answers["sections.svc.copay"] == "$0"
        assert answers["sections.svc.prior_auth"] == "N/A"

    def test_yes_opens_the_gated_questions(self) -> None:
        plan = _svc_plan()
        answers = {"sections.svc.covered": "Yes"}
        field = advance(_task(plan, "svc"), plan, answers)
        assert field is not None and field.field_path == "sections.svc.copay"


def _field(**kw: Any) -> PlanField:
    base: dict[str, Any] = {
        "field_path": "f",
        "role": "ask",
        "status": "COLLECT",
        "resolved_prompt": "?",
    }
    return PlanField(**{**base, **kw})


_YESNO = ["Yes", "No", "N/A"]


class TestNormalizeAnswer:
    def test_enum_exact_and_synonym_map_to_canonical(self) -> None:
        f = _field(expected_values=_YESNO)
        assert normalize_answer(f, "Yes") == "Yes"
        assert normalize_answer(f, "yes it's covered") == "Yes"  # the transcription-miss fix
        assert normalize_answer(f, "no") == "No"
        assert normalize_answer(f, "N/A") == "N/A"

    def test_enum_no_match_is_none(self) -> None:
        # Unresolvable enum answer → None so the agent re-prompts instead of mis-gating.
        f = _field(expected_values=_YESNO)
        assert normalize_answer(f, "maybe later") is None
        assert normalize_answer(f, "") is None

    def test_special_values_match(self) -> None:
        f = _field(special_values=["$0", "None"], validation={"range": {"min": 0}})
        assert normalize_answer(f, "$0") == "$0"
        assert normalize_answer(f, "the copay is $0") == "$0"
        assert normalize_answer(f, "None") == "None"

    def test_numeric_range_validation(self) -> None:
        f = _field(special_values=["$0"], validation={"range": {"min": 0}})
        assert normalize_answer(f, "$30") == "$30"  # numeric, in range, no candidate
        assert normalize_answer(f, "-5") is None  # below min
        assert normalize_answer(f, "banana") is None  # no number

    def test_free_text_passthrough(self) -> None:
        f = _field()  # no expected/special/validation → accept trimmed text
        assert normalize_answer(f, "  Blue Cross rep Martha ") == "Blue Cross rep Martha"


def _terminate_plan() -> CallPlan:
    """Three tasks + a pure terminate_call rule (no skip_to_task) gated on eligibility."""
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
                    }
                },
            },
            "detail": {
                "title": "Detail",
                "fields": {
                    "note": {
                        "type": "text",
                        "title": "Note",
                        "role": "ask",
                        "prompt": {"ask": "Note?"},
                    }
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
            {"task_key": "middle", "title": "Middle", "sections": ["detail"]},
            {"task_key": "wrap_up", "title": "Wrap", "sections": ["closing"]},
        ],
        "flow_rules": [
            {
                "rule_key": "ineligible",
                "when": {"field": "sections.elig.eligible", "op": "eq", "value": "No"},
                "action": "terminate_call",
            }
        ],
    }
    return compile_call_plan(
        FormSchemaDoc.model_validate(raw), call_id="c", room_name="r", current_year=2026
    )


class TestTerminateFlowRule:
    def test_terminate_without_skip_routes_to_final_task(self) -> None:
        # A pure terminate_call rule (skip_to_task=None) must still end the interview: route
        # to the final task (wrap_up) so the rep name is captured, not fall through to middle.
        plan = _terminate_plan()
        assert next_task("start", plan, {"sections.elig.eligible": "No"}) == "wrap_up"

    def test_terminate_from_final_task_ends(self) -> None:
        plan = _terminate_plan()
        assert next_task("wrap_up", plan, {"sections.elig.eligible": "No"}) is None

    def test_rule_does_not_fire_when_eligible(self) -> None:
        plan = _terminate_plan()
        assert next_task("start", plan, {"sections.elig.eligible": "Yes"}) == "middle"


class TestNextTaskRouting:
    def test_advances_to_next_task_then_ends_after_last(self) -> None:
        assert next_task("closing_admin", PLAN, {}) == "wrap_up"
        assert next_task("wrap_up", PLAN, {}) is None

    def test_male_partner_skipped_for_individual_plan(self) -> None:
        answers = {COVERAGE: "Individual", SPOUSE_GENDER: "Female"}
        assert next_task("financial", PLAN, answers) == "closing_admin"

    def test_male_partner_runs_for_family_plan_with_male_spouse(self) -> None:
        answers = {COVERAGE: "Family", SPOUSE_GENDER: "Male"}
        assert next_task("financial", PLAN, answers) == "male_partner"

    def test_no_out_of_network_jumps_straight_to_wrap_up(self) -> None:
        answers = {
            "sections.insurance_information.doctor_inside_network": "No",
            "sections.insurance_information.facility_inside_network": "No",
            "sections.insurance_information.out_of_network_coverage": "No",
        }
        assert next_task("insurance_basics", PLAN, answers) == "wrap_up"
