"""Phase 1 compiler: FormSchemaDoc -> immutable, PHI-free CallPlan for one call."""

from typing import Any

from vera_core.forms.dsl import Comparison, FormSchemaDoc, RefCondition
from vera_core.forms.planning import CallPlan, PlanField, compile_call_plan


def _doc() -> FormSchemaDoc:
    """A small but representative document: gates, a ref, a nested group, a derive
    template, a plain confirm, a context section, and two tasks."""
    raw: dict[str, Any] = {
        "dsl_version": "2.1",
        "name": "Test",
        "insurance_type": "infertility_treatment",
        "shared_conditions": {
            "is_ppo": {"field": "sections.basics.plan_type", "op": "eq", "value": "PPO"}
        },
        "sections": {
            "patient": {
                "title": "Patient",
                "role": "context",
                "fields": {
                    "patient_name": {"type": "text", "title": "Patient Name", "role": "context"}
                },
            },
            "basics": {
                "title": "Basics",
                "fields": {
                    "plan_type": {
                        "type": "text",
                        "title": "Plan Type",
                        "role": "ask",
                        "required": True,
                        "prompt": {"ask": "What type of plan is this?"},
                    },
                    "policy_number": {
                        "type": "text",
                        "title": "Policy Number",
                        "role": "confirm",
                        "prompt": {
                            "confirm": "I have the policy number as {{value}} — is that right?"
                        },
                    },
                    "notes": {
                        "type": "text",
                        "title": "Notes",
                        "role": "ask",
                        "applicable_when": {
                            "field": "sections.basics.plan_type",
                            "op": "eq",
                            "value": "PPO",
                        },
                        "inapplicable_value": "N/A",
                        "prompt": {"ask": "Any notes?"},
                    },
                    "ref_gated": {
                        "type": "text",
                        "title": "Ref Gated",
                        "role": "ask",
                        "applicable_when": {"ref": "is_ppo"},
                        "inapplicable_value": "N/A",
                        "prompt": {"ask": "PPO detail?"},
                    },
                    "plan_year": {
                        "type": "date",
                        "title": "Plan Year",
                        "role": "ask",
                        "derive": {
                            "when": {
                                "field": "sections.basics.plan_type",
                                "op": "eq",
                                "value": "Calendar",
                            },
                            "value": "01/01/{{current_year}}",
                        },
                        "prompt": {"ask": "What is the plan year?"},
                    },
                    "deep": {
                        "type": "group",
                        "title": "Deep",
                        "fields": {
                            "inner_leaf": {
                                "type": "text",
                                "title": "Inner",
                                "role": "ask",
                                "prompt": {"ask": "Inner value?"},
                            }
                        },
                    },
                },
            },
            "coverage": {
                "title": "Coverage",
                "fields": {
                    "covered": {
                        "type": "enum",
                        "title": "Covered",
                        "role": "ask",
                        "values": ["Yes", "No"],
                        "prompt": {"ask": "Is it covered?"},
                    }
                },
            },
        },
        "tasks": [
            {
                "task_key": "main",
                "title": "Main",
                "intro": "Let's start.",
                "outro": "Great.",
                "sections": ["basics"],
            },
            {"task_key": "cov", "title": "Coverage", "sections": ["coverage"]},
        ],
    }
    return FormSchemaDoc.model_validate(raw)


def _compile(prefill: dict[str, str] | None = None) -> CallPlan:
    return compile_call_plan(
        _doc(), call_id="call_1", room_name="room_1", current_year=2026, prefill=prefill or {}
    )


def _field(plan: CallPlan, path: str) -> PlanField:
    for task in plan.tasks:
        for field in task.fields:
            if field.field_path == path:
                return field
    raise KeyError(path)


def test_plan_pins_schema_version_and_ids() -> None:
    plan = _compile()
    assert plan.schema_version == "2.1"
    assert plan.call_id == "call_1"
    assert plan.room_name == "room_1"


