"""Runtime condition evaluation + the v2 branches of the intake/review readers.

Uses the real compiled IBV v2 artifact where structure matters (gate chains,
shared_conditions), and small literal documents where a targeted shape is
clearer.
"""

import json
from pathlib import Path
from typing import Any, ClassVar

from vera_core.forms.conditions import (
    alternative_fills,
    alternative_index,
    alternative_pairs,
    evaluate,
    is_applicable,
    is_required,
    is_satisfied,
    is_v2,
    leaf_gates,
    routing_branch_fills,
)
from vera_core.forms.dsl import FormSchemaDoc, PromotedFields, load_document
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
        "system_fields": {"a": "sections.s.a"},
        "promoted_fields": dict.fromkeys(PromotedFields.model_fields, "sections.s.a"),
        "rep_call_reference_number_field": "sections.s.a",
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
        # a, b, c are all role="input" — none is askable, so `relevant` is empty in every
        # case regardless of applicability, and `completion_pct_v2` takes the `not relevant`
        # branch every time (a real semantic change: a call can fill none of these, so the
        # form reads as vacuously complete rather than partially filled).
        assert completion_pct_v2({}, self.SMALL) == 100.0
        assert completion_pct_v2({"sections.s.a": "No"}, self.SMALL) == 100.0
        assert completion_pct_v2({"sections.s.a": "Yes"}, self.SMALL) == 100.0


class TestIntakeV2:
    def test_required_intake_fields_matches_system_fields_without_a_default(self) -> None:
        fields = required_intake_fields(V2_JSON)
        assert "sections.patient_information.patient_name" in fields
        assert "sections.patient_information.patient_dob" in fields
        assert "sections.patient_information.patient_gender" in fields
        assert "sections.patient_information.chart_number" in fields
        assert "sections.verification_information.callback_number" in fields
        # carries default "N/A" → not intake-blocking even though it's a
        # system_fields target (appointment_type; the one deliberate exception).
        assert "sections.appointment_information.appointment_type" not in fields
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
            "patient_information": {
                "chart_number": "CH-10293",
                "patient_name": "Test",
                "patient_dob": "1990-01-01",
                "patient_gender": "Female",
            },
            "appointment_information": {"appointment_date": "2026-08-03"},
            "insurance_information": {"policy_number": "POL-550411"},
            "insurance_reference_information": {
                "insurance_provider_name": "Demo Health Plan",
                "insurance_phone_number": "+1 555 0100",
            },
            "verification_information": {
                "verified_by": "Dr. Reyes",
                "callback_number": "+1 555 0199",
            },
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
            "sections.patient_information.patient_gender",
            "sections.patient_information.chart_number",
            "sections.insurance_information.policy_number",
            "sections.insurance_reference_information.insurance_provider_name",
            "sections.insurance_reference_information.insurance_phone_number",
            "sections.verification_information.verified_by",
            "sections.verification_information.callback_number",
            "sections.hospital_information.hospital_name",
            "sections.hospital_information.hospital_address",
            "sections.hospital_information.tax_id",
            "sections.hospital_information.npi",
            "sections.provider_reference_information.provider_name",
            "sections.provider_reference_information.npi",
        }


PAIR = ("sections.s.cpt_1.copay", "sections.s.cpt_1.coinsurance")
OTHER_PAIR = ("sections.s.cpt_2.copay", "sections.s.cpt_2.coinsurance")
INDEX = alternative_index([PAIR, OTHER_PAIR])


class TestIsSatisfied:
    """The one owed-set rule shared by the gap sweep, both completion guards and the form's
    completion percentage. Satisfaction, never applicability — see the both-answered case."""

    def test_a_sibling_answer_satisfies_the_empty_side(self) -> None:
        values = {PAIR[1]: "30"}
        assert is_satisfied(PAIR[0], None, values, INDEX)
        assert is_satisfied(PAIR[1], None, values, INDEX)

    def test_neither_answered_owes_both(self) -> None:
        assert not is_satisfied(PAIR[0], None, {}, INDEX)
        assert not is_satisfied(PAIR[1], None, {}, INDEX)

    def test_one_codes_answer_does_not_satisfy_another_code(self) -> None:
        # `panel_cost_pairs` flattens every code into ONE authored set; treating the whole set as
        # satisfied would mark eight codes answered off one reply.
        values = {PAIR[0]: "$25"}
        assert not is_satisfied(OTHER_PAIR[0], None, values, INDEX)
        assert not is_satisfied(OTHER_PAIR[1], None, values, INDEX)

    def test_a_declared_default_owes_nothing(self) -> None:
        assert is_satisfied("sections.s.group_name", "N/A", {}, INDEX)
        assert not is_satisfied("sections.s.group_name", None, {}, INDEX)

    def test_blank_and_whitespace_are_not_answers(self) -> None:
        for blank in ("", "   ", None):
            assert not is_satisfied(PAIR[0], None, {PAIR[1]: blank}, INDEX), repr(blank)

    def test_a_field_in_no_pair_is_unaffected(self) -> None:
        assert not is_satisfied("sections.s.lonely", None, {PAIR[0]: "$25"}, INDEX)


