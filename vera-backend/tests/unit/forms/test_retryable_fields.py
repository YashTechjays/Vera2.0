from vera_core.forms.review import (
    FieldStatus,
    field_labels,
    is_field_satisfied,
    retryable_required_paths,
)

FLOOR = 70

# Minimal v2 schema: one required askable leaf + one required readonly leaf.
V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
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
