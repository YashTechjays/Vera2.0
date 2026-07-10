"""Runtime condition evaluation + the v2 branches of the intake/review readers.

Uses the real compiled IBV v2 artifact where structure matters (gate chains,
shared_conditions), and small literal documents where a targeted shape is
clearer.
"""

import json
from pathlib import Path
from typing import Any, ClassVar

from vera_core.forms.conditions import (
    evaluate,
    is_applicable,
    is_required,
    is_v2,
    leaf_gates,
)
from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.intake import missing_required, required_intake_fields
from vera_core.forms.review import completion_pct_v2

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"

V2_JSON: dict[str, Any] = json.loads(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
V2_DOC: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
SHARED = V2_DOC.shared_conditions or {}

COVERAGE = "sections.benefit_coverage.coverage_type"
SPOUSE_GENDER = "sections.patient_information.spouse_gender"


def leaf_entry(path: str) -> tuple[Any, tuple[Any, ...]]:
    for p, leaf, gates in leaf_gates(V2_DOC):
        if p == path:
            return leaf, gates
    raise AssertionError(f"no leaf at {path}")


class TestIsV2:
    def test_detects_dsl_2x(self) -> None:
        assert is_v2(V2_JSON) is True
        assert is_v2({"sections": []}) is False
        assert is_v2({"dsl_version": 2}) is False


class TestEvaluate:
    def test_eq_and_missing_value_semantics(self) -> None:
        cond = SHARED["family_coverage"]
        assert evaluate(cond, {COVERAGE: "Family"}, SHARED) is True
        assert evaluate(cond, {COVERAGE: "Individual"}, SHARED) is False
        assert evaluate(cond, {}, SHARED) is False

    def test_nested_ref_and_all(self) -> None:
        cond = SHARED["male_partner_in_scope"]
        values = {COVERAGE: "Family", SPOUSE_GENDER: "Male"}
        assert evaluate(cond, values, SHARED) is True
        assert evaluate(cond, {COVERAGE: "Family"}, SHARED) is False

    def test_not_in_treats_missing_as_empty(self) -> None:
        _leaf, gates = leaf_entry("sections.deductibles.individual.met_amount")
        total = "sections.deductibles.individual.total"
        assert is_applicable(gates, {total: "No Limit"}, SHARED) is False
        assert is_applicable(gates, {total: "$1,500"}, SHARED) is True
        assert is_applicable(gates, {}, SHARED) is True  # unanswered → not_in holds

    def test_non_string_values_compare_as_text(self) -> None:
        cond = SHARED["family_coverage"]
        assert evaluate(cond, {COVERAGE: 5}, SHARED) is False


class TestLeafGates:
    def test_section_gate_reaches_leaves(self) -> None:
        _leaf, gates = leaf_entry("sections.male_partner_coverage.male_partner_covered")
        assert is_applicable(gates, {COVERAGE: "Family"}, SHARED) is False
        assert is_applicable(gates, {COVERAGE: "Family", SPOUSE_GENDER: "Male"}, SHARED) is True

    def test_group_gate_chains_with_leaf_gate(self) -> None:
        copay = "sections.infertility_treatment.intrauterine_insemination.cpt_58323.copay"
        _leaf, gates = leaf_entry(copay)
        covered_gate = "sections.infertility_treatment.infertility_tx_covered"
        row_covered = "sections.infertility_treatment.intrauterine_insemination.cpt_58323.covered"
        assert is_applicable(gates, {covered_gate: "Yes", row_covered: "Yes"}, SHARED) is True
        assert is_applicable(gates, {covered_gate: "No", row_covered: "Yes"}, SHARED) is False

    def test_conditional_requiredness(self) -> None:
        leaf, _gates = leaf_entry("sections.patient_information.spouse_partner_name")
        assert is_required(leaf, {}, SHARED) is False
        assert is_required(leaf, {COVERAGE: "Family"}, SHARED) is True


class TestCompletionPctV2:
    def test_empty_form_is_partially_complete_via_defaults(self) -> None:
        # Required leaves with a declared default (e.g. patient_gender "N/A")
        # count as filled even with no recorded values.
        pct = completion_pct_v2({}, V2_JSON)
        assert 0.0 < pct < 100.0

    def test_full_form_reaches_100(self) -> None:
        # Fill required ∧ applicable leaves to a fixpoint (answering a gate field
        # can make new leaves applicable).
        values: dict[str, Any] = {COVERAGE: "Individual"}
        for _ in range(10):
            changed = False
            for path, leaf, gates in leaf_gates(V2_DOC):
                if values.get(path):
                    continue
                if not is_applicable(gates, values, SHARED):
                    continue
                if not is_required(leaf, values, SHARED):
                    continue
                values[path] = (leaf.values or ["1"])[0]
                changed = True
            if not changed:
                break
        assert completion_pct_v2(values, V2_JSON) == 100.0

    # Small literal doc so the arithmetic is exact: `a` gates `b`; `c` is
    # required but filled by its declared default.
    SMALL: ClassVar[dict[str, Any]] = {
        "dsl_version": "2.1",
        "name": "T",
        "insurance_type": "infertility_treatment",
        "sections": {
            "s": {
                "title": "S",
                "role": "ui_only",
                "fields": {
                    "a": {
                        "type": "enum",
                        "title": "A",
                        "role": "input",
                        "required": True,
                        "values": ["Yes", "No"],
                    },
                    "b": {
                        "type": "text",
                        "title": "B",
                        "role": "input",
                        "required": True,
                        "applicable_when": {
                            "field": "sections.s.a",
                            "op": "eq",
                            "value": "Yes",
                        },
                    },
                    "c": {
                        "type": "text",
                        "title": "C",
                        "role": "input",
                        "required": True,
                        "default": "N/A",
                    },
                },
            }
        },
        "tasks": [],
    }

    def test_applicability_gates_the_denominator(self) -> None:
        # {} → relevant {a, c}; only c (default) filled.
        assert completion_pct_v2({}, self.SMALL) == 50.0
        # a=No → b stays inapplicable; a and c filled.
        assert completion_pct_v2({"sections.s.a": "No"}, self.SMALL) == 100.0
        # a=Yes → b becomes relevant and is unfilled.
        assert completion_pct_v2({"sections.s.a": "Yes"}, self.SMALL) == 66.67


class TestIntakeV2:
    def test_required_intake_fields_matches_system_fields_without_a_default(self) -> None:
        fields = required_intake_fields(V2_JSON)
        assert "sections.patient_information.patient_name" in fields
        assert "sections.patient_information.patient_dob" in fields
        # carries default "N/A" → not intake-blocking even though it's a
        # system_fields target (patient_gender).
        assert "sections.patient_information.patient_gender" not in fields
        # not a system_fields target at all, despite `required: true` +
        # conditional `{when family_coverage}` — a "form filling" concern, not
        # a creation-time one.
        assert "sections.patient_information.spouse_partner_name" not in fields
        # dynamic over the WHOLE document, not just `patient_information`
        assert "sections.hospital_information.npi" in fields
        # role=confirm, but it IS a system_fields target (member_id)
        # and carries no default → still required at creation.
        assert "sections.insurance_information.policy_number" in fields

    def test_missing_required_reports_root_anchored_paths(self) -> None:
        missing = missing_required({"patient_information": {}}, V2_JSON)
        assert "sections.patient_information.patient_name" in missing
        assert all(path.startswith("sections.") for path in missing)

    def test_filled_v2_payload_passes(self) -> None:
        payload = {
            "patient_information": {"patient_name": "Test", "patient_dob": "1990-01-01"},
            "appointment_information": {"appointment_date": "2026-08-03"},
            "insurance_information": {"policy_number": "POL-550411"},
            "insurance_reference_information": {
                "insurance_provider_name": "Demo Health Plan",
                "insurance_phone_number": "+1 555 0100",
            },
            "verification_information": {"verified_by": "Dr. Reyes"},
            "hospital_information": {
                "hospital_name": "Demo Health Partners",
                "hospital_address": "123 Demo St, Austin, TX",
                "tax_id": "987654313",
                "npi": "1234567893",
            },
            "provider_reference_information": {
                "provider_name": "Dr. Jane Smith",
                "npi": "1982736450",
            },
        }
        assert missing_required(payload, V2_JSON) == []

    def test_missing_field_outside_patient_information_is_caught(self) -> None:
        # Regression: `missing_required` used to only inspect `patient_information`,
        # so system fields declared in other sections silently passed even when
        # their section was omitted entirely.
        payload = {
            "patient_information": {"patient_name": "Test", "patient_dob": "1990-01-01"},
            "appointment_information": {"appointment_date": "2026-08-03"},
        }
        assert set(missing_required(payload, V2_JSON)) == {
            "sections.insurance_information.policy_number",
            "sections.insurance_reference_information.insurance_provider_name",
            "sections.insurance_reference_information.insurance_phone_number",
            "sections.verification_information.verified_by",
            "sections.hospital_information.hospital_name",
            "sections.hospital_information.hospital_address",
            "sections.hospital_information.tax_id",
            "sections.hospital_information.npi",
            "sections.provider_reference_information.provider_name",
            "sections.provider_reference_information.npi",
        }
