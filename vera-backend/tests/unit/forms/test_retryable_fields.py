import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from vera_core.forms.conditions import alternative_pairs
from vera_core.forms.dsl import FormSchemaDoc, PromotedFields
from vera_core.forms.intake import required_intake_fields
from vera_core.forms.review import (
    REVIEW_CONFIDENCE_FLOOR,
    FieldStatus,
    _confirm_paths,
    field_labels,
    is_field_satisfied,
    retryable_required_paths,
    unsatisfied_required_paths,
)
from vera_core.models.enums import AnswerSource

FLOOR = 70

# Minimal v2 schema: one required askable leaf + one required readonly leaf.
V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    # The DSL requires every promoted column mapped to a system_fields target —
    # point them all at one leaf (same shortcut as test_conditions.py).
    "system_fields": {"network_status": "sections.cov.network_status"},
    "rep_call_reference_number_field": "sections.cov.network_status",
    "promoted_fields": dict.fromkeys(PromotedFields.model_fields, "sections.cov.network_status"),
    "sections": {
        "cov": {
            "title": "Coverage",
            "role": "collect",
            "fields": {
                "network_status": {
                    "type": "text",
                    "title": "Network status",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is the network status?"},
                },
                "plan_name": {
                    "type": "text",
                    "title": "Plan name",
                    "role": "readonly",
                    "required": True,
                },
            },
        },
    },
    "tasks": [
        {
            "task_key": "t1",
            "title": "Task 1",
            "sections": ["cov"],
        }
    ],
}


def _ai(conf: int, sup: bool = True) -> FieldStatus:
    return FieldStatus(source="ai_call", ai_supported=sup, ai_confidence=conf)


def _human() -> FieldStatus:
    return FieldStatus(source="human", ai_supported=None, ai_confidence=None)


def test_is_field_satisfied_rules() -> None:
    assert is_field_satisfied(_human(), floor=FLOOR) is True  # trusted
    assert is_field_satisfied(_ai(90), floor=FLOOR) is True  # ai supported, >=70
    assert is_field_satisfied(_ai(60), floor=FLOOR) is False  # ai <70
    assert is_field_satisfied(_ai(90, sup=False), floor=FLOOR) is False  # unsupported
    assert is_field_satisfied(None, floor=FLOOR) is False  # unfilled (no status)
    # Unjudged: load_field_status yields ai_supported=None for an answer with no evaluation.
    # This is the accepted cost of the Observer recording confirmations — an intake value the
    # rep confirms becomes ai_call and so judge-conditional. Invisible on the normal path
    # (evaluate_call judges before computing `unsatisfied`, one transaction), but PERMANENT on
    # the fallback path, where no judge ever runs.
    assert is_field_satisfied(FieldStatus("ai_call", None, 95), floor=FLOOR) is False


def test_a_confirmed_intake_value_becomes_unsatisfied_when_no_judge_runs() -> None:
    """The isolated `is_field_satisfied` pin above does not reach the consequence: the field
    lands back in `unsatisfied_required_paths`, which is the auto-complete gate. So a rep
    merely repeating a value intake already held can move a form from "nothing outstanding"
    to "outstanding" on the no-judge path.

    Accepted trade-off, decided with the product owner (spec §3.5, "Accepted regression:
    `ask`-role intake fields become judge-conditional") — pinned at the level where it bites,
    not to reargue it. If this ever needs to change, the spec changes with it.
    """
    path = "sections.cov.network_status"
    intake = FieldStatus(source=AnswerSource.INTAKE.value, ai_supported=None, ai_confidence=None)
    # `unsatisfied_required_paths` spans every role, so the readonly leaf is filled too —
    # otherwise it masks the one field under test.
    on_file = {path: intake, "sections.cov.plan_name": intake}
    assert unsatisfied_required_paths(on_file, V2, floor=FLOOR) == []
    # Same value, now owned by the call that confirmed it, with no evaluation row behind it.
    confirmed_unjudged = FieldStatus(
        source=AnswerSource.AI_CALL.value, ai_supported=None, ai_confidence=95
    )
    assert unsatisfied_required_paths({**on_file, path: confirmed_unjudged}, V2, floor=FLOOR) == [
        path
    ]


def test_retryable_only_unsatisfied_askable_required() -> None:
    p = "sections.cov.network_status"
    # unfilled (absent from the status map) required askable -> retryable
    assert retryable_required_paths({}, V2, floor=FLOOR) == [p]
    # low-conf ai_call required askable -> retryable
    assert retryable_required_paths({p: _ai(50)}, V2, floor=FLOOR) == [p]
    # satisfied -> not retryable
    assert retryable_required_paths({p: _ai(90)}, V2, floor=FLOOR) == []
    # readonly required field never retryable even if unfilled (not askable)
    assert "sections.cov.plan_name" not in retryable_required_paths({p: _ai(90)}, V2, floor=FLOOR)


def test_field_labels_uses_titles() -> None:
    assert field_labels(V2, ["sections.cov.network_status"]) == ["Network status"]


# ---------------------------------------------------------------------------
# The authoritative completeness check (unsatisfied_required_paths) vs the
# retry-nudge list (retryable_required_paths).
# ---------------------------------------------------------------------------


