"""Phase 2 execution core: the cascade field-walk and the handoff-target resolver.

Pure — driven only by a CallPlan + the shared answer map, zero schema logic. Routing
runs against the real compiled IBV plan so the three design traversals are exercised
end to end without LiveKit.
"""

from pathlib import Path
from typing import Any

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.planning import CallPlan, PlanTask, compile_call_plan
from vera_core.forms.runtime import advance, next_task

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
