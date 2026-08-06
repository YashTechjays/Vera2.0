"""Runtime evaluation of DSL v2 conditions against current answer values.

Pure and DB-free (like `intake`/`review`): consumers pass the current answer map
(root-anchored path → raw value). Values compare as strings; a missing answer
compares as "" (so `eq` is false and `ne`/`not_in` are true until the field is
answered). An unknown `ref` evaluates to False — never raise or log (the values
are PHI). Mirrors the frontend evaluator (`vera-frontend/src/lib/ibv/conditions.ts`).
"""

from collections.abc import Iterator, Mapping
from typing import Any, Protocol

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
    RequiredWhen,
    condition_field_paths,
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


class HasRequired(Protocol):
    """Anything carrying the DSL's `required` shape. Structural on purpose: the rule is
    shared by the schema's `Leaf` and the compiled plan's `PlanFieldDescriptor`, which
    hold the identical field under unrelated types. One implementation keeps them from
    drifting — this rule has to agree with the form's completion-percentage maths."""

    @property
    def required(self) -> bool | RequiredWhen: ...


def is_required(field: HasRequired, values: Values, shared: SharedConditions) -> bool:
    """Resolve `required: bool | {when}` against the current values."""
    if isinstance(field.required, bool):
        return field.required
    return evaluate(field.required.when, values, shared)


def decided_at_entry(
    gate: Condition,
    task_of_path: Mapping[str, int],
    task_index: int,
    shared: SharedConditions,
) -> bool:
    """Is this gate conjunct's answer already final when `task_index` is entered?

    True when every field it references is collected by an EARLIER task — answered, or
    never answered because it was gated out upstream — or by no task at all (a
    context/prefilled leaf). A conjunct referencing this task or a later one is undecided:
    its gate question has not been asked yet, so evaluating it reads false and would
    forbid a follow-up the agent is about to need.

    Shared by the prompt compiler (which omits a decided conjunct's prose, since the
    runtime resolves it) and the worker (which drops the question outright). One
    implementation because the two disagreeing means the compiler keeps a gate the worker
    has already acted on, or the reverse — the `HasRequired` precedent above.

    A conjunct referencing NO path is treated as undecided: there is nothing whose answer
    could settle it, so the safe reading is "leave it to the live prose".
    """
    refs = set(condition_field_paths(gate, dict(shared)))
    return bool(refs) and all(task_of_path.get(ref, -1) < task_index for ref in refs)
