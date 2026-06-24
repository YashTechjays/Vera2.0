"""Pure, DB-free helpers for the IBV review + dispute-resolution endpoints.

Kept free of SQLAlchemy/FastAPI so they unit-test without a database: the endpoint
queries the ORM, maps rows into the small `AnswerRow`/`EvalRow` value objects here,
and this module assembles the field views, dispute flags, completion %, and the
adjudication-action choice.

A field is "disputed" when its current `field_answer` has a `field_evaluation` with
`supported=false` (ADR §4) and the human hasn't already adjudicated it
(`resolved_answer_ids`). PHI lives in the values — callers never log them.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from vera_core.models.enums import DisputeActionType


@dataclass(frozen=True)
class AnswerRow:
    """The bits of a `field_answer` the review assembly needs."""

    id: UUID
    field_path: str
    value: Any  # stored JSONB, e.g. {"value": ...}
    source: str
    confidence: int | None
    evidence: str | None


@dataclass(frozen=True)
class EvalRow:
    """The bits of a `field_evaluation` (LLM-judge verdict) the assembly needs."""

    supported: bool
    confidence: int | None
    evidence: str | None


def unwrap_value(stored: Any) -> Any:
    """Field answers persist as `{"value": <raw>}`; return the raw value (pass other
    shapes through unchanged)."""
    if isinstance(stored, dict) and "value" in stored:
        return stored["value"]
    return stored


def is_disputed(evaluation: EvalRow | None, *, min_confidence: int | None = None) -> bool:
    """True when the judge disagrees (`supported=false`) or — when a threshold is
    given — the judge's confidence is below it."""
    if evaluation is None:
        return False
    if not evaluation.supported:
        return True
    return (
        min_confidence is not None
        and evaluation.confidence is not None
        and evaluation.confidence < min_confidence
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
    evaluations_by_answer_id: Mapping[UUID, EvalRow],
    prior_value_by_path: Mapping[str, Any],
    *,
    resolved_answer_ids: Collection[UUID],
    min_confidence: int | None = None,
) -> list[dict[str, Any]]:
    """Assemble the flat, dotted-path field views the detail endpoint returns. Each
    item is `{field_path, value, source, confidence, dispute}`; `dispute` is non-null
    only for an unresolved, judge-flagged field."""
    views: list[dict[str, Any]] = []
    for answer in sorted(current_answers, key=lambda a: a.field_path):
        evaluation = evaluations_by_answer_id.get(answer.id)
        dispute: dict[str, Any] | None = None
        if answer.id not in resolved_answer_ids and is_disputed(
            evaluation, min_confidence=min_confidence
        ):
            assert evaluation is not None  # is_disputed(None) is False
            prior = prior_value_by_path.get(answer.field_path)
            dispute = {
                "previous_value": unwrap_value(prior) if prior is not None else None,
                "current_value": unwrap_value(answer.value),
                "confidence": evaluation.confidence,
                "evidence": answer.evidence,  # what was captured
                "reasoning": evaluation.evidence,  # why the judge disputes it
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
