"""Compiler tests against the REAL seeded schema (`ibv_form_standard.json`) —
the compile contract is exercised on the artifact production runs, plus small
synthetic schemas for the failure modes."""

import json
from pathlib import Path
from typing import Any

import pytest

from vera_core.callplan import (
    CompileError,
    RuleEffect,
    compile_call_plan,
)
from vera_core.schemas import PersonaTweak

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "form_schemas" / "ibv_form_standard.json"
)

_PREFILL = {
    "patient_information.patient_name": "Jane Doe",
    "patient_information.patient_dob": "1990-02-03",
    "insurance_information.policy_number": "ABC123456",
    "benefit_coverage.coverage_type": "Family",
}


@pytest.fixture(scope="module")
def schema_json() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())
    return loaded


@pytest.fixture(scope="module")
def plan(schema_json: dict[str, Any]) -> Any:
    return compile_call_plan(
        schema_json,
        _PREFILL,
        PersonaTweak(extra_instructions="Tenant extra line.", greeting="Tenant hello."),
        room_name="call--t--c",
        tenant_id="t",
        call_id="c",
        schema_version_id="sv",
        prompt_version_id="pv",
        context_values={"clinic_name": "Acme Fertility", "verified_by": "Vera"},
    )


class TestRealSchemaCompiles:
    def test_every_section_compiles_with_a_phase_or_context(self, plan: Any) -> None:
        assert len(plan.sections) == 21
        for section in plan.sections:
            if section.mode == "context":
                assert section.instructions == ""
            else:
                assert section.phase_key
                assert section.instructions  # every recipe ref resolved

    def test_context_sections_are_the_pre_provided_six(self, plan: Any) -> None:
        context = {s.section_key for s in plan.sections if s.mode == "context"}
        assert context == {
            "patient_information",
            "appointment_information",
            "verification_information",
            "hospital_information",
            "provider_reference_information",
            "form_information",
        }

    def test_nested_object_fields_flatten_with_group(self, plan: Any) -> None:
        diag = next(s for s in plan.sections if s.section_key == "diagnostic_labs_xray_ultrasound")
        paths = {f.field_path for f in diag.fields}
        assert "diagnostic_labs_xray_ultrasound.diagnostic_testing.covered" in paths
        group_keys = {g.group_key for g in diag.groups}
        assert "diagnostic_labs_xray_ultrasound.diagnostic_testing" in group_keys
        for field in diag.fields:
            assert field.group_key in group_keys

    def test_full_key_carry_through(self, plan: Any) -> None:
        """The user-visible contract: verbatim prompts, CPT/ICD metadata, rules
        and policies all survive compilation (not just the confirm pair)."""
        all_fields = [f for s in plan.sections for f in s.fields]
        all_groups = [g for s in plan.sections for g in s.groups]
        assert any(f.verbatim_prompt for f in all_fields)
        ivf = next(g for g in all_groups if "in_vitro" in g.group_key)
        assert ivf.metadata is not None and "58970" in ivf.metadata.cpt_codes
        assert ivf.metadata.icd10 == "Z31.83"
        assert any(f.policies for f in all_fields)  # after_answer checkpoints
        assert any(f.rules for f in all_fields)  # undecided rules ride along

    def test_metadata_renders_into_instructions_once(self, plan: Any) -> None:
        treatment = next(s for s in plan.sections if s.section_key == "infertility_treatment")
        assert treatment.instructions.count("<spell>58970</spell>") == 1

    def test_confirm_fields_carry_raw_values(self, plan: Any) -> None:
        confirm = {f.field_path: f for s in plan.sections for f in s.fields if f.mode == "confirm"}
        assert confirm["insurance_information.policy_number"].confirm_value == "ABC123456"
        assert confirm["patient_information.patient_name"].confirm_value == "Jane Doe"

    def test_prefilled_values_flow_into_the_plan(self, plan: Any) -> None:
        """PHI tokenization was removed: the raw prefilled values now DO appear in
        the serialized plan (as confirm values). This is the intended behavior —
        synthetic-data-only, see adr/devops-todo.md #8."""
        blob = plan.model_dump_json()
        for value in _PREFILL.values():
            assert value in blob

    def test_compile_time_rule_resolution(self, plan: Any) -> None:
        """coverage_type=Family is prefilled → the spouse fields' `make this
        required` rule resolves now: required=True and the rule is dropped."""
        patient = next(s for s in plan.sections if s.section_key == "patient_information")
        spouse = next(f for f in patient.fields if f.field_path.endswith("spouse_partner_name"))
        assert spouse.required is True
        assert spouse.rules == []

    def test_undecidable_rules_stay_in_plan(self, plan: Any) -> None:
        """out_of_network_coverage's terminate rule depends on in-call answers —
        it must ride into the plan for the runtime."""
        insurance = next(s for s in plan.sections if s.section_key == "insurance_information")
        oon = next(f for f in insurance.fields if f.field_path.endswith("out_of_network_coverage"))
        assert any(r.effect is RuleEffect.TERMINATE_CALL_WHEN for r in oon.rules)

    def test_tweak_and_markup_guide_appended(self, plan: Any) -> None:
        assert plan.greeting == "Tenant hello."
        assert plan.flat_instructions.rstrip().endswith("Never wrap a tool call in a tag.")
        assert "Tenant extra line." in plan.flat_instructions

    def test_flat_instructions_cover_all_collect_sections(self, plan: Any) -> None:
        for section in plan.sections:
            if section.mode != "context":
                assert f'<section name="{section.section_key}">' in plan.flat_instructions


