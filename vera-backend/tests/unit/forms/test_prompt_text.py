"""Deterministic condition → English rendering."""

from typing import Any

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompt_text import build_condition_renderer


def doc_with(conditions: dict[str, Any] | None = None) -> FormSchemaDoc:
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "T",
            "insurance_type": "infertility_treatment",
            "shared_conditions": conditions or {},
            "sections": {
                "a": {
                    "title": "A",
                    "fields": {
                        "x": {
                            "type": "text",
                            "title": "Plan Type",
                            "role": "ask",
                            "prompt": {"ask": "x?"},
                        },
                        "dup": {
                            "type": "text",
                            "title": "Copay",
                            "role": "ask",
                            "prompt": {"ask": "d?"},
                        },
                    },
                },
                "b": {
                    "title": "B",
                    "fields": {
                        "dup": {
                            "type": "text",
                            "title": "Copay",
                            "role": "ask",
                            "prompt": {"ask": "d?"},
                        }
                    },
                },
            },
            "tasks": [{"task_key": "main", "title": "Main", "sections": ["a", "b"]}],
        }
    )


def test_comparison_ops() -> None:
    render = build_condition_renderer(doc_with())
    eq = {"field": "sections.a.x", "op": "eq", "value": "PPO"}
    assert render(_cond(eq)) == '"Plan Type" is "PPO"'
    assert render(_cond({**eq, "op": "ne"})) == '"Plan Type" is not "PPO"'
    assert (
        render(_cond({"field": "sections.a.x", "op": "in", "value": ["PPO", "HMO"]}))
        == '"Plan Type" is one of "PPO", "HMO"'
    )
    assert (
        render(_cond({"field": "sections.a.x", "op": "not_in", "value": ["N/A"]}))
        == '"Plan Type" is none of "N/A"'
    )


def test_duplicate_titles_get_ancestor_disambiguation() -> None:
    render = build_condition_renderer(doc_with())
    text = render(_cond({"field": "sections.b.dup", "op": "eq", "value": "1"}))
    assert text == '"Copay" (B) is "1"'


def test_nesting_and_ref_expansion() -> None:
    shared = {"fam": {"field": "sections.a.x", "op": "eq", "value": "Family"}}
    render = build_condition_renderer(doc_with(shared))
    cond = _cond(
        {
            "all": [
                {"ref": "fam"},
                {
                    "any": [
                        {"field": "sections.a.x", "op": "eq", "value": "PPO"},
                        {"not": {"field": "sections.a.x", "op": "eq", "value": "HMO"}},
                    ]
                },
            ]
        }
    )
    assert render(cond) == (
        '"Plan Type" is "Family" and ("Plan Type" is "PPO" or not ("Plan Type" is "HMO"))'
    )


def test_ref_expanding_to_any_gets_parenthesized_inside_all() -> None:
    shared = {
        "opts": {
            "any": [
                {"field": "sections.a.x", "op": "eq", "value": "PPO"},
                {"field": "sections.a.x", "op": "eq", "value": "HMO"},
            ]
        }
    }
    render = build_condition_renderer(doc_with(shared))
    cond = _cond(
        {"all": [{"ref": "opts"}, {"field": "sections.a.x", "op": "eq", "value": "Family"}]}
    )
    assert render(cond) == (
        '("Plan Type" is "PPO" or "Plan Type" is "HMO") and "Plan Type" is "Family"'
    )


def test_cyclic_refs_do_not_recurse_forever() -> None:
    shared = {"a": {"ref": "b"}, "b": {"ref": "a"}}
    render = build_condition_renderer(doc_with(shared))
    assert isinstance(render(_cond({"ref": "a"})), str)


def _cond(data: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter

    from vera_core.forms.dsl import Condition

    return TypeAdapter(Condition).validate_python(data)