class TestAlternativePairsFromTheRealSchema:
    def test_the_flattened_diagnostic_set_yields_one_pair_per_code(self) -> None:
        pairs = alternative_pairs(V2_DOC)
        diagnostic = [p for p in pairs if "labs_xray_ultrasound" in p[0]]
        assert len(diagnostic) == 8
        assert all(len(p) == 2 for p in diagnostic)

    def test_routing_alternatives_over_groups_are_excluded(self) -> None:
        flat = {path for pair in alternative_pairs(V2_DOC) for path in pair}
        assert all(p.rsplit(".", 1)[1] in {"copay", "coinsurance"} for p in flat)


_IUI = "sections.infertility_treatment.intrauterine_insemination.cpt_58323"
_CRYO = "sections.infertility_treatment.embryo_cryopreservation.cpt_89342"


class TestAlternativeFills:
    """The export is the final product, so the unused side of an either/or must read $0 / 0%
    rather than blank. Values come from the leaf's own authored `inapplicable_value`."""

    def test_answering_coinsurance_fills_the_copay(self) -> None:
        fills = alternative_fills(V2_DOC, {f"{_IUI}.coinsurance": "30"}, f"{_IUI}.coinsurance")
        assert fills == {f"{_IUI}.copay": "$0"}

    def test_answering_copay_fills_the_coinsurance(self) -> None:
        fills = alternative_fills(V2_DOC, {f"{_IUI}.copay": "$25"}, f"{_IUI}.copay")
        assert fills == {f"{_IUI}.coinsurance": "0%"}

    def test_a_sibling_that_already_has_a_value_is_never_overwritten(self) -> None:
        values = {f"{_IUI}.copay": "$25", f"{_IUI}.coinsurance": "30"}
        assert alternative_fills(V2_DOC, values, f"{_IUI}.coinsurance") == {}

    def test_it_fills_only_the_answered_code_not_its_panel_siblings(self) -> None:
        # The authored set spans three IUI codes; one reply must not fill the other two.
        fills = alternative_fills(V2_DOC, {f"{_IUI}.copay": "$25"}, f"{_IUI}.copay")
        assert set(fills) == {f"{_IUI}.coinsurance"}

    def test_a_blank_answer_fills_nothing(self) -> None:
        assert alternative_fills(V2_DOC, {f"{_IUI}.copay": "   "}, f"{_IUI}.copay") == {}

    def test_a_field_in_no_pair_fills_nothing(self) -> None:
        path = "sections.insurance_representative.rep_name"
        assert alternative_fills(V2_DOC, {path: "Martha"}, path) == {}

    def test_no_fill_opens_a_gated_field(self) -> None:
        # storage_time_coverage exists only when cpt_89342.covered is "Yes"; a cost fill must
        # never flip that gate. Filling the cost pair leaves it shut.
        values = {f"{_CRYO}.covered": "Yes", f"{_CRYO}.coinsurance": "30"}
        fills = alternative_fills(V2_DOC, values, f"{_CRYO}.coinsurance")
        assert fills == {f"{_CRYO}.copay": "$0"}
        assert "storage_time_coverage" not in " ".join(fills)


_ELECTIVE = "sections.infertility_treatment.egg_cryopreservation_elective.cpt_89337"
_CANCER = "sections.infertility_treatment.egg_cryopreservation_cancer.cpt_89337"
_ASC_PRO = "sections.general_coverage.asc_professional.cpt_58555"
_ASC_FAC = "sections.general_coverage.asc_facility.cpt_58555"


class TestRoutingBranchFills:
    """A routing `alternatives` gates none of its branches, so the untaken one stays owed forever
    and blocks auto-completion. `N/A` closes it, and the cost-sharing cascade follows."""

    def test_the_untaken_asc_branch_is_marked_not_applicable(self) -> None:
        fills = routing_branch_fills(V2_DOC, {f"{_ASC_FAC}.covered": "Yes"})
        assert fills == {f"{_ASC_PRO}.covered": "N/A"}

    def test_it_works_the_other_way_round(self) -> None:
        fills = routing_branch_fills(V2_DOC, {f"{_ASC_PRO}.covered": "Yes"})
        assert fills == {f"{_ASC_FAC}.covered": "N/A"}

    def test_both_branches_answered_fills_nothing(self) -> None:
        # ASC professional and facility genuinely both applying is not unusual — observed on a
        # live call — and must never be overwritten.
        values = {f"{_ASC_PRO}.covered": "Yes", f"{_ASC_FAC}.covered": "Yes"}
        assert routing_branch_fills(V2_DOC, values) == {}

    def test_neither_answered_fills_nothing(self) -> None:
        assert routing_branch_fills(V2_DOC, {}) == {}

    def test_the_untaken_egg_cryo_branch_gets_na_not_no(self) -> None:
        # The live-call defect: the Observer wrote `No` here at confidence 90, asserting the plan
        # does not cover elective egg cryopreservation, for a service never discussed.
        values = {
            "sections.infertility_treatment.infertility_tx_covered": "Yes",
            f"{_CANCER}.covered": "Yes",
        }
        fills = routing_branch_fills(V2_DOC, values)
        assert fills.get(f"{_ELECTIVE}.covered") == "N/A"

    def test_cost_sharing_is_left_to_the_gate_cascade(self) -> None:
        # copay/coinsurance/prior_auth are gated on `covered == "Yes"`, so filling covered=N/A
        # keeps them inapplicable — never owed, never written.
        fills = routing_branch_fills(V2_DOC, {f"{_ASC_FAC}.covered": "Yes"})
        assert not any(p.endswith((".copay", ".coinsurance", ".prior_auth")) for p in fills)
