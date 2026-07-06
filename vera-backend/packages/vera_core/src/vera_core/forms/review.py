"""Pure, DB-free helpers for the IBV review + dispute-resolution endpoints.

Kept free of SQLAlchemy/FastAPI so they unit-test without a database: the endpoint
queries the ORM, maps rows into the small `AnswerRow` value objects here, and this
module assembles the field views, dispute flags, completion %, and the
adjudication-action choice.

A field is "disputed" when its current `field_answer` came from the AI call
(`source='ai_call'`) and its value diverges from the **baseline** — the most recent
`intake`/`human` answer for that path (`IS DISTINCT FROM` semantics: an absent baseline
counts as `NULL`, so a divergent AI value is disputed even with no prior). The signal is
derived purely from `field_answer` history — `field_evaluation` plays no part, and
`dispute_action` is a pure audit record that does not gate the dispute. PHI lives in the
values — callers never log them.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from vera_core.forms.conditions import is_applicable, is_required, leaf_gates
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.models.enums import AnswerSource, DisputeActionType


@dataclass(frozen=True)
class AnswerRow:
    """The bits of a `field_answer` the review assembly needs."""

    id: UUID
    field_path: str
    value: Any  # stored JSONB, e.g. {"value": ...}
    source: str
    confidence: int | None
    evidence: str | None


def unwrap_value(stored: Any) -> Any:
    """Field answers persist as `{"value": <raw>}`; return the raw value (pass other
    shapes through unchanged)."""
    if isinstance(stored, dict) and "value" in stored:
        return stored["value"]
    return stored


# Strip only ASCII whitespace (not the default `str.strip()`, which also folds Unicode
# whitespace like U+00A0). A deliberately conservative, stable rule: a non-ASCII space is
# retained, so it still counts as a real value difference.
_ASCII_WHITESPACE = " \t\n\r\f\v"


def normalize_value(value: Any) -> Any:
    """Canonicalize a value for dispute comparison: strings are stripped (ASCII whitespace
    only) + lowercased so case- and whitespace-only differences are not disputes;
    non-strings (numbers, bools, null, objects) pass through unchanged. This is the sole
    dispute-normalization rule — both the detail view and the complete-gate/resolve count
    go through `is_disputed` / `build_field_views`, so there is no second (SQL)
    implementation to keep in sync."""
    if isinstance(value, str):
        return value.strip(_ASCII_WHITESPACE).lower()
    return value


def is_disputed(current: AnswerRow, baseline_value: Any) -> bool:
    """True when the current value came from the AI call and diverges from the
    human/intake baseline. `baseline_value` is the stored baseline (`{"value": ...}`) or
    `None` if absent; `!=` matches `IS DISTINCT FROM` semantics for `None`. Values are
    normalized first, so case/whitespace-only differences are not disputes."""
    if current.source != AnswerSource.AI_CALL.value:
        return False
    return bool(
        normalize_value(unwrap_value(current.value))
        != normalize_value(unwrap_value(baseline_value))
    )


def all_required_paths(schema_json: Mapping[str, Any]) -> list[str]:
    """Dotted paths of every `required_state == "required"` field across all sections."""
    paths: list[str] = []
    for section in schema_json.get("sections", []):
        section_key = section.get("section_key", "")
        for field_key, field_def in (section.get("properties") or {}).items():
            if isinstance(field_def, dict) and field_def.get("required_state") == "required":
                paths.append(f"{section_key}.{field_key}")
    return sorted(paths)


def completion_pct(filled_paths: Collection[str], schema_json: Mapping[str, Any]) -> float:
    """Percentage (0-100, 2 dp) of required fields that have a value."""
    required = all_required_paths(schema_json)
    if not required:
        return 0.0
    filled = sum(1 for path in required if path in filled_paths)
    return round(filled / len(required) * 100, 2)


def completion_pct_v2(values: Mapping[str, Any], schema_json: Mapping[str, Any]) -> float:
    """DSL 2.x completion (0-100, 2 dp): required ∧ applicable leaves, evaluated
    against the current answer values (`applicable_when` chains from the section
    down, `required: bool | {when}`). A leaf with a declared `default` counts as
    filled — display/export assume it (spec §4.4). Mirrors the frontend's
    `completionPercent`."""
    doc = FormSchemaDoc.model_validate(schema_json)
    shared = doc.shared_conditions or {}
    relevant = [
        (path, leaf)
        for path, leaf, gates in leaf_gates(doc)
        if is_applicable(gates, values, shared) and is_required(leaf, values, shared)
    ]
    if not relevant:
        return 100.0
    filled = sum(
        1
        for path, leaf in relevant
        if leaf.default is not None or str(values.get(path) or "").strip() != ""
    )
    return round(filled / len(relevant) * 100, 2)


def adjudication_action(new_value: Any, current_value: Any, prior_values: Collection[Any]) -> str:
    """Which `DisputeActionType` a human edit represents: ACCEPT (unchanged),
    OVERRIDE (reverted to a known prior value), else CORRECT (a fresh value)."""
    if new_value == current_value:
        return DisputeActionType.ACCEPT.value
    if new_value in prior_values:
        return DisputeActionType.OVERRIDE.value
    return DisputeActionType.CORRECT.value


def build_field_views(
    current_answers: Iterable[AnswerRow],
    baseline_value_by_path: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Assemble the flat, dotted-path field views the detail endpoint returns. Each
    item is `{field_path, value, source, confidence, dispute}`; `dispute` is non-null
    only when the current AI value diverges from the human/intake baseline.

    `baseline_value_by_path` maps a field path to its most recent intake/human stored
    value (`{"value": ...}`); a missing entry means no baseline (treated as `None`)."""
    views: list[dict[str, Any]] = []
    for answer in sorted(current_answers, key=lambda a: a.field_path):
        baseline = baseline_value_by_path.get(answer.field_path)
        dispute: dict[str, Any] | None = None
        if is_disputed(answer, baseline):
            dispute = {
                "previous_value": unwrap_value(baseline),
                "current_value": unwrap_value(answer.value),
                "confidence": answer.confidence,  # the AI answer's own confidence
                "evidence": answer.evidence,  # what the AI captured
                "reasoning": None,  # field_evaluation is not part of disputes
            }
        views.append(
            {
                "field_path": answer.field_path,
                "value": unwrap_value(answer.value),
                "source": answer.source,
                "confidence": answer.confidence,
                "dispute": dispute,
            }
        )
    return views
