"""Prompt compilation: schema document → per-task composite_json."""

from pathlib import Path
from typing import Any

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.prompting import compile_prompt_document

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"

IBV: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
COMPOSITE: dict[str, Any] = compile_prompt_document(IBV)


def task(key: str) -> dict[str, Any]:
    return next(t for t in COMPOSITE["tasks"] if t["task_key"] == key)


class TestCompositeShape:
    def test_one_nested_entry_per_task_in_document_order(self) -> None:
        assert [t["task_key"] for t in COMPOSITE["tasks"]] == [t.task_key for t in IBV.tasks]
        assert COMPOSITE["generated_from"] == "form_schema"
        assert COMPOSITE["name"] == "Infertility"

    def test_task_level_prompt_is_carried(self) -> None:
        for entry in COMPOSITE["tasks"]:
            assert entry["prompt"], f"task {entry['task_key']} lost its prompt"
        assert "spouse" in task("insurance_basics")["prompt"]

    def test_sections_nest_their_question_lists(self) -> None:
        basics = task("insurance_basics")
        insurance = next(
            s for s in basics["sections"] if s["section_key"] == "insurance_information"
        )
        by_path = {q["field_path"]: q for q in insurance["questions"]}
        plan_type = by_path["sections.insurance_information.plan_type"]
        assert plan_type["question"].startswith("What type of plan")
        assert plan_type["special_values"] == ["PPO", "HMO", "EPO", "POS"]
        assert plan_type["required"] is True
        # ask_groups ride along as the combined-question overlay
        assert any(
            "plan_type" in member for group in insurance["ask_groups"] for member in group["fields"]
        )

    def test_questions_carry_gates_as_skip_conditions(self) -> None:
        financial = task("financial")
        deductibles = next(s for s in financial["sections"] if s["section_key"] == "deductibles")
        met = next(
            q
            for q in deductibles["questions"]
            if q["field_path"] == "sections.deductibles.individual.met_amount"
        )
        assert any("not_in" in str(gate.values()) for gate in met["skip_unless"])

    def test_confirm_in_task_fields_attach_to_their_task_end(self) -> None:
        basics = task("insurance_basics")
        paths = [q["field_path"] for q in basics["confirm_at_end"]]
        assert "sections.patient_information.spouse_partner_name" in paths
        assert "sections.patient_information.spouse_partner_dob" in paths
        # and they are NOT duplicated into any section question list
        all_section_questions = [
            q["field_path"]
            for t in COMPOSITE["tasks"]
            for s in t["sections"]
            for q in s["questions"]
        ]
        assert "sections.patient_information.spouse_partner_name" not in all_section_questions

    def test_context_fields_form_the_known_background_block(self) -> None:
        paths = {c["field_path"] for c in COMPOSITE["context_fields"]}
        assert "sections.patient_information.patient_name" in paths
        assert "sections.hospital_information.npi" in paths
        # input/readonly leaves are no-ops — never in the prompt
        assert "sections.form_information.practice" not in paths

    def test_date_format_nuance_reaches_the_questions(self) -> None:
        coverage = task("insurance_basics")
        benefit = next(s for s in coverage["sections"] if s["section_key"] == "benefit_coverage")
        effective_date = next(
            q
            for q in benefit["questions"]
            if q["field_path"] == "sections.benefit_coverage.plan_effective_date"
        )
        assert effective_date["validation"]["date_format"] == "M/D/YYYY"

    def test_rules_ride_along(self) -> None:
        assert COMPOSITE["flow_rules"][0]["action"] == "terminate_call"
        assert {c["rule_key"] for c in COMPOSITE["contradictions"]} == {
            "small_group_self_insured_conflict",
            "mandate_requires_infertility_coverage",
        }
        assert "family_coverage" in COMPOSITE["shared_conditions"]


class TestDiseaseOnlyComposite:
    def test_compiles_for_every_catalog_schema(self) -> None:
        doc = load_document(
            (FORM_SCHEMA_DIR / "disease_only_verification.json").read_text(encoding="utf-8")
        )
        composite = compile_prompt_document(doc)
        assert len(composite["tasks"]) == len(doc.tasks)
        assert all(t["prompt"] for t in composite["tasks"])
