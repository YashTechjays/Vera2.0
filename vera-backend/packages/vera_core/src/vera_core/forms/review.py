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

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from vera_core.forms.conditions import is_applicable, is_required, is_v2, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, FormSchemaDoc
from vera_core.models.enums import AnswerSource, DisputeActionType

# A judge verdict below this confidence (or unsupported) routes the field to review,
# and an AI answer below it is not "satisfied" for the retry decision. The single
# default for the whole post-call pipeline; `settings.post_call_review_floor` overrides
# it at consumer wiring time.
REVIEW_CONFIDENCE_FLOOR = 70


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


def form_completion_pct(values: Mapping[str, Any], schema_json: Mapping[str, Any]) -> float:
    """Version-gated completion %: v2 evaluates conditions against the values;
    v1 only needs which paths are filled."""
    if is_v2(schema_json):
        return completion_pct_v2(values, schema_json)
    return completion_pct(set(values), schema_json)


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


@dataclass(frozen=True)
class FieldStatus:
    """Immutable snapshot of a filled field's satisfaction state: source and AI
    confidence. An unfilled field has no status at all (absent from the map)."""

    source: str | None
    ai_supported: bool | None
    ai_confidence: int | None


def is_field_satisfied(status: FieldStatus | None, *, floor: int) -> bool:
    """True when a field's status meets retry-gate requirements: human/intake-sourced
    (trusted), or AI-sourced with supported language and confidence >= floor.
    ``None`` means the field is unfilled — never satisfied."""
    if status is None:
        return False
    if status.source in (AnswerSource.INTAKE.value, AnswerSource.HUMAN.value):
        return True
    if status.source == AnswerSource.AI_CALL.value:
        return bool(status.ai_supported) and (status.ai_confidence or 0) >= floor
    return True  # unknown source but filled — treat as satisfied


def _required_paths(
    schema_json: Mapping[str, Any], values: Mapping[str, Any], *, askable_only: bool
) -> list[str]:
    """Paths of required, applicable leaves — optionally only collectible
    (ask/confirm role) ones. v2: filters by role + applicability. v1: returns
    all required paths (no role concept)."""
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        return [
            path
            for path, leaf, gates in leaf_gates(doc)
            if (not askable_only or leaf.role in COLLECTED_ROLES)
            and is_applicable(gates, values, shared)
            and is_required(leaf, values, shared)
        ]
    return all_required_paths(schema_json)


def _gate_values(
    status_by_path: Mapping[str, FieldStatus], values: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The values conditions evaluate against. With *values* (the form's real
    current answers — PHI, so only in-session callers pass them) gates evaluate
    exactly. Without, a sentinel stands in for each filled field (PHI-free):
    presence-based gates evaluate exactly; a value-comparing gate (``eq``/``in``…)
    sees the sentinel and reads as "not matching", so its dependents are treated
    as inapplicable — a deliberate conservative approximation for the dispatcher's
    retry nudge, never for an authoritative status decision."""
    return values if values is not None else dict.fromkeys(status_by_path, "x")


def unsatisfied_required_paths(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> list[str]:
    """Paths of required, applicable fields (ANY role) that are not yet satisfied.
    The authoritative completeness check: a form may only auto-COMPLETE when this
    is empty — an unsatisfied non-askable field can never be fixed by a retry
    call, so it must route to human review instead."""
    gate_values = _gate_values(status_by_path, values)
    return [
        path
        for path in _required_paths(schema_json, gate_values, askable_only=False)
        if not is_field_satisfied(status_by_path.get(path), floor=floor)
    ]


def retryable_required_paths(
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any] | None = None,
) -> list[str]:
    """Paths of required, applicable, askable fields that are not yet satisfied.
    These are the fields a retry call should attempt to fill. See _gate_values
    for the values-vs-sentinel evaluation contract."""
    gate_values = _gate_values(status_by_path, values)
    return [
        path
        for path in _required_paths(schema_json, gate_values, askable_only=True)
        if not is_field_satisfied(status_by_path.get(path), floor=floor)
    ]


def field_labels(schema_json: Mapping[str, Any], paths: Sequence[str]) -> list[str]:
    """Human-readable labels for field paths: leaf titles in v2, else the paths themselves."""
    if not is_v2(schema_json):
        return list(paths)
    doc = FormSchemaDoc.model_validate(schema_json)
    titles = {path: leaf.title for path, leaf, _ in leaf_gates(doc)}
    return [titles.get(p, p) for p in paths]
