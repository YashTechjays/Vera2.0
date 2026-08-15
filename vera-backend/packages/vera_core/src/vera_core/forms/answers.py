"""Canonicalization of an answer against the literals its leaf authors.

`conditions.evaluate` compares answers byte-for-byte, so the invariant is not "normalize on
write" but "every string a gate may compare carries the authored spelling" — which covers a
row about to be written, a prefill seeded into the worker's gate baseline, and an in-memory
decision map alike. Dispute-comparison rules live in `review`; this module owns that one
invariant, for every writer and every gate baseline.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from vera_core.forms.consistency import parse_currency
from vera_core.forms.dsl import FormSchemaDoc, LeafType
from vera_core.forms.review import normalize_value, strip_answer


class AuthoredField(Protocol):
    """The literal-bearing attributes `dsl.Leaf` and `call_plan.PlanFieldDescriptor` share."""

    type: LeafType
    values: list[str] | None
    special_values: list[str] | None
    default: str | None
    inapplicable_value: str | None


@dataclass(frozen=True)
class LeafLiterals:
    """One leaf's authored literals, sliced by who reads them."""

    #: Every literal `conditions.evaluate` may compare — what an answer snaps onto.
    gate: tuple[str, ...]
    #: The named answers an extraction prompt spells out. Empty for an enum, whose own
    #: `(one of: …)` clause carries its vocabulary, and which must not be told that the
    #: auto-fill's `inapplicable_value` is something the representative can say.
    spoken: tuple[str, ...]
    #: A currency leaf's "$0.00", "0" and "$0" are one answer; no other type compares by amount.
    money: bool


def literals_of(field: AuthoredField) -> LeafLiterals:
    """One leaf's literals — `gate` is the union `intake.enum_accepted_values` accepts."""
    declared = (
        *(field.values or ()),
        *(field.special_values or ()),
        field.default,
        field.inapplicable_value,
    )
    return LeafLiterals(
        gate=tuple(dict.fromkeys(literal for literal in declared if literal)),
        spoken=() if field.values else tuple(field.special_values or ()),
        money=field.type == "currency",
    )


def canonical_answer(value: Any, literals: LeafLiterals | None) -> Any:
    """Snap an answer that IS one of its leaf's authored literals onto that spelling.

    A case, padding or money-format variant re-opens the very questions the literal exists to
    close — an "unlimited" deductible still owing met + remaining — and, since storage IS
    display here, ships the variant into the export beside the authored one. Non-strings pass
    through as in `review.normalize_value`; an answer matching no literal is only stripped,
    since currency SHAPE stays a separate, untreated concern (`extraction_prompt`).
    """
    if not isinstance(value, str):
        return value
    stripped = strip_answer(value)
    if literals is None:
        return stripped
    folded = normalize_value(stripped)
    exact = next((s for s in literals.gate if normalize_value(s) == folded), None)
    if exact is not None:
        return exact
    # "$0.00" and "0" are the likeliest spellings of a `$0` sentinel and fold onto neither.
    if not literals.money or (amount := parse_currency(stripped)) is None:
        return stripped
    return next((s for s in literals.gate if parse_currency(s) == amount), stripped)


def leaf_literals(doc: FormSchemaDoc) -> dict[str, LeafLiterals]:
    """Every leaf that authors at least one literal, by root-anchored path — one walk, so no
    caller has to pick between two near-identical maps of the same document."""
    return {
        path: literals for path, leaf in doc.leaf_items() if (literals := literals_of(leaf)).gate
    }


def spoken_literals(literals: Mapping[str, LeafLiterals]) -> dict[str, tuple[str, ...]]:
    """The named answers each path's extraction prompt must spell out."""
    return {path: leaf.spoken for path, leaf in literals.items() if leaf.spoken}


def canonicalize_answers(
    answers: Sequence[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """`canonical_answer` over a flattened `(path, raw)` answer list."""
    literals = leaf_literals(doc)
    return [(path, canonical_answer(raw, literals.get(path))) for path, raw in answers]
