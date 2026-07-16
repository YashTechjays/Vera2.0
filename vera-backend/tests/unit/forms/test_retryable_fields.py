from typing import Any, cast

from vera_core.forms.dsl import PromotedFields
from vera_core.forms.review import (
    FieldStatus,
    field_labels,
    is_field_satisfied,
    retryable_required_paths,
    unsatisfied_required_paths,
)

FLOOR = 70

# Minimal v2 schema: one required askable leaf + one required readonly leaf.
V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    # The DSL requires every promoted column mapped to a system_fields target —
    # point them all at one leaf (same shortcut as test_conditions.py).
    "system_fields": {"network_status": "sections.cov.network_status"},
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