class TestFailClosed:
    def _minimal(self) -> dict[str, Any]:
        return {
            "constraint_library": {},
            "global_policies": [
                {"title": "P", "source": "x.py:AGENT_PERSONA", "exact_text": "persona"}
            ],
            "phase_order": {"phase_1": ["AGENT_PERSONA", "<SECTIONS>"]},
            "sections": [
                {
                    "section_key": "s1",
                    "title": "S1",
                    "phase_key": "phase_1",
                    "properties": {"f1": {"type": "string", "title": "F1"}},
                }
            ],
        }

    def _compile(self, schema: dict[str, Any]) -> Any:
        return compile_call_plan(
            schema,
            {},
            None,
            room_name="r",
            tenant_id="t",
            call_id="c",
            schema_version_id="sv",
        )

    def test_minimal_schema_compiles(self) -> None:
        plan = self._compile(self._minimal())
        assert plan.sections[0].instructions.startswith("persona")

    def test_dangling_recipe_ref_raises(self) -> None:
        schema = self._minimal()
        schema["phase_order"]["phase_1"].append("NO_SUCH_FRAGMENT")
        with pytest.raises(CompileError, match="NO_SUCH_FRAGMENT"):
            self._compile(schema)

    def test_missing_phase_key_raises(self) -> None:
        schema = self._minimal()
        del schema["sections"][0]["phase_key"]
        with pytest.raises(CompileError, match="phase_key"):
            self._compile(schema)

    def test_unknown_constraint_ref_raises(self) -> None:
        schema = self._minimal()
        schema["sections"][0]["properties"]["f1"]["constraint_ref"] = "NOPE"
        with pytest.raises(CompileError, match="NOPE"):
            self._compile(schema)

    def test_unknown_rule_effect_raises(self) -> None:
        schema = self._minimal()
        schema["sections"][0]["properties"]["f1"]["rules"] = [{"effect": "explode"}]
        with pytest.raises(CompileError, match="explode"):
            self._compile(schema)

    def test_no_phase_order_raises(self) -> None:
        schema = self._minimal()
        del schema["phase_order"]
        with pytest.raises(CompileError, match="phase_order"):
            self._compile(schema)

    def test_confirm_only_without_prefill_stays_ask(self) -> None:
        """Fail-safe: never read back a value we do not hold."""
        schema = self._minimal()
        schema["sections"][0]["properties"]["f1"]["confirm_only"] = True
        plan = self._compile(schema)
        assert plan.sections[0].fields[0].mode == "ask"
