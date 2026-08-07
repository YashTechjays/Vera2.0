"""Runtime evaluation of DSL v2 conditions against current answer values.

Pure and DB-free (like `intake`/`review`): consumers pass the current answer map
(root-anchored path → raw value). Values compare as strings; a missing answer
compares as "" (so `eq` is false and `ne`/`not_in` are true until the field is
answered). An unknown `ref` evaluates to False — never raise or log (the values
are PHI). Mirrors the frontend evaluator (`vera-frontend/src/lib/ibv/conditions.ts`).
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
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


type AlternativeIndex = Mapping[str, tuple[str, ...]]


def alternative_pairs(doc: FormSchemaDoc) -> list[tuple[str, ...]]:
    """Either/or groups: members of ONE `alternatives` set that share a parent path.

    Grouped by parent rather than taken whole because `panel_cost_pairs` flattens every code's
    copay AND coinsurance into a single set — the diagnostic panel is 16 members over 8 CPT codes,
    since the spoken question is one question fanned across them. Treating that set as satisfied by
    any one member would mark eight codes answered off one reply. Grouping recovers the
    `cost_pair(base) -> [base.copay, base.coinsurance]` pairs the flattening erased.

    An `alternatives` over GROUPS is a routing question ("elective or cancer-related?"), not an
    either/or over two answers, so its members resolve to no leaf and drop out here."""
    return _pairs_from(list(leaf_gates(doc)), doc)


def _pairs_from(
    gated: Sequence[tuple[str, Leaf, tuple[Condition, ...]]], doc: FormSchemaDoc
) -> list[tuple[str, ...]]:
    """`alternative_pairs` over an already-walked leaf list, so a caller that needs the walk for
    other reasons pays for it once."""
    leaf_paths = {path for path, _leaf, _gates in gated}
    pairs: list[tuple[str, ...]] = []
    for section in doc.sections.values():
        for alternatives in section.alternatives or []:
            by_parent: dict[str, list[str]] = {}
            for member in alternatives.members:
                if member in leaf_paths:
                    by_parent.setdefault(member.rsplit(".", 1)[0], []).append(member)
            pairs.extend(tuple(group) for group in by_parent.values() if len(group) > 1)
    return pairs


def alternative_index(pairs: Iterable[Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Each member path mapped to the OTHER members of its either/or group."""
    index: dict[str, tuple[str, ...]] = {}
    for group in pairs:
        for path in group:
            index[path] = tuple(other for other in group if other != path)
    return index


def has_value(values: Values, path: str) -> bool:
    """Whether an answer is on file — the emptiness test the owed-set consumers share.

    Deliberately `completion_pct_v2`'s own long-standing expression, so `gap_fields` and the form's
    percentage cannot disagree about what counts as filled. That makes it differ from
    `review.is_blank_answer` on falsy non-strings (`0`, `False`): those read as blank here and as
    answered there. Reconciling the two is a behaviour decision about zero-valued answers, not a
    cleanup — do it deliberately or not at all."""
    return str(values.get(path) or "").strip() != ""


def is_satisfied(
    path: str, default: str | None, values: Values, alternatives: AlternativeIndex
) -> bool:
    """Whether a required, applicable field owes nothing — shared by `gap_fields` (and so both
    `task_complete` guards through it) and `completion_pct_v2`. `review`'s path lists apply the
    same rule through `is_field_satisfied` instead, since they may hold a sentinel value map.

    Three ways to owe nothing: a value on file; a declared `default`, which `completion_pct_v2`
    counts as filled and the export writes; or another member of its either/or group answered,
    since one reply satisfies the group — a rep who gave coinsurance answered the cost question.

    Satisfaction, NOT applicability. Nothing is made inapplicable, so when both sides of a pair
    legitimately have values — which is not uncommon — both still display and both still export."""
    return (
        has_value(values, path)
        or default is not None
        or any(has_value(values, other) for other in alternatives.get(path, ()))
    )


def _would_open_a_gated_field(
    gated: Sequence[tuple[str, Leaf, tuple[Condition, ...]]],
    values: Values,
    shared: SharedConditions,
    path: str,
    candidate: str,
) -> bool:
    """Whether recording `candidate` at `path` would make some currently-inapplicable leaf apply.

    Filling `N/A` closes the fields gated behind it, which is the point; filling a gate's expected
    value would conjure required questions out of nothing — asserting `cpt_89342.covered = "Yes"`
    summons `embryo_cryo_storage.storage_time_coverage`, which nobody asked about."""
    after = {**values, path: candidate}
    return any(
        other != path
        and not is_applicable(gates, values, shared)
        and is_applicable(gates, after, shared)
        for other, _leaf, gates in gated
    )


def alternative_fills(doc: FormSchemaDoc, values: Values, answered: str) -> dict[str, str]:
    """What to record for the empty members of `answered`'s either/or group, `{path: value}`.

    The export is the platform's final product, so the unused side has to read `$0` / `0%` rather
    than blank — a placeholder would leave the cell empty. The value recorded is the member's own
    authored `inapplicable_value`, so nothing is invented; a member without one stays blank (and
    `is_satisfied` still stops it being owed).

    Never overwrites, and never returns a value that would open a gated field — see
    `_would_open_a_gated_field`."""
    if not has_value(values, answered):
        return {}
    gated = list(leaf_gates(doc))
    siblings = alternative_index(_pairs_from(gated, doc)).get(answered, ())
    if not siblings:
        return {}
    leaves = {path: leaf for path, leaf, _gates in gated}
    shared = doc.shared_conditions or {}
    fills: dict[str, str] = {}
    for sibling in siblings:
        leaf = leaves.get(sibling)
        if leaf is None or leaf.inapplicable_value is None or has_value(values, sibling):
            continue
        if not _would_open_a_gated_field(gated, values, shared, sibling, leaf.inapplicable_value):
            fills[sibling] = leaf.inapplicable_value
    return fills


def is_required(field: HasRequired, values: Values, shared: SharedConditions) -> bool:
    """Resolve `required: bool | {when}` against the current values."""
    if isinstance(field.required, bool):
        return field.required
    return evaluate(field.required.when, values, shared)
