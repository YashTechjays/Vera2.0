"""Normalizations applied to a value before it becomes a `field_answer` row.

Written to by BOTH extraction stacks — the worker's live Observer and the control plane's
post-call top-up — which is why this is not in either of them. Read-side rules live in
`review`; this module is the write side.
"""

from collections.abc import Sequence

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import normalize_value


def canonical_special_value(value: str, special_values: Sequence[str] | None) -> str:
    """Snap an answer that IS a declared special value onto that literal's exact spelling.

    Conditions compare the stored string byte-for-byte (`forms.conditions.evaluate`), so a
    case or whitespace variant of an authored sentinel re-opens the very questions the
    sentinel exists to close — an "unlimited" deductible still owing met + remaining. It is
    not only the gate: storage IS display here, so the snap is also what keeps the export
    from showing "unlimited" beside "No Limit".

    Confined to `review.normalize_value` equality, so a real amount is never reshaped —
    currency formatting is a separate, deliberately untreated concern (`extraction_prompt`).
    """
    if not special_values:
        return value
    folded = normalize_value(value)
    return next((s for s in special_values if normalize_value(s) == folded), value)


def special_values_by_path(doc: FormSchemaDoc) -> dict[str, list[str]]:
    """Every leaf that declares `special_values`, by root-anchored path."""
    return {path: leaf.special_values for path, leaf in doc.leaf_items() if leaf.special_values}