def test_tasks_are_ordered_with_intro_outro() -> None:
    plan = _compile()
    assert [t.task_key for t in plan.tasks] == ["main", "cov"]
    assert [t.order for t in plan.tasks] == [0, 1]
    main = plan.tasks[0]
    assert main.intro == "Let's start." and main.outro == "Great."


def test_ask_field_is_collect() -> None:
    field = _field(_compile(), "sections.basics.plan_type")
    assert field.status == "COLLECT"
    assert field.role == "ask"
    assert field.resolved_prompt == "What type of plan is this?"


def test_confirm_field_is_pending_context_with_no_value() -> None:
    field = _field(_compile(), "sections.basics.policy_number")
    assert field.status == "PENDING_CONTEXT"
    assert field.prefilled_value is None


def test_confirm_field_prefilled_becomes_confirm_with_value_substituted() -> None:
    field = _field(
        _compile({"sections.basics.policy_number": "W123"}), "sections.basics.policy_number"
    )
    assert field.status == "CONFIRM"
    assert field.prefilled_value == "W123"
    assert field.resolved_prompt == "I have the policy number as W123 — is that right?"


def test_context_field_prefilled_carries_value() -> None:
    plan = _compile({"sections.patient.patient_name": "Jane Doe"})
    item = next(
        c for c in plan.context_knowledge if c.field_path == "sections.patient.patient_name"
    )
    assert item.value == "Jane Doe"


def test_ask_field_ignores_prefill() -> None:
    # ask fields are always collected live, never prefilled.
    field = _field(_compile({"sections.basics.plan_type": "PPO"}), "sections.basics.plan_type")
    assert field.status == "COLLECT"
    assert field.prefilled_value is None


def test_deeply_nested_path_is_flattened() -> None:
    field = _field(_compile(), "sections.basics.deep.inner_leaf")
    assert field.resolved_prompt == "Inner value?"


def test_gate_chain_carried_unevaluated() -> None:
    field = _field(_compile(), "sections.basics.notes")
    assert len(field.applicable_when) == 1
    gate = field.applicable_when[0]
    assert isinstance(gate, Comparison)
    assert (gate.field, gate.op, gate.value) == ("sections.basics.plan_type", "eq", "PPO")


def test_ref_is_carried_with_shared_conditions() -> None:
    plan = _compile()
    field = _field(plan, "sections.basics.ref_gated")
    assert field.applicable_when == [RefCondition(ref="is_ppo")]
    assert plan.shared_conditions is not None
    assert "is_ppo" in plan.shared_conditions


def test_current_year_resolved_in_derive_default() -> None:
    field = _field(_compile(), "sections.basics.plan_year")
    assert field.derive is not None
    assert field.derive.value == "01/01/2026"


def test_context_fields_listed_without_values() -> None:
    plan = _compile()
    paths = {c.field_path: c for c in plan.context_knowledge}
    assert "sections.patient.patient_name" in paths
    assert paths["sections.patient.patient_name"].value is None


def test_plan_is_phi_free() -> None:
    """No prefilled PHI values anywhere in the serialized plan."""
    plan = _compile()
    assert all(f.prefilled_value is None for t in plan.tasks for f in t.fields)
    assert all(c.value is None for c in plan.context_knowledge)


def test_plan_round_trips_through_json() -> None:
    """The plan is stored in / read from Redis as JSON — including a `not` gate,
    whose alias is the classic round-trip trap."""
    doc = _doc()
    raw = doc.model_dump()
    raw["sections"]["basics"]["fields"]["ref_gated"]["applicable_when"] = {
        "not": {"field": "sections.basics.plan_type", "op": "eq", "value": "HMO"}
    }
    plan = compile_call_plan(
        FormSchemaDoc.model_validate(raw), call_id="c", room_name="r", current_year=2026
    )
    restored = CallPlan.model_validate_json(plan.model_dump_json(by_alias=True))
    assert restored == plan