def test_unsatisfied_includes_non_askable_required_fields() -> None:
    """A required readonly leaf that is unfilled blocks completion (it routes to
    review) even though a retry call can never ask for it."""
    status = {"sections.cov.network_status": _human()}  # plan_name unfilled
    unsatisfied = unsatisfied_required_paths(status, V2, floor=FLOOR)
    assert unsatisfied == ["sections.cov.plan_name"]
    # ...while the retry list correctly excludes it (nothing askable is missing).
    assert retryable_required_paths(status, V2, floor=FLOOR) == []


def _gated_schema() -> dict[str, Any]:
    """`copay` is required only when network_status == "In Network" — a
    value-comparing gate the sentinel approximation cannot see."""
    import copy

    doc = cast("dict[str, Any]", copy.deepcopy(V2))
    fields = doc["sections"]["cov"]["fields"]
    fields["copay"] = {
        "type": "text",
        "title": "Copay",
        "role": "ask",
        "required": True,
        "prompt": {"ask": "What is the copay?"},
        "applicable_when": {
            "field": "sections.cov.network_status",
            "op": "eq",
            "value": "In Network",
        },
    }
    return doc


def test_real_values_evaluate_value_gates_exactly() -> None:
    """With the form's real values, a value-gated required field counts; the
    PHI-free sentinel approximation (no values passed) conservatively skips it —
    which is why only the dispatcher's nudge may use the sentinel path."""
    doc = _gated_schema()
    status = {
        "sections.cov.network_status": _human(),
        "sections.cov.plan_name": _human(),
    }
    values = {"sections.cov.network_status": "In Network", "sections.cov.plan_name": "Acme"}
    with_values = unsatisfied_required_paths(status, doc, floor=FLOOR, values=values)
    assert with_values == ["sections.cov.copay"]
    sentinel = unsatisfied_required_paths(status, doc, floor=FLOOR)
    assert sentinel == []  # sentinel reads the eq-gate as not matching


# ---------------------------------------------------------------------------
# A `role="confirm"` leaf's declared purpose is payer confirmation, so an intake
# value alone must not satisfy it (spec §4.1). Measured scope: the shipped IBV
# catalog has exactly three confirm leaves; the two spouse leaves declare
# `default: "N/A"` and so are already outside every gate population
# (`_required_paths` drops defaulted leaves) — this rule reaches `policy_number`
# alone. Constants and schema come from the compiled artifact, not hand-typed.
# ---------------------------------------------------------------------------

_FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"

POLICY_NUMBER = "sections.insurance_information.policy_number"
SPOUSE_NAME = "sections.patient_information.spouse_partner_name"
SPOUSE_DOB = "sections.patient_information.spouse_partner_dob"
COVERAGE_TYPE = "sections.benefit_coverage.coverage_type"

IBV_STANDARD_V2: dict[str, Any] = json.loads(
    (_FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text()
)
DISEASE_ONLY_V2: dict[str, Any] = json.loads(
    (_FORM_SCHEMA_DIR / "disease_only_verification.json").read_text()
)
SCHEMA_JSON = IBV_STANDARD_V2
INTAKE_VALUES: dict[str, Any] = dict.fromkeys(required_intake_fields(SCHEMA_JSON), "x")


def test_intake_does_not_satisfy_a_confirm_leaf() -> None:
    """A confirm leaf's declared purpose is payer confirmation, so the value typed at intake
    is the thing to be confirmed, not the confirmation (spec §4.1)."""
    status = {POLICY_NUMBER: FieldStatus(AnswerSource.INTAKE.value, None, None)}
    assert POLICY_NUMBER in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_call_satisfies_a_confirm_leaf() -> None:
    status = {
        POLICY_NUMBER: FieldStatus(AnswerSource.AI_CALL.value, True, 90, uuid4()),
    }
    assert POLICY_NUMBER not in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_human_edit_satisfies_a_confirm_leaf() -> None:
    """A reviewer typing the value IS a decision — only intake is excluded. Without this the
    reviewer could never clear the field and the form could never complete."""
    status = {POLICY_NUMBER: FieldStatus(AnswerSource.HUMAN.value, None, None)}
    assert POLICY_NUMBER not in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_defaulted_confirm_leaf_stays_outside_every_gate() -> None:
    """The coverage-flip case: the rep says Family mid-call and will not disclose dependent
    PHI. Both spouse leaves declare `default: "N/A"`, so they must never enter the gate
    population and never point the retry loop at data the payer cannot give (spec E8)."""
    values = dict(INTAKE_VALUES) | {COVERAGE_TYPE: "Family"}
    status = {COVERAGE_TYPE: FieldStatus(AnswerSource.AI_CALL.value, True, 95, uuid4())}
    unsat = unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=values
    )
    retryable = retryable_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=values
    )
    for path in (SPOUSE_NAME, SPOUSE_DOB):
        assert path not in unsat
        assert path not in retryable


def test_no_confirm_leaf_is_an_either_or_member_in_any_shipped_catalog() -> None:
    """The confirm rule is applied to the leaf itself, not to its either/or siblings — sound
    only while no confirm leaf has any. If a catalog adds one, revisit `_satisfied`."""
    for schema_json in (IBV_STANDARD_V2, DISEASE_ONLY_V2):
        confirm = _confirm_paths(schema_json)
        members = {
            m for pair in alternative_pairs(FormSchemaDoc.model_validate(schema_json)) for m in pair
        }
        assert not (confirm & members)
