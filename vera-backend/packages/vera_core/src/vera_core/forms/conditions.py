"""Runtime evaluation of DSL v2 conditions against current answer values.

Pure and DB-free (like `intake`/`review`): consumers pass the current answer map
(root-anchored path → raw value). Values compare as strings; a missing answer
compares as "" (so `eq` is false and `ne`/`not_in` are true until the field is
answered). An unknown `ref` evaluates to False — never raise or log (the values
are PHI). Mirrors the frontend evaluator (`vera-frontend/src/lib/ibv/conditions.ts`).
"""

from collections.abc import Iterator, Mapping
from typing import Any

from vera_core.forms.dsl import (
    PATH_PREFIX,
    AllCondition,
    AnyCondition,
    Condition,
    FormField,
    FormSchemaDoc,
    Group,
    Leaf,
    NotCondition,
    RefCondition,
)

Values = Mapping[str, Any]
SharedConditions = Mapping[str, Condition]


def is_v2(schema_json: Mapping[str, Any]) -> bool:
    """True when a stored schema document is DSL 2.x (else legacy v1)."""
    version = schema_json.get("dsl_version")
    return isinstance(version, str) and version.startswith("2.")


def _as_text(raw: Any) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


def evaluate(cond: Condition, values: Values, shared: SharedConditions) -> bool:
    """Evaluate one condition tree against the current values (spec §4.5)."""
    if isinstance(cond, RefCondition):
        target = shared.get(cond.ref)
        return evaluate(target, values, shared) if target is not None else False
    if isinstance(cond, AllCondition):
        return all(evaluate(c, values, shared) for c in cond.all)
    if isinstance(cond, AnyCondition):
        return any(evaluate(c, values, shared) for c in cond.any)
    if isinstance(cond, NotCondition):
        return not evaluate(cond.not_, values, shared)

    value = _as_text(values.get(cond.field))
    match cond.op:
        case "eq":
            return value == cond.value
        case "ne":
            return value != cond.value
        case "in" | "not_in":
            # The model validator guarantees a list value for in/not_in.
            members = cond.value if isinstance(cond.value, list) else [cond.value]
            return value in members if cond.op == "in" else value not in members


def leaf_gates(doc: FormSchemaDoc) -> Iterator[tuple[str, Leaf, tuple[Condition, ...]]]:
    """Every `(path, leaf, gate chain)` in document order. The chain collects each
    `applicable_when` from the section down to the leaf itself; the leaf is
    applicable when every gate holds."""

    def walk(
        prefix: str, fields: dict[str, FormField], gates: tuple[Condition, ...]
    ) -> Iterator[tuple[str, Leaf, tuple[Condition, ...]]]:
        for key, field in fields.items():
            path = f"{prefix}.{key}"
            own = (*gates, field.applicable_when) if field.applicable_when else gates
            if isinstance(field, Group):
                yield from walk(path, field.fields, own)
            else:
                yield path, field, own

    for section_key, section in doc.sections.items():
        base: tuple[Condition, ...] = (section.applicable_when,) if section.applicable_when else ()
        yield from walk(f"{PATH_PREFIX}{section_key}", section.fields, base)


def is_applicable(gates: tuple[Condition, ...], values: Values, shared: SharedConditions) -> bool:
    return all(evaluate(gate, values, shared) for gate in gates)


def is_required(leaf: Leaf, values: Values, shared: SharedConditions) -> bool:
    """Resolve `required: bool | {when}` against the current values."""
    if isinstance(leaf.required, bool):
        return leaf.required
    return evaluate(leaf.required.when, values, shared)
